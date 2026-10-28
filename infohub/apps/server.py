"""Layer 3: local dashboard + JSON API for people and agents (stdlib HTTP, 127.0.0.1 by default).

GET  /api/summary?hours=24          message lanes (Layer 1 triage)
GET  /api/today?scope=mine|all      needs-you messages, due todos, upcoming events
GET  /api/live                      in-progress conversations (open segments) not yet organized
POST /api/organize-now              {"channel": optional} force-organize open segments right now
GET  /api/units?tags=a,b&kind=&status=&q=&days=&order=due|activity&roots=1
GET  /api/graph?tags=&kind=&status=&q=&days=&mine=&order=&roots=&context=0|1  strict matches + optional context
GET  /api/unit/<id>?depth=2         one unit with parents, children (recursive), tags, evidence messages
GET  /api/tags?days=&prefix=        tag counts (for filter chips)
GET  /api/views                     saved views from config (apps.views)
GET  /api/channels?days=30          every chat with its policy, mute state and volume (settings page)
GET  /api/channels/defaults         preview what the default rules would change
POST /api/channels/defaults         {"apply": true} apply them; {"source": "lark"} to limit
POST /api/channels/batch            {"policy": "watch", "chats": [{"source","channel"}, ...]}
GET  /api/people?q=&limit=40        senders (for VIP management); includes who is already VIP
GET  /api/backups                   last verified backup, next due time, directory and errors
POST /api/backups                   queue a manual backup without blocking the dashboard
GET  /api/settings                  editable settings + current values
POST /api/settings                  {"ai.enabled": true, ...} -> config/settings.local.json
GET  /api/schedules                 user-authored assistant schedules
POST /api/schedules                 create/update/delete a schedule
GET  /api/brief?days=7&tags=        Markdown brief for agents
GET  /api/search?q=                 everything: units, messages, people, channels, tags
GET  /api/status                    last runs per layer, AI queue and spend
GET  /api/agent/next?limit=20       lease organizing jobs for an external agent (see organize/agent_bridge.py)
POST /api/agent/submit              {"results": [{"job_id", "result"}]}
GET  /api/agent/context?days=7      brief + counts for other agent tasks
POST /api/units                     create a unit {title, kind, tags, due, parent_id}
POST /api/unit/<id>                 update {status, title, add_tags, remove_tags, parent_id}
POST /api/chat                      {"messages": [{role, content}]} -> assistant reply + changes
POST /api/chat/stream               same input -> Server-Sent Events (delta/status/done) for live output
POST /api/done | /api/teach | /api/poll
GET/POST /login, POST /logout     account login (see apps/auth.py); required once a user exists

When hub.viewer.enabled is true, a separate listener serves an anonymous, read-only
Explorer snapshot at hub.viewer.bind:hub.viewer.port. It has no API or login route.
"""
from __future__ import annotations

import html
import json
import re
import ssl
import threading
import time
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from ..backup import BackupManager
from ..organize import agent_bridge, ai_organizer
from ..store import Store
from ..sync import policy as policy_mod
from ..organize import units as U
from . import auth
from . import brief as brief_mod
from . import scheduler as scheduler_mod

WEB = Path(__file__).resolve().parent / "web"
_AI_KICK_LOCK = threading.Lock()


def _kick_ai(cfg) -> bool:
    """Wake a short-lived AI worker after an explicit flag/organize request.

    The regular service loop remains the fallback. A separate Store connection keeps the HTTP
    request responsive and prevents two manual kicks from processing the same queue concurrently.
    """
    if not cfg.get("ai", {}).get("enabled") or not _AI_KICK_LOCK.acquire(blocking=False):
        return False
    def worker():
        s = Store(cfg["hub"]["database"])
        try:
            ai_organizer.run(cfg, s)
        except Exception:
            pass
        finally:
            s.db.close()
            _AI_KICK_LOCK.release()
    threading.Thread(target=worker, name="infohub-ai-kick", daemon=True).start()
    return True


def _queue_segments(store, segment_ids) -> int:
    """Close and enqueue selected segments after an explicit Watch/Flag action."""
    ids = list(dict.fromkeys(int(sid) for sid in segment_ids if sid is not None))
    if not ids:
        return 0
    now = int(time.time())
    for sid in ids:
        store.q("UPDATE segments SET closed=1, policy='watch', rule_done=0, ai_done=0, updated_at=? WHERE id=?", (now, sid))
        store.q("""INSERT INTO jobs(kind, ref_id, status, created_at, updated_at, not_before)
                  VALUES('segment',?,'pending',?,?,0)
                  ON CONFLICT(kind, ref_id) DO UPDATE SET status='pending', not_before=0,
                  last_error=NULL, updated_at=excluded.updated_at""", (sid, now, now))
    return len(ids)


def viewer_payload(store) -> dict:
    """Build the read-only Explorer snapshot embedded in the viewer HTML."""
    units = U.query(store, status="all", order="activity", limit=1000, mine=False)
    return {"tags": U.tag_counts(store, limit=400), "units": units,
            "graph": graph(store, status="all", limit=1000, order="activity", include_context=False),
            "views": []}


