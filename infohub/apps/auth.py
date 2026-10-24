"""Login for the dashboard and API when the hub is reachable from other devices.

- Accounts live in config/users.local.json (git-ignored): PBKDF2-SHA256 hashes with per-user salt, never passwords.
  Manage with `python -m infohub user add|passwd|remove|list` and `python -m infohub token add|remove|list`.
- Browser sessions: random 256-bit token in an HttpOnly, SameSite=Strict cookie (Secure when TLS is on);
  only its SHA-256 is stored (table `sessions`), so a copied database cannot be replayed as a login.
- API tokens (for agents/scripts): `Authorization: Bearer <token>`, also stored hashed.
- Failed logins are throttled per client address.
`hub.auth.mode`: "auto" (required as soon as one user exists), "on", or "off".
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from ..config import ROOT

ITERATIONS = 600_000
COOKIE = "infohub_session"
_DUMMY = {"salt": "00" * 16, "hash": "00" * 32, "iter": ITERATIONS}


def users_file(cfg) -> Path:
    p = Path(cfg["hub"]["auth"]["users_file"])
    return p if p.is_absolute() else ROOT / p


def load(cfg) -> dict:
    p = users_file(cfg)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data.setdefault("users", {})
    data.setdefault("tokens", {})
    return data


def save(cfg, data: dict) -> None:
    p = users_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def _pbkdf2(password: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations).hex()


def set_password(cfg, user: str, password: str) -> None:
    if not user or not user.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValueError("user name: letters, digits, . _ - only")
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    data = load(cfg)
    salt = secrets.token_hex(16)
    data["users"][user] = {"salt": salt, "hash": _pbkdf2(password, salt, ITERATIONS), "iter": ITERATIONS,
                           "updated": int(time.time())}
    save(cfg, data)


def remove_user(cfg, user: str) -> bool:
    data = load(cfg)
    found = data["users"].pop(user, None) is not None
    save(cfg, data)
    return found


def verify_password(cfg, user: str, password: str) -> bool:
    rec = load(cfg)["users"].get(user)
    ref = rec or _DUMMY                       # same work for unknown users (no user enumeration by timing)
    ok = hmac.compare_digest(_pbkdf2(password, ref["salt"], int(ref["iter"])), ref["hash"])
    return bool(rec) and ok


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def add_token(cfg, name: str) -> str:
    token = "ih_" + secrets.token_urlsafe(32)
    data = load(cfg)
    data["tokens"][name] = {"hash": _sha(token), "created": int(time.time())}
    save(cfg, data)
    return token


def remove_token(cfg, name: str) -> bool:
    data = load(cfg)
    found = data["tokens"].pop(name, None) is not None
    save(cfg, data)
    return found


def check_token(cfg, token: str) -> str | None:
    h = _sha(token)
    for name, rec in load(cfg)["tokens"].items():
        if hmac.compare_digest(rec["hash"], h):
            return f"token:{name}"
    return None


def required(cfg) -> bool:
    mode = cfg["hub"]["auth"]["mode"]
    if mode == "on":
        return True
    if mode == "off":
        return False
    return bool(load(cfg)["users"])


class Sessions:
    def __init__(self, store, days: int):
        self.store, self.ttl = store, int(days) * 86400

    def create(self, user: str, client: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.store.q("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (_sha(token), user, now, now + self.ttl, now, client))
        self.store.q("DELETE FROM sessions WHERE expires_at < ?", (now,))
        self.store.db.commit()
        return token

    def user(self, token: str) -> str | None:
        if not token:
            return None
        now = int(time.time())
        row = self.store.q("SELECT user, expires_at, last_seen FROM sessions WHERE token_hash=?", (_sha(token),)).fetchone()
        if not row or row["expires_at"] < now:
            return None
        if now - row["last_seen"] > 3600:                       # sliding expiry, at most one write per hour
            self.store.q("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?", (now, now + self.ttl, _sha(token)))
            self.store.db.commit()
        return row["user"]

    def delete(self, token: str) -> None:
        self.store.q("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))
        self.store.db.commit()

    def revoke_user(self, user: str) -> int:
        n = self.store.q("DELETE FROM sessions WHERE user=?", (user,)).rowcount
        self.store.db.commit()
        return n


class RateLimiter:
    """At most `limit` failed logins per client within `window` seconds."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window, self.fails = limit, window, {}

    def blocked(self, client: str) -> bool:
        now = time.time()
        recent = [t for t in self.fails.get(client, []) if now - t < self.window]
        self.fails[client] = recent
        return len(recent) >= self.limit

    def fail(self, client: str) -> None:
        self.fails.setdefault(client, []).append(time.time())

    def reset(self, client: str) -> None:
        self.fails.pop(client, None)


LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Info Hub · Login</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f141c;color:#e8eef6;
font:15px/1.5 system-ui,"Segoe UI","Microsoft YaHei",sans-serif}form{background:#161d27;border:1px solid #232c39;
border-radius:14px;padding:28px;width:min(340px,90vw)}h1{font-size:18px;margin:0 0 16px}label{display:block;
font-size:13px;color:#93a2b6;margin:10px 0 4px}input{width:100%;box-sizing:border-box;background:#1c2430;
border:1px solid #2a3442;border-radius:8px;color:#e8eef6;padding:9px 10px;font-size:15px}button{margin-top:18px;
width:100%;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:10px;font-size:15px;cursor:pointer}
.err{color:#f87171;font-size:13px;margin-top:10px;min-height:1em}</style></head><body>
<form method="post" action="/login"><h1>Info Hub</h1>
<input type="hidden" name="next" value="__NEXT__">
<label for="u">Account</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Log in</button><div class="err">__ERROR__</div></form></body></html>"""
