"""Login, session cookie, API token and request guards against a real server instance (synthetic data)."""
import copy
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from infohub.apps import auth
from infohub.apps.server import serve
from infohub.config import DEFAULTS
from infohub.store import Store


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Auth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # server thread keeps the DB open on Windows
        cfg = copy.deepcopy(DEFAULTS)
        cfg["hub"]["database"] = str(Path(cls.tmp.name) / "hub.db")
        cfg["hub"]["backup"].update(enabled=False, directory=str(Path(cls.tmp.name) / "backups"))
        cfg["hub"]["auth"]["users_file"] = str(Path(cls.tmp.name) / "users.local.json")
        cfg["hub"]["dashboard"]["port"] = cls.port = free_port()
        cls.cfg = cfg
        threading.Thread(target=serve, args=(cfg, Store(cfg["hub"]["database"]), lambda: {}), daemon=True).start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if isinstance(body, dict):
            body, h["Content-Type"] = urlencode(body), "application/x-www-form-urlencoded"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def login(self, user, pw, nxt="/"):
        return self.req("POST", "/login", {"username": user, "password": pw, "next": nxt})

    def test_flow(self):
        # no users yet: auto mode leaves the local dashboard open
        self.assertEqual(self.req("GET", "/api/today")[0], 200)
        with self.assertRaises(ValueError):
            auth.set_password(self.cfg, "me", "short")
        auth.set_password(self.cfg, "me", "correct horse battery")
        stored = Path(self.cfg["hub"]["auth"]["users_file"]).read_text(encoding="utf-8")
        self.assertNotIn("correct horse", stored)                         # only a salted hash is stored
        # now everything except /login and /api/health needs a session
        self.assertEqual(self.req("GET", "/api/today")[0], 401)
        st, hd, _ = self.req("GET", "/")
        self.assertEqual((st, hd["Location"]), (303, "/login?next=%2F"))
        self.assertEqual(self.req("GET", "/login")[0], 200)
        self.assertEqual(self.req("GET", "/api/health")[0], 200)
        self.assertEqual(self.login("me", "wrong password!!")[0], 401)
        self.assertEqual(self.login("nobody", "correct horse battery")[0], 401)
        st, hd, _ = self.login("me", "correct horse battery", nxt="//evil.example/x")
        self.assertEqual((st, hd["Location"]), (303, "/"))                 # no open redirect
        cookie = hd["Set-Cookie"]
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"):
            self.assertIn(attr, cookie)
        jar = {"Cookie": cookie.split(";")[0]}
        self.assertEqual(self.req("GET", "/api/today", headers=jar)[0], 200)
        self.assertEqual(self.req("GET", "/api/today", headers={"Cookie": "infohub_session=forged"})[0], 401)
        # writes must be same-origin, and the Host header must be known
        self.assertEqual(self.req("POST", "/api/poll", "{}", {**jar, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/poll", "{}", {**jar, "Origin": f"http://127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(self.req("GET", "/api/today", headers={**jar, "Host": "evil.example"})[0], 403)
        # API token for agents
        token = auth.add_token(self.cfg, "codex")
        self.assertEqual(self.req("GET", "/api/agent/context", headers={"Authorization": f"Bearer {token}"})[0], 200)
        self.assertEqual(self.req("GET", "/api/today", headers={"Authorization": "Bearer ih_wrong"})[0], 401)
        # logout ends the session
        st, hd, _ = self.req("POST", "/logout", "", jar)
        self.assertEqual(st, 303)
        self.assertIn("Max-Age=0", hd["Set-Cookie"])
        self.assertEqual(self.req("GET", "/api/today", headers=jar)[0], 401)
        # throttling: after 5 failures even the right password is refused for a while
        for _ in range(5):
            self.login("me", "nope nope nope")
        self.assertEqual(self.login("me", "correct horse battery")[0], 429)

    def test_public_bind_requires_a_user(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["hub"]["auth"]["users_file"] = str(Path(self.tmp.name) / "empty.local.json")
        cfg["hub"]["dashboard"].update(bind="0.0.0.0", port=free_port())
        with self.assertRaises(SystemExit):
            serve(cfg, Store(Path(self.tmp.name) / "other.db"), lambda: {})


if __name__ == "__main__":
    unittest.main()