def viewer_page(store) -> bytes:
    """Serve one self-contained page; the browser has no API endpoint to call."""
    payload = json.dumps(viewer_payload(store), ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    source = (WEB / "index.html").read_text(encoding="utf-8")
    source = source.replace("<title>Info Hub</title>", "<title>Info Hub Viewer</title>", 1)
    source = source.replace("<script>", f"<script>window.__INFOHUB_VIEWER__={payload};</script><script>", 1)
    return source.encode("utf-8")


def serve_viewer(cfg, store):
    """Anonymous read-only listener. It exposes only GET / and no /api routes."""
    vcfg = cfg["hub"].get("viewer") or {}
    bind, port = vcfg.get("bind", "127.0.0.1"), int(vcfg.get("port", 8766))
    lock = threading.Lock()
    cached, cached_at = {"body": b"", "at": 0}, 0

    class Viewer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal cached_at
            if urlparse(self.path).path not in ("/", "/index.html"):
                self.send_error(404)
                return
            now = time.time()
            with lock:
                if now - cached_at >= 5 or not cached["body"]:
                    cached["body"], cached_at = viewer_page(store), now
                body = cached["body"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=5")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((bind, port), Viewer)
    print(f"viewer: http://{bind}:{port}/  (read-only, no login, no API)", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _rows(cur):
    return [dict(r) for r in cur]


def summary(store, hours: float, query: str = "") -> dict:
    since = int(time.time() - hours * 3600) if hours > 0 else 0
    q = store.q
    needle = (query or "").strip()
    text_filter = (" HAVING sum(CASE WHEN text LIKE ? OR channel_name LIKE ? OR sender_name LIKE ? THEN 1 ELSE 0 END) > 0"
                   if needle else "")
    text_args = (f"%{needle}%", f"%{needle}%", f"%{needle}%") if needle else ()
    needs = _rows(q("""SELECT id, source, account, channel, channel_name, sender, sender_name, sent_at, kind, text, link, priority, rule
                       FROM messages WHERE sent_at>=? AND priority IN ('P0','P1') AND done=0
                       ORDER BY priority, sent_at DESC LIMIT 200""", (since,)))
    digest = _rows(q("""SELECT source, account, channel, max(channel_name) AS channel_name, max(is_group) AS is_group,
                         max(policy) AS policy, count(*) AS n, max(sent_at) AS last_at,
                         sum(priority IN ('P0','P1','P2')) AS important
                       FROM messages WHERE sent_at>=? AND policy IN ('watch','digest','store')
                       GROUP BY source, account, channel""" + text_filter + """
                       ORDER BY (max(policy)='watch') DESC, important DESC, n DESC LIMIT 60""", (since, *text_args)))
    for d in digest[:30]:
        d["highlights"] = _rows(q("""SELECT id, sender_name, sent_at, text, priority FROM messages
                                     WHERE source=? AND account=? AND channel=? AND sent_at>=? AND kind NOT IN ('sticker','system')
                                     ORDER BY priority, sent_at DESC LIMIT 4""", (d["source"], d["account"], d["channel"], since)))
        d["units"] = _rows(q("""SELECT DISTINCT u.id, u.kind, u.title, u.status FROM units u JOIN unit_messages um ON um.unit_id=u.id
                                JOIN messages m ON m.id=um.message_id WHERE m.source=? AND m.account=? AND m.channel=?
                                AND m.sent_at>=? AND u.kind NOT IN ('digest') ORDER BY u.last_activity_at DESC LIMIT 6""",
                             (d["source"], d["account"], d["channel"], since)))
    counts = _rows(q("""SELECT source || CASE WHEN account!='' THEN ':'||account ELSE '' END AS source,
                        count(*) AS messages, count(DISTINCT channel) AS chats, sum(priority IN ('P0','P1')) AS needs_you
                        FROM messages WHERE sent_at>=? GROUP BY 1""", (since,)))
    ignored = _rows(q("""SELECT source || CASE WHEN account!='' THEN ':'||account ELSE '' END AS source, count(*) AS chats
                         FROM channels WHERE policy='ignore' GROUP BY 1"""))
    return {"hours": hours, "needs_you": needs, "digest": digest, "counts": counts, "ignored": ignored, "now": int(time.time())}


def channel_messages(store, source: str, account: str, channel: str, hours: float = 72, limit: int = 120) -> dict:
    """Recent messages for one group card; used by the explicit Expand action."""
    since = int(time.time() - hours * 3600) if hours > 0 else 0
    limit = max(1, min(500, int(limit)))
    rows = _rows(store.q("""SELECT id, source, account, channel, channel_name, is_group,
                              sender_name, sent_at, kind, text, link, priority, done
                           FROM messages WHERE source=? AND account=? AND channel=? AND sent_at>=?
                           ORDER BY sent_at DESC, id DESC LIMIT ?""",
                         (source, account or "", channel, since, limit)))
    return {"source": source, "account": account or "", "channel": channel,
            "messages": rows, "count": len(rows), "hours": hours}


def today(store, mine: bool = False) -> dict:
    """Three lanes, all info units except the last: things to do, things to attend,
    and flagged messages the organizers have not turned into a unit yet."""
    now = int(time.time())
    dated = U.query(store, kind=["todo", "question", "decision"], status="open",
                    due_before=now + 7 * 86400, order="due", limit=100, mine=mine)
    seen = {u["id"] for u in dated}
    recent = [u for u in U.query(store, kind=["todo", "question", "decision"], status="open",
                                 since=now - 7 * 86400, limit=80, mine=mine) if u["id"] not in seen]
    from ..sync.triage import actionable, trivial
    rows = _rows(store.q("""SELECT id, source, account, channel, channel_name, sender, sender_name, sent_at,
                             kind, text, link, flags, mentions_me, priority, rule FROM messages
                           WHERE sent_at>=? AND priority IN ('P0','P1') AND done=0
                             AND id NOT IN (SELECT message_id FROM unit_messages)
                           ORDER BY priority, sent_at DESC LIMIT 400""", (now - 3 * 86400,)))
    # Keep only messages that genuinely want attention: a real task, an @mention, or a
    # substantive line from a VIP/watched person - never trivial acks or stickers.
    unsorted_rows = [m for m in rows if not trivial(m) and (actionable(m) or m.get("mentions_me"))][:50]
    for m in unsorted_rows:
        m.pop("flags", None)
    return {"needs": dated + recent,
            "events": [u for u in U.query(store, kind="event", status="open", due_before=now + 7 * 86400, order="due",
                                          limit=100, mine=mine) if (u["start_at"] or 0) >= now - 3600],
            "unsorted": unsorted_rows,
            "scope": "mine" if mine else "all",
            "counts": {"mine": store.q("SELECT count(*) FROM units WHERE for_me=1 AND status='open'").fetchone()[0],
                       "all": store.q("SELECT count(*) FROM units WHERE status='open' AND kind!='digest'").fetchone()[0]},
            "now": now}


def live(store) -> dict:
    """Watched chats with an open (in-progress) segment and its recent messages — visible before
    the AI organizes them into units."""
    segs = _rows(store.q("""SELECT s.id, s.source, s.account, s.channel, s.channel_name, s.n, s.start_at, s.end_at
                            FROM segments s WHERE s.closed=0 AND s.policy='watch' ORDER BY s.end_at DESC LIMIT 40"""))
    for s in segs:
        s["messages"] = _rows(store.q("""SELECT id, sender_name, sent_at, text, kind FROM messages
            WHERE segment_id=? AND kind NOT IN ('sticker','system') ORDER BY sent_at DESC LIMIT 12""", (s["id"],)))
        s["messages"].reverse()
    return {"segments": segs, "now": int(time.time())}


def _gnode(u, seed: bool) -> dict:
    tags = u.get("tags", []) or []
    return {"id": u["id"], "kind": u["kind"], "title": u["title"], "status": u["status"],
            "for_me": u.get("for_me"), "due_at": u.get("due_at"),
            "first_activity_at": u.get("first_activity_at"), "last_evidence_at": u.get("last_evidence_at"),
            "at": u.get("last_activity_at") or u.get("updated_at"),
            "n_messages": u.get("n_messages"), "n_direct_messages": u.get("n_direct_messages"),
            "n_children": u.get("n_children"), "n_descendants": u.get("n_descendants"),
            "tags": [t for t in tags if not t.startswith("source:")],
            "source": next((t.split(":", 1)[1] for t in tags if t.startswith("source:")), None),
            "seed": seed}


def unit_query_params(qs, now=None):
    """One filter/order contract for List and Graph."""
    days = float(qs["days"]) if qs.get("days") else None
    return {"tags": [t for t in qs.get("tags", "").split(",") if t],
            "kind": [k for k in qs.get("kind", "").split(",") if k] or None,
            "status": qs.get("status") or None, "q": qs.get("q") or None,
            "since": (time.time() if now is None else now) - days * 86400 if days else None,
            "roots_only": qs.get("roots") == "1", "order": qs.get("order", "activity"),
            "limit": max(1, min(1000, int(qs.get("limit", 400)))), "mine": qs.get("mine") == "1"}


def graph(store, tags=(), kind=None, status=None, q=None, since=None, mine=False, limit=300,
          order="activity", roots_only=False, include_context=False) -> dict:
    """Strict matching nodes by default; optional one-hop context is explicitly marked."""
    seed = U.query(store, tags, kind=kind, status=status, q=q, since=since, order=order,
                   roots_only=roots_only, limit=limit, mine=mine)
    nodes = {u["id"]: _gnode(u, seed=True) for u in seed}
    if include_context and nodes:
        ids = list(nodes)
        ph = ",".join("?" for _ in ids)
        rows = store.q(f"SELECT u.* FROM units u WHERE u.status!='dropped' AND u.id NOT IN ({ph}) AND u.id IN ("
                       f"SELECT parent_id FROM unit_links WHERE child_id IN ({ph}) UNION "
                       f"SELECT child_id FROM unit_links WHERE parent_id IN ({ph})) "
                       "ORDER BY u.last_activity_at DESC, u.id DESC LIMIT ?", (*ids, *ids, *ids, int(limit))).fetchall()
        for u in U._rows(store, rows):
            nodes[u["id"]] = _gnode(u, seed=False)
    edges = []
    if nodes:
        ids = list(nodes)
        ph = ",".join("?" for _ in ids)
        edges = [dict(row) for row in store.q(
            f"SELECT parent_id AS source, child_id AS target, relation FROM unit_links "
            f"WHERE parent_id IN ({ph}) AND child_id IN ({ph}) ORDER BY parent_id, child_id, relation", (*ids, *ids))]
    return {"nodes": list(nodes.values()), "edges": edges, "matched_count": len(seed),
            "context_count": len(nodes) - len(seed), "now": int(time.time())}


def organize_now(cfg, store, channel: str | None = None) -> dict:
    """Close the open segment(s) now (optionally for one chat) and organize them immediately."""
    from ..organize import ai_organizer, polish, rule_organizer
    if channel:
        store.q("UPDATE segments SET closed=1, updated_at=? WHERE closed=0 AND channel=?", (int(time.time()), channel))
    else:
        store.q("UPDATE segments SET closed=1, updated_at=? WHERE closed=0 AND policy='watch'", (int(time.time()),))
    store.db.commit()
    rules = rule_organizer.run(cfg, store)
    ai = ai_organizer.run(cfg, store)
    polish.run(cfg, store)
    return {"rules": {k: rules.get(k) for k in ("units", "queued_for_ai")}, "ai": ai}


def channels(store, days: float = 30) -> list[dict]:
    since = int(time.time() - days * 86400)
    return _rows(store.q("""SELECT c.source, c.account, c.channel, c.channel_name, c.is_group, c.policy, c.unread,
                              c.muted, c.chat_mode, c.last_at,
                              (SELECT count(*) FROM messages m WHERE m.source=c.source AND m.account=c.account
                                 AND m.channel=c.channel AND m.sent_at>=?) AS recent,
                              (SELECT count(*) FROM messages m WHERE m.source=c.source AND m.account=c.account
                                 AND m.channel=c.channel) AS total
                            FROM channels c ORDER BY recent DESC, total DESC LIMIT 400""", (since,)))


def status(store, cfg=None, limit: int = 120) -> dict:
    q = store.q
    now = int(time.time())
    day = now - 86400
    cfg = cfg or {}
    runs = _rows(q("""SELECT layer, source, status, stored, error, started_at, finished_at,
                        (finished_at - started_at) AS seconds, detail FROM polls
                      ORDER BY finished_at DESC, id DESC LIMIT ?""", (limit,)))
    latest = _rows(q("""SELECT layer, source, status, stored, error, finished_at FROM polls
                        WHERE id IN (SELECT max(id) FROM polls GROUP BY layer, source) ORDER BY layer, source"""))
    health = {"ok": sum(r["status"] == "ok" for r in latest), "waiting": sum(r["status"] == "waiting" for r in latest),
              "error": sum(r["status"] == "error" for r in latest)}
    failing = [r for r in latest if r["status"] != "ok"]
    ai = (cfg.get("ai") or {})
    return {"now": now, "health": health, "failing": failing, "runs": runs, "latest": latest,
            "jobs": {r["status"]: r["n"] for r in q("SELECT status, count(*) AS n FROM jobs GROUP BY status")},
            "ai": {"enabled": bool(ai.get("enabled")), "provider": ai.get("provider"), "model": ai.get("model"),
                   "spent_today_usd": round(ai_organizer.spent_today(store), 4),
                   "budget_usd": ai.get("daily_budget_usd"),
                   "usage": _rows(q("""SELECT day, model, requests, input_tokens, output_tokens, cache_read_tokens,
                                         round(cost_usd, 4) AS cost_usd FROM llm_usage ORDER BY day DESC LIMIT 7"""))},
            "units": {r["kind"]: r["n"] for r in q("SELECT kind, count(*) AS n FROM units WHERE status!='dropped' GROUP BY kind")},
            "totals": {
                "messages": q("SELECT count(*) FROM messages").fetchone()[0],
                "messages_24h": q("SELECT count(*) FROM messages WHERE sent_at>=?", (day,)).fetchone()[0],
                "units": q("SELECT count(*) FROM units WHERE status!='dropped'").fetchone()[0],
                "segments_open": q("SELECT count(*) FROM segments WHERE closed=0").fetchone()[0],
                "channels": {r[0]: r[1] for r in q("SELECT policy, count(*) FROM channels GROUP BY policy")},
                "sources": {r[0]: r[1] for r in q("""SELECT source || CASE WHEN account!='' THEN ':'||account ELSE '' END,
                                                      count(*) FROM messages WHERE sent_at>=? GROUP BY 1""", (day,))}}}


def search(store, text: str, limit=100) -> list[dict]:
    if len(text) >= 3:
        cur = store.q("""SELECT m.id, m.source, m.account, m.channel_name, m.sender_name, m.sent_at, m.text, m.priority, m.link
            FROM messages_fts f JOIN messages m ON m.id=f.rowid WHERE messages_fts MATCH ? ORDER BY m.sent_at DESC LIMIT ?""",
                      ('"' + text.replace('"', '""') + '"', limit))
    else:
        cur = store.q("""SELECT id, source, account, channel_name, sender_name, sent_at, text, priority, link FROM messages
            WHERE text LIKE ? OR channel_name LIKE ? ORDER BY sent_at DESC LIMIT ?""", (f"%{text}%", f"%{text}%", limit))
    return _rows(cur)


def search_all(store, text: str, limit: int = 60) -> dict:
    """One query box over the whole hub: info units, raw messages, people, chats and tags."""
    text = (text or "").strip()
    like = f"%{text}%"
    q = store.q
    people = _rows(q("""SELECT source, sender, max(sender_name) AS sender_name, count(*) AS n, max(sent_at) AS last_at
                        FROM messages WHERE sender IS NOT NULL AND sender_name LIKE ?
                        GROUP BY source, sender ORDER BY n DESC LIMIT ?""", (like, limit)))
    channels = _rows(q("""SELECT source, account, channel, channel_name, policy, is_group,
                            (SELECT count(*) FROM messages m WHERE m.source=c.source AND m.account=c.account
                               AND m.channel=c.channel) AS n
                          FROM channels c WHERE channel_name LIKE ? ORDER BY n DESC LIMIT ?""", (like, limit)))
    tags = _rows(q("""SELECT t.tag, count(*) AS n FROM unit_tags t JOIN units u ON u.id=t.unit_id
                      WHERE t.tag LIKE ? AND u.status!='dropped' GROUP BY t.tag ORDER BY n DESC LIMIT ?""", (like, limit)))
    return {"q": text, "units": U.query(store, q=text, limit=limit), "messages": search(store, text, limit),
            "people": people, "channels": channels, "tags": tags}


def serve(cfg, store, poll_fn):
    lock = threading.Lock()
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    d, acfg = cfg["hub"]["dashboard"], cfg["hub"]["auth"]
    hosts = {f"{h}:{d['port']}" for h in ("127.0.0.1", "localhost", d["bind"])} | set(d.get("allowed_hosts") or [])
    tls = bool(d.get("tls_cert") and d.get("tls_key"))
    secure_cookie = tls or bool(acfg.get("cookie_secure"))
    sessions = auth.Sessions(store, acfg["session_days"])
    limiter = auth.RateLimiter()
    if d["bind"] not in ("127.0.0.1", "localhost", "::1") and not auth.required(cfg):
        raise SystemExit("Refusing to listen on a non-loopback address without login: run "
                         "`python -m infohub user add <name>` first (or set hub.auth.mode).")

    backups = BackupManager(cfg)

    class H(BaseHTTPRequestHandler):
        user = None

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", headers=()):
            data = body if isinstance(body, bytes) else (body.encode("utf-8") if isinstance(body, str)
                                                         else json.dumps(body, ensure_ascii=False).encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, to, headers=()):
            self.send_response(303)
            self.send_header("Location", to)
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()

        def _client(self) -> str:
            if acfg.get("trust_forwarded_for") and self.headers.get("X-Forwarded-For"):
                return self.headers["X-Forwarded-For"].split(",")[0].strip()
            return self.client_address[0]

        def _cookie(self) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == auth.COOKIE:
                    return v
            return None

        def _set_cookie(self, value: str, max_age: int) -> tuple:
            attrs = f"{auth.COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            return ("Set-Cookie", attrs + ("; Secure" if secure_cookie else ""))

        def _guard(self) -> bool:
            if "*" not in hosts and self.headers.get("Host") not in hosts:   # DNS-rebinding protection
                self._send(403, {"error": "bad host"})
                return False
            path = urlparse(self.path).path
            if path in ("/login", "/api/health") or not auth.required(cfg):
                return True
            bearer = (self.headers.get("Authorization") or "")
            if bearer.startswith("Bearer "):
                self.user = auth.check_token(cfg, bearer[7:].strip())
            else:
                with lock:
                    self.user = sessions.user(self._cookie() or "")
            if self.user:
                return True
            if path.startswith("/api/"):
                self._send(401, {"error": "login required"})
            else:
                self._redirect("/login?next=" + quote(self.path, safe=""))
            return False

        def _login_page(self, error="", nxt="/", code=200):
            page = auth.LOGIN_PAGE.replace("__NEXT__", html.escape(nxt, quote=True)).replace("__ERROR__", html.escape(error))
            self._send(code, page, "text/html; charset=utf-8")

        def do_GET(self):
            if not self._guard():
                return
            u = urlparse(self.path)
            if u.path == "/login":
                nxt = parse_qs(u.query).get("next", ["/"])[0]
                return self._login_page(nxt=nxt)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            tags = [t for t in qs.get("tags", "").split(",") if t]
            with lock:
                if u.path in ("/", "/index.html"):
                    return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/summary":
                    return self._send(200, summary(store, float(qs.get("hours", 24)), qs.get("q", "")))
                if u.path == "/api/channel/messages":
                    source, channel = qs.get("source", ""), qs.get("channel", "")
                    if not source or not channel:
                        return self._send(400, {"error": "source and channel required"})
                    return self._send(200, channel_messages(store, source, qs.get("account", ""),
                                                            channel, float(qs.get("hours", 72)),
                                                            int(qs.get("limit", 120))))
                if u.path == "/api/today":
                    scope = qs.get("scope") or cfg["apps"].get("today_scope", "mine")
                    return self._send(200, today(store, mine=scope == "mine"))
                if u.path == "/api/live":
                    return self._send(200, live(store))
                if u.path == "/api/channels":
                    return self._send(200, channels(store, float(qs.get("days", 30))))
                if u.path == "/api/channels/defaults":
                    return self._send(200, policy_mod.apply(store, cfg, qs.get("source"), dry_run=True))
                if u.path == "/api/people":
                    vip = store.overrides().get("vip", set())
                    needle = (qs.get("q") or "").strip()
                    where = "WHERE sender IS NOT NULL AND sender_name IS NOT NULL"
                    args = []
                    if needle:
                        where += " AND sender_name LIKE ?"; args.append(f"%{needle}%")
                    rows = _rows(store.q(f"""SELECT source, sender, max(sender_name) AS sender_name, count(*) AS n
                                            FROM messages {where} GROUP BY source, sender
                                            ORDER BY n DESC LIMIT ?""", (*args, int(qs.get("limit", 40)))))
                    watched = store.overrides().get("watch_person", set()) | store.overrides().get("watch", set())
                    for r in rows:
                        r["vip"] = (r["source"], r["sender"]) in vip
                        r["watch"] = ((r["source"], r["sender"]) in watched or
                                      (r["source"], r["sender_name"]) in watched)
                    vrows = _rows(store.q("SELECT source, value AS sender, label, "
                                          "(SELECT max(sender_name) FROM messages m WHERE m.source=overrides.source AND m.sender=overrides.value) AS sender_name "
                                          "FROM overrides WHERE kind='vip'"))
                    wrows = _rows(store.q("SELECT source, value AS sender, label FROM overrides WHERE kind='watch_person'"))
                    return self._send(200, {"people": rows, "vips": vrows, "watchers": wrows})
                if u.path == "/api/backups":
                    return self._send(200, backups.status())
                if u.path == "/api/settings":
                    from .. import config as config_mod
                    saved = config_mod.read_settings()
                    current = {}
                    for key in config_mod.EDITABLE:
                        node = cfg
                        for part in key.split("."):
                            node = node.get(part) if isinstance(node, dict) else None
                        current[key] = saved[key] if key in saved else node
                    return self._send(200, {"editable": config_mod.EDITABLE, "values": current,
                                            "saved": saved,
                                            "restart_needed_for": ["hub.poll_minutes", "ai.interval_minutes"]})
                if u.path == "/api/schedules":
                    return self._send(200, store.schedules())
                if u.path == "/api/units":
                    return self._send(200, U.query(store, **unit_query_params(qs)))
                if u.path == "/api/graph":
                    return self._send(200, graph(store, **unit_query_params(qs), include_context=qs.get("context") == "1"))
                if m := re.fullmatch(r"/api/unit/(\d+)", u.path):
                    unit = U.get(store, int(m[1]), depth=int(qs.get("depth", 2)))
                    return self._send(200 if unit else 404, unit or {"error": "not found"})
                if u.path == "/api/tags":
                    days = float(qs["days"]) if qs.get("days") else None
                    return self._send(200, U.tag_counts(store, since=time.time() - days * 86400 if days else None,
                                                        prefix=qs.get("prefix")))
                if u.path == "/api/views":
                    return self._send(200, cfg["apps"]["views"])
                if u.path == "/api/brief":
                    return self._send(200, brief_mod.brief(cfg, store, int(qs.get("days", 7)), tags), "text/markdown; charset=utf-8")
                if u.path == "/api/search":
                    return self._send(200, search_all(store, qs.get("q", "")))
                if u.path == "/api/status":
                    return self._send(200, status(store, cfg, int(qs.get("limit", 120))))
                if u.path == "/api/agent/next":
                    return self._send(200, agent_bridge.lease(cfg, store, int(qs["limit"]) if qs.get("limit") else None))
                if u.path == "/api/agent/context":
                    return self._send(200, {"brief": brief_mod.brief(cfg, store, int(qs.get("days", 7)), tags),
                                            "status": status(store, cfg, 20)})
                if u.path == "/api/health":
                    return self._send(200, {"ok": True, "app": "infohub"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            origin = self.headers.get("Origin")
            if origin and origin != "null" and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, {"error": "cross-origin"})          # same-origin writes only
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20_000_000:
                return self._send(413, {"error": "too large"})
            raw = self.rfile.read(length)
            path = urlparse(self.path).path
            if path == "/login":
                form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
                nxt = form.get("next") or "/"
                if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                    nxt = "/"                                        # no open redirects
                client = self._client()
                if limiter.blocked(client):
                    return self._login_page("Too many attempts. Try again in 15 minutes.", nxt, 429)
                user = (form.get("username") or "").strip()
                if not auth.verify_password(cfg, user, form.get("password") or ""):
                    limiter.fail(client)
                    return self._login_page("Wrong account or password.", nxt, 401)
                limiter.reset(client)
                with lock:
                    token = sessions.create(user, client)
                return self._redirect(nxt, [self._set_cookie(token, int(acfg["session_days"]) * 86400)])
            if path == "/logout":
                with lock:
                    sessions.delete(self._cookie() or "")
                return self._redirect("/login", [self._set_cookie("", 0)])
            if urlparse(self.path).path == "/api/agent/submit":
                with lock:
                    return self._send(200, agent_bridge.submit(cfg, store, raw.decode("utf-8", "replace")))
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            p = urlparse(self.path).path
            if p == "/api/chat/stream":                 # Server-Sent Events: stream outside the global lock
                from . import chat as chat_mod
                self.send_response(200)
                for k, v in (("Content-Type", "text/event-stream; charset=utf-8"), ("Cache-Control", "no-store, no-transform"),
                             ("X-Content-Type-Options", "nosniff"), ("X-Accel-Buffering", "no"), ("Connection", "close")):
                    self.send_header(k, v)
                self.end_headers()
                try:
                    for ev in chat_mod.chat_stream(cfg, store, lock, body.get("messages") or []):
                        self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionError):
                    pass
                return
            with lock:
                if p == "/api/done":
                    store.mark_done(int(body["id"]), bool(body.get("done", True)))
                elif p == "/api/teach":
                    if body.get("kind") not in ("mute", "watch", "watch_person", "digest", "vip"):
                        return self._send(400, {"error": "bad kind"})
                    queued = 0
                    if body.get("undo"):
                        store.remove_override(body["kind"], body["source"], body["value"])
                    else:
                        store.add_override(body["kind"], body["source"], body["value"], body.get("label", ""))
                        if body["kind"] in ("mute", "watch", "digest"):
                            pol = {"mute": "ignore", "watch": "watch", "digest": "digest"}[body["kind"]]
                            store.q("UPDATE channels SET policy=? WHERE source=? AND channel=?", (pol, body["source"], body["value"]))
                            store.q("UPDATE messages SET policy=? WHERE source=? AND channel=?", (pol, body["source"], body["value"]))
                            if body["kind"] == "watch":
                                segment_rows = store.q("SELECT DISTINCT segment_id FROM messages WHERE source=? AND channel=? AND segment_id IS NOT NULL",
                                                        (body["source"], body["value"])).fetchall()
                                queued = _queue_segments(store, [r["segment_id"] for r in segment_rows])
                            if pol == "ignore":
                                store.q("UPDATE messages SET done=1 WHERE source=? AND channel=?", (body["source"], body["value"]))
                        elif body["kind"] == "watch_person":
                            label = (body.get("label") or "").strip()
                            rows = store.q("SELECT DISTINCT segment_id FROM messages WHERE source=? AND "
                                            "(sender=? OR (? <> '' AND sender_name=?))",
                                            (body["source"], body["value"], label, label)).fetchall()
                            store.q("UPDATE messages SET policy='watch' WHERE source=? AND "
                                    "(sender=? OR (? <> '' AND sender_name=?))",
                                    (body["source"], body["value"], label, label))
                            segment_ids = [r["segment_id"] for r in rows if r["segment_id"] is not None]
                            if segment_ids:
                                queued = _queue_segments(store, segment_ids)
                    store.db.commit()
                    return self._send(200, {"ok": True, "queued": queued,
                                            "ai_started": bool(queued and _kick_ai(cfg))})
                elif p == "/api/units":
                    src_msg = None
                    if body.get("from_message"):
                        src_msg = store.q("SELECT * FROM messages WHERE id=?", (int(body["from_message"]),)).fetchone()
                        if not src_msg:
                            return self._send(404, {"error": "message not found"})
                        body.setdefault("title", (src_msg["text"] or "")[:120])
                        body.setdefault("kind", "todo")
                    if not (body.get("title") or "").strip():
                        return self._send(400, {"error": "title required"})
                    uid = U.upsert(store, None, kind=body.get("kind") if body.get("kind") in U.KINDS else "note",
                                   title=body["title"].strip()[:200], summary=body.get("summary"),
                                   due_at=U.iso_to_epoch(body.get("due"), tz), start_at=U.iso_to_epoch(body.get("start"), tz),
                                   origin=body.get("origin") if body.get("origin") in ("user", "agent") else "user")
                    U.add_tags(store, uid, [f"type:{body.get('kind') or 'note'}", *(body.get("tags") or [])])
                    if src_msg is not None:
                        U.attach(store, uid, [src_msg["id"]])
                        U.add_tags(store, uid, [f"channel:{src_msg['channel_name']}"]
                                   + ([f"person:{src_msg['sender_name']}"] if src_msg["sender_name"] else []))
                        U.mark_for_me(store, uid, cfg.get("me", {}).get("names", []))
                        store.q("UPDATE units SET polished=0 WHERE id=?", (uid,))   # let the AI shorten the title
                    if body.get("parent_id"):
                        U.link(store, int(body["parent_id"]), uid)
                    store.db.commit()
                    return self._send(200, {"ok": True, "id": uid})
                elif m := re.fullmatch(r"/api/unit/(\d+)", p):
                    uid = int(m[1])
                    if not store.q("SELECT 1 FROM units WHERE id=?", (uid,)).fetchone():
                        return self._send(404, {"error": "not found"})
                    fields = {k: body[k] for k in ("status", "title", "summary") if body.get(k)}
                    if fields.get("status") not in (None, "open", "done", "dropped"):
                        return self._send(400, {"error": "bad status"})
                    # `type:<kind>` is reserved: adding one means "reclassify this unit", not a literal tag.
                    add = [t for t in (body.get("add_tags") or []) if isinstance(t, str)]
                    kind = body.get("kind")
                    type_kinds = [t.split(":", 1)[1].strip().lower() for t in add
                                  if t.lower().startswith("type:") and t.split(":", 1)[1].strip().lower() in U.KINDS]
                    if type_kinds and not kind:
                        kind = type_kinds[-1]
                    add = [t for t in add if not t.lower().startswith("type:")]   # type tags are managed via kind
                    if kind:
                        if kind not in U.KINDS:
                            return self._send(400, {"error": "bad kind"})
                        fields["kind"] = kind
                        store.q("DELETE FROM unit_tags WHERE unit_id=? AND tag LIKE 'type:%'", (uid,))  # exactly one type tag
                        U.add_tags(store, uid, [f"type:{kind}"])
                    U.update(store, uid, **fields)
                    U.add_tags(store, uid, add)
                    U.remove_tags(store, uid, body.get("remove_tags"))
                    if body.get("parent_id") and not U.link(store, int(body["parent_id"]), uid):
                        return self._send(400, {"error": "link would create a cycle"})
                    store.db.commit()
                elif p == "/api/channels/defaults":
                    res = policy_mod.apply(store, cfg, body.get("source"), dry_run=not body.get("apply"))
                    return self._send(200, res)
                elif p == "/api/channels/batch":
                    want = body.get("policy")
                    if want not in ("watch", "store", "digest", "ignore"):
                        return self._send(400, {"error": "bad policy"})
                    kind = {"watch": "watch", "digest": "digest", "ignore": "mute"}.get(want)
                    n = 0
                    for ch in body.get("chats") or []:
                        src, cid = ch.get("source"), ch.get("channel")
                        if not src or not cid:
                            continue
                        for k in ("watch", "digest", "mute"):
                            store.remove_override(k, src, cid)
                        if kind:
                            store.add_override(kind, src, cid, ch.get("channel_name", ""))
                        store.q("UPDATE channels SET policy=? WHERE source=? AND channel=?", (want, src, cid))
                        store.q("UPDATE messages SET policy=? WHERE source=? AND channel=?", (want, src, cid))
                        n += 1
                    store.db.commit()
                    return self._send(200, {"ok": True, "updated": n, "policy": want})
                elif p == "/api/channels/flag":
                    source, account, channel = body.get("source"), body.get("account", ""), body.get("channel")
                    if not source or not channel:
                        return self._send(400, {"error": "source and channel required"})
                    hours = float(body.get("hours", 0))
                    since = int(time.time() - hours * 3600) if hours > 0 else 0
                    cur = store.q("""UPDATE messages SET priority='P1', done=0
                                     WHERE source=? AND account=? AND channel=? AND sent_at>=?""",
                                  (source, account, channel, since))
                    # Flagging is an explicit request to organize these messages. Reopen their
                    # segments, reset any previous AI completion, and enqueue them immediately.
                    segs = [r[0] for r in store.q("SELECT DISTINCT segment_id FROM messages WHERE source=? AND account=? AND channel=? AND sent_at>=? AND segment_id IS NOT NULL",
                                                   (source, account, channel, since))]
                    queued = _queue_segments(store, segs)
                    store.db.commit()
                    return self._send(200, {"ok": True, "flagged": cur.rowcount,
                                            "queued": queued, "ai_started": _kick_ai(cfg),
                                            "source": source, "channel": channel, "hours": hours})
                elif p == "/api/backups":
                    queued = backups.request()
                    return self._send(202, {"queued": queued, **backups.status()})
                elif p == "/api/settings":
                    from .. import config as config_mod
                    try:
                        saved = config_mod.write_settings(body)
                    except ValueError as exc:
                        return self._send(400, {"error": str(exc)})
                    changes = {key.removeprefix("hub.backup."): value for key, value in body.items()
                               if key.startswith("hub.backup.")}
                    if changes:
                        cfg["hub"].setdefault("backup", {}).update(changes)
                        backups.configure(changes)
                    # Keep the long-running process in sync with settings.local.json.
                    # Otherwise a panel reload reads the old startup cfg until restart.
                    for key, value in body.items():
                        node = cfg
                        parts = key.split(".")
                        for part in parts[:-1]:
                            node = node.setdefault(part, {})
                        node[parts[-1]] = value
                    return self._send(200, {"ok": True, "saved": saved,
                                            "note": "Backup settings apply immediately; restart for sync/AI interval changes."})
                elif p == "/api/schedules":
                    try:
                        if body.get("delete"):
                            ok = store.delete_schedule(int(body["delete"]))
                            return self._send(200, {"ok": ok})
                        tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
                        out = scheduler_mod.save(store, body, sid=int(body["id"]) if body.get("id") else None, tz=tz)
                    except (ValueError, TypeError) as exc:
                        return self._send(400, {"error": str(exc)})
                    return self._send(200, out)
                elif p == "/api/organize-now":
                    return self._send(200, organize_now(cfg, store, body.get("channel")))
                elif p == "/api/chat":
                    from . import chat as chat_mod
                    return self._send(200, chat_mod.chat(cfg, store, body.get("messages") or []))
                elif p == "/api/poll":
                    result = poll_fn()
                    result["ai_started"] = _kick_ai(cfg)
                    return self._send(200, result)
                else:
                    return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})

    httpd = ThreadingHTTPServer((d["bind"], int(d["port"])), H)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(d["tls_cert"], d["tls_key"])
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"dashboard: {'https' if tls else 'http'}://{d['bind']}:{d['port']}/  (login {'on' if auth.required(cfg) else 'off'})", flush=True)
    backups.start()
    try:
        httpd.serve_forever()
    finally:
        backups.stop()
        httpd.server_close()
