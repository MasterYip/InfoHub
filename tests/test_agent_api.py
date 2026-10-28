"""Agent API v1 against a real server instance and a synthetic database.

Covers the ten acceptance checks in docs/agent-api.md: tool parity with the Chat
tab, scopes, dry runs, the context budget, incremental cursors, error shapes and
authentication.
"""
import copy
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from infohub.apps import agent_api, auth, chat
from infohub.apps.server import serve
from infohub.config import DEFAULTS
from infohub.organize import units as U
from infohub.store import Store

PREFIX = "/api/agent/v1"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def strip_refs(value):
    if isinstance(value, dict):
        return {k: strip_refs(v) for k, v in value.items() if k not in ("ref", "html_url")}
    if isinstance(value, list):
        return [strip_refs(v) for v in value]
    return value


def seed(store):
    now = int(time.time())
    for index in range(60):
        kind = ("todo", "event", "topic", "question", "decision", "note")[index % 6]
        uid = U.upsert(store, None, kind=kind, title=f"Unit {index} alpha",
                       status="open" if index % 3 else "done", origin="user",
                       due_at=now + index * 3600 if kind in ("todo", "event") else None)
        if index == 5:
            store.q("UPDATE units SET summary=? WHERE id=?", ("A decision about alpha rollout", uid))
    for index in range(20):
        store.q("INSERT INTO messages(source, account, item_key, channel, channel_name, sender, sender_name, "
                "sent_at, text) VALUES('lark','org1',?, 'c1','Lab','u1','Ada',? ,?)",
                (f"m{index}", now - index * 60, f"alpha message {index}"))
    parent = U.upsert(store, None, kind="topic", title="Parent topic", origin="user")
    child = U.upsert(store, None, kind="todo", title="Child task", origin="user")
    U.link(store, parent, child)
    store.db.commit()
    return store


class AgentApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = copy.deepcopy(DEFAULTS)
        cfg["hub"]["database"] = str(Path(cls.tmp.name) / "hub.db")
        cfg["hub"]["backup"].update(enabled=False, directory=str(Path(cls.tmp.name) / "backups"))
        cfg["hub"]["auth"]["users_file"] = str(Path(cls.tmp.name) / "users.local.json")
        cfg["hub"]["dashboard"]["port"] = cls.port = free_port()
        cls.cfg = cfg
        cls.store = seed(Store(cfg["hub"]["database"]))
        threading.Thread(target=serve, args=(cfg, cls.store, lambda: {}), daemon=True).start()
        time.sleep(0.5)
        auth.set_password(cfg, "me", "correct horse battery")     # turn auth on
        cls.read = auth.add_token(cfg, "reader")
        cls.write = auth.add_token(cfg, "writer", scopes=["read", "write"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def req(self, method, path, body=None, token=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=payload, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    # ---- 1. discovery ------------------------------------------------
    def test_01_tools_match_the_chat_tab(self):
        code, body = self.req("GET", f"{PREFIX}/tools", token=self.read)
        self.assertEqual(code, 200)
        self.assertEqual([t["name"] for t in body["tools"]],
                         [t["function"]["name"] for t in chat.TOOLS])
        for mine, theirs in zip(body["tools"], chat.TOOLS):
            self.assertEqual(mine["parameters"], theirs["function"]["parameters"])
            self.assertEqual(mine["writes"], mine["name"] in chat.WRITE_TOOLS)
            self.assertEqual(mine["scope"], "write" if mine["writes"] else "read")

    # ---- 2. read parity ----------------------------------------------
    def test_02_read_tools_return_exactly_what_chat_returns(self):
        unit_id = self.store.q("SELECT id FROM units WHERE title='Unit 5 alpha'").fetchone()["id"]
        first = self.store.q("SELECT id FROM messages ORDER BY id LIMIT 2").fetchall()
        cases = [
            ("search", {"query": "alpha"}),
            ("list_units", {"kind": "todo"}),
            ("get_unit", {"id": unit_id}),
            ("get_messages", {"message_ids": [r["id"] for r in first]}),
            ("list_tags", {}),
            ("get_graph", {"kind": "todo"}),
        ]
        for name, args in cases:
            with self.subTest(tool=name):
                code, body = self.req("POST", f"{PREFIX}/tool/{name}", args, token=self.read)
                self.assertEqual(code, 200, body)
                expected = chat.run_tool(self.cfg, self.store, name, args, [])
                self.assertEqual(strip_refs(body), strip_refs(expected))

    # ---- 3. scopes ----------------------------------------------------
    def test_03_write_requires_the_write_scope(self):
        before = self.store.q("SELECT count(*) FROM units").fetchone()[0]
        code, body = self.req("POST", f"{PREFIX}/tool/create_unit",
                              {"kind": "note", "title": "blocked"}, token=self.read)
        self.assertEqual(code, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        self.assertEqual(self.store.q("SELECT count(*) FROM units").fetchone()[0], before)

    # ---- 4. dry run ---------------------------------------------------
    def test_04_dry_run_changes_nothing(self):
        before = self.store.q("SELECT count(*) FROM units").fetchone()[0]
        code, body = self.req("POST", f"{PREFIX}/tool/create_unit?dry_run=1",
                              {"kind": "note", "title": "preview only"}, token=self.write)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["dry_run"])
        self.assertFalse(body["changed"])
        self.assertIn("created", body["would"])
        self.assertEqual(self.store.q("SELECT count(*) FROM units").fetchone()[0], before)

        code, body = self.req("POST", f"{PREFIX}/tool/create_unit",
                              {"kind": "note", "title": "really created"}, token=self.write)
        self.assertEqual(code, 200)
        self.assertIsInstance(body["created"], int)
        self.assertEqual(self.store.q("SELECT count(*) FROM units").fetchone()[0], before + 1)

    # ---- 5. context budget -------------------------------------------
    def test_05_context_respects_the_token_budget(self):
        code, body = self.req("GET", f"{PREFIX}/context?days=30&tokens=800", token=self.read)
        self.assertEqual(code, 200, body)
        self.assertLessEqual(len(json.dumps(body, ensure_ascii=False)), 800 * 4)
        self.assertTrue(body["truncated"]["sections_dropped"], body["truncated"])
        self.assertEqual(body["trust"], "untrusted")
        self.assertIn("brief_markdown", body)

    def test_05b_context_default_is_compact_and_cited(self):
        code, body = self.req("GET", f"{PREFIX}/context", token=self.read)
        self.assertEqual(code, 200)
        self.assertTrue(body["units"])
        self.assertTrue(body["units"][0]["ref"].startswith("[[u:"))
        self.assertGreaterEqual(body["counts"]["units"], 60)

    # ---- 6. incremental cursor ---------------------------------------
    def test_06_changes_cursor_round_trip(self):
        code, body = self.req("GET", f"{PREFIX}/changes?limit=5", token=self.read)
        self.assertEqual(code, 200)
        cursor = body["cursor"]
        code, created = self.req("POST", f"{PREFIX}/tool/create_unit",
                                 {"kind": "note", "title": "after the cursor"}, token=self.write)
        self.assertEqual(code, 200, created)
        new_id = created["created"]
        code, body = self.req("GET", f"{PREFIX}/changes?since={cursor}", token=self.read)
        self.assertEqual(code, 200, body)
        self.assertIn(new_id, [item["id"] for item in body["items"]])
        code, bad = self.req("GET", f"{PREFIX}/changes?since=not-a-cursor", token=self.read)
        self.assertEqual(code, 400)
        self.assertEqual(bad["error"]["code"], "invalid_argument")

    # ---- 7. limits ----------------------------------------------------
    def test_07_more_than_ten_messages_is_rejected(self):
        ids = [r["id"] for r in self.store.q("SELECT id FROM messages LIMIT 11").fetchall()]
        code, body = self.req("POST", f"{PREFIX}/tool/get_messages", {"message_ids": ids}, token=self.read)
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["code"], "invalid_argument")

    # ---- 8. error shape ----------------------------------------------
    def test_08_errors_are_status_codes_with_a_code(self):
        code, body = self.req("GET", f"{PREFIX}/units/999999", token=self.read)
        self.assertEqual(code, 404)
        self.assertEqual(body["error"]["code"], "not_found")

        code, body = self.req("POST", f"{PREFIX}/tool/nope", {}, token=self.read)
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["code"], "invalid_argument")

        code, body = self.req("POST", f"{PREFIX}/tool/list_units", {"nonsense": 1}, token=self.read)
        self.assertEqual(code, 400)
        self.assertIn("nonsense", body["error"]["message"])

        code, body = self.req("POST", f"{PREFIX}/tool/link_units",
                              {"parent_id": 1, "child_id": 1}, token=self.write)
        self.assertEqual(code, 409)
        self.assertEqual(body["error"]["code"], "conflict")

    # ---- 9. audit -----------------------------------------------------
    def test_09_writes_are_audited_without_message_text(self):
        self.req("POST", f"{PREFIX}/tool/create_unit",
                 {"kind": "note", "title": "audited write"}, token=self.write)
        rows = agent_api.recent_audit(self.store, 5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["tool"], "create_unit")
        self.assertTrue(rows[0]["principal"].startswith("token:"))
        self.assertNotIn("alpha", rows[0]["target"])

    # ---- 10. auth -----------------------------------------------------
    def test_10_authentication_is_required(self):
        code, body = self.req("GET", f"{PREFIX}/tools")
        self.assertEqual(code, 401)
        self.assertEqual(body["error"]["code"], "unauthenticated")
        self.assertEqual(self.req("GET", f"{PREFIX}/tools", token="ih_wrong")[0], 401)
        self.assertEqual(self.req("GET", f"{PREFIX}/tools", token=self.read)[0], 200)


class TokenScopeTests(unittest.TestCase):
    def test_default_scopes_are_read_and_organize(self):
        self.assertEqual(auth.token_scopes({}), ("read", "organize"))

    def test_legacy_tokens_get_the_migration_default(self):
        self.assertEqual(auth.token_scopes({"hash": "x"}), ("read", "organize"))

    def test_unknown_scope_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = copy.deepcopy(DEFAULTS)
            cfg["hub"]["auth"]["users_file"] = str(Path(tmp) / "users.json")
            with self.assertRaises(ValueError):
                auth.add_token(cfg, "x", scopes=["read", "admin"])


if __name__ == "__main__":
    unittest.main()
