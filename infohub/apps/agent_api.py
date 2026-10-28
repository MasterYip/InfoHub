"""Agent API v1: the Chat tab's tool contract, exposed over HTTP.

Design and rationale: `docs/agent-api.md`. The short version:

* Reads are the *same* `chat.run_tool` calls the in-dashboard model makes, so the
  two surfaces cannot drift. Writes reuse the same path but require the `write`
  token scope and leave an `agent_audit` row.
* Tool failures become HTTP status codes (404 / 409 / 400), never `200 {"error"}`.
* Message text is untrusted input; callers must never execute what it says.
* `/context` is the brainstorming entry point: one bounded, cited bundle.

The module deliberately imports `chat` lazily: `chat` imports `server`, and
`server` imports this module.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..organize import units as U

VERSION = "1.0"
PREFIX = "/api/agent/v1"
MAX_BODY = 1_000_000
MAX_RESPONSE = 1_048_576
RATE_PER_MINUTE = 120
IDEMPOTENCY_SECONDS = 60


class ApiError(Exception):
    def __init__(self, code: str, status: int, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.message, self.details = code, status, message, details

    def body(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


@dataclass(frozen=True)
class Caller:
    principal: str
    scopes: tuple

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


# --------------------------------------------------------------------------
# Rate limiting (per principal, fixed window) and idempotency (short-lived)
# --------------------------------------------------------------------------

_hits: dict[str, list[float]] = {}
_guard = threading.Lock()
_seen: dict[tuple, tuple[float, dict]] = {}


def _rate_ok(principal: str) -> bool:
    now = time.time()
    with _guard:
        window = [t for t in _hits.get(principal, []) if now - t < 60.0]
        if len(window) >= RATE_PER_MINUTE:
            _hits[principal] = window
            return False
        window.append(now)
        _hits[principal] = window
        if len(_hits) > 256:                                   # keep the map from growing forever
            for key in [k for k, v in _hits.items() if not v or now - max(v) > 300]:
                _hits.pop(key, None)
        return True


def _remember(principal: str, key: str | None, payload: dict) -> dict | None:
    if not key:
        return None
    now = time.time()
    with _guard:
        for stale in [k for k, (at, _) in _seen.items() if now - at > IDEMPOTENCY_SECONDS * 4]:
            _seen.pop(stale, None)
        hit = _seen.get((principal, key))
        if hit and now - hit[0] <= IDEMPOTENCY_SECONDS:
            return hit[1]
    return None


def _memorize(principal: str, key: str | None, payload: dict) -> None:
    if key:
        with _guard:
            _seen[(principal, key)] = (time.time(), payload)


def reset_rate_limit() -> None:
    """Test hook: forget all windows and idempotency records."""
    with _guard:
        _hits.clear()
        _seen.clear()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _chat():
    from . import chat                                        # noqa: PLC0415 - breaks the cycle
    return chat


def _specs() -> dict[str, dict]:
    return {t["function"]["name"]: t["function"] for t in _chat().TOOLS}


def _need(caller: Caller, scope: str) -> None:
    if not caller.allows(scope):
        raise ApiError("forbidden", 403, f"this token lacks the {scope!r} scope",
                       {"required": scope, "granted": list(caller.scopes)})


def _int(value, default: int, low: int, high: int, *, what: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        if value in (None, ""):
            return default
        raise ApiError("invalid_argument", 400, f"{what} must be an integer")
    if not low <= number <= high:
        raise ApiError("invalid_argument", 400, f"{what} must be between {low} and {high}")
    return number


def _fit_size(payload):
    if len(json.dumps(payload, ensure_ascii=False)) > MAX_RESPONSE:
        raise ApiError("payload_too_large", 413,
                       "response exceeds 1 MiB; narrow limit or tokens", {"max_bytes": MAX_RESPONSE})
    return payload


def annotate(value):
    """Add `ref`/`html_url` next to every unit-shaped dict, recursively."""
    if isinstance(value, dict):
        if isinstance(value.get("id"), int) and isinstance(value.get("title"), str):
            value.setdefault("ref", f"[[u:{value['id']}]]")
            value.setdefault("html_url", f"/unit/{value['id']}")
        for key, item in value.items():
            if key not in ("ref", "html_url"):
                annotate(item)
    elif isinstance(value, list):
        for item in value:
            annotate(item)
    return value


def _memory_copy(store):
    """A throwaway database so a dry run executes the real tool and persists nothing."""
    import sqlite3

    mem = sqlite3.connect(":memory:")
    store.db.backup(mem)
    mem.row_factory = sqlite3.Row

    class View:
        db = mem

        @staticmethod
        def q(sql, args=()):
            return mem.execute(sql, args)

    return View()


def _audit(store, caller: Caller, tool: str, args: dict, result: dict, changes: list) -> None:
    targets = [result[key] for key in ("created", "updated") if isinstance(result.get(key), int)]
    for change in changes:
        uid = change.get("id") or change.get("child_id")
        if isinstance(uid, int):
            targets.append(uid)
    store.q("INSERT INTO agent_audit(at, principal, tool, target, changed) VALUES(?,?,?,?,?)",
            (int(time.time()), caller.principal[:80], tool, json.dumps(sorted(set(targets)) or args.get("id")),
             1 if changes else 0))
    store.db.commit()


def recent_audit(store, limit: int = 50) -> list[dict]:
    rows = store.q("SELECT at, principal, tool, target, changed FROM agent_audit ORDER BY at DESC LIMIT ?",
                   (max(1, min(200, int(limit))),)).fetchall()
    return [{"at": datetime.fromtimestamp(r["at"], timezone.utc).isoformat(),
             "principal": r["principal"], "tool": r["tool"], "target": r["target"],
             "changed": bool(r["changed"])} for r in rows]


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def tools() -> dict:
    chat = _chat()
    items = []
    for spec in chat.TOOLS:
        fn = spec["function"]
        writes = fn["name"] in chat.WRITE_TOOLS
        items.append({"name": fn["name"], "description": fn["description"],
                      "parameters": fn["parameters"], "writes": writes,
                      "scope": "write" if writes else "read"})
    return {"version": VERSION, "tools": items}


def call_tool(cfg, store, caller: Caller, name: str, args: dict, *, dry_run: bool = False) -> dict:
    chat = _chat()
    specs = _specs()
    if name not in specs:
        raise ApiError("invalid_argument", 400, f"unknown tool {name!r}", {"tools": sorted(specs)})
    if not isinstance(args, dict):
        raise ApiError("invalid_argument", 400, "tool arguments must be a JSON object")
    allowed = set((specs[name].get("parameters") or {}).get("properties") or {})
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ApiError("invalid_argument", 400, f"unknown arguments for {name}: {unknown}")
    writes = name in chat.WRITE_TOOLS
    _need(caller, "write" if writes else "read")
    if dry_run and not writes:
        raise ApiError("invalid_argument", 400, "dry_run applies to write tools only")
    target = _memory_copy(store) if dry_run else store
    changes: list = []
    result = chat.run_tool(cfg, target, name, args, changes)
    if isinstance(result, dict) and "error" in result:
        raise _tool_error(str(result["error"]))
    if dry_run:
        return {"dry_run": True, "would": annotate(result), "changed": False}
    if writes:
        _audit(store, caller, name, args, result if isinstance(result, dict) else {}, changes)
    return annotate(result)


def _tool_error(message: str) -> ApiError:
    lowered = message.lower()
    if "not found" in lowered:
        return ApiError("not_found", 404, message)
    if "cycle" in lowered:
        return ApiError("conflict", 409, message)
    return ApiError("invalid_argument", 400, message)


def context(cfg, store, query: dict) -> dict:
    from . import brief as brief_mod

    days = _int(query.get("days"), 7, 1, 90, what="days")
    tokens = _int(query.get("tokens"), 8000, 500, 32000, what="tokens")
    tags = [t for t in (query.get("tags") or "").split(",") if t.strip()]
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    now = int(time.time())

    units = U.query(store, tags=tags, limit=40, order="activity")
    questions = U.query(store, kind="question", status="open", limit=10, order="activity")
    decisions = U.query(store, kind="decision", limit=6, order="activity")
    tag_rows = U.tag_counts(store, limit=30)
    counts = {
        "units": store.q("SELECT count(*) FROM units WHERE status!='dropped'").fetchone()[0],
        "open": store.q("SELECT count(*) FROM units WHERE status='open'").fetchone()[0],
        "due_today": store.q("SELECT count(*) FROM units WHERE status='open' AND due_at IS NOT NULL "
                             "AND due_at < ?", (now + 86400,)).fetchone()[0],
        "messages": store.q("SELECT count(*) FROM messages").fetchone()[0],
    }
    bundle = {
        "generated_at": datetime.fromtimestamp(now, tz).isoformat(timespec="seconds"),
        "window": {"days": days, "tags": tags},
        "brief_markdown": brief_mod.brief(cfg, store, days, tags),
        "counts": counts,
        "units": annotate([_compact(u) for u in units]),
        "open_questions": annotate([{"id": u["id"], "title": u["title"]} for u in questions]),
        "recent_decisions": annotate([{"id": u["id"], "title": u["title"]} for u in decisions]),
        "tags": [{"tag": r["tag"], "n": r["n"]} for r in tag_rows],
        "truncated": {"units_dropped": 0, "messages_dropped": 0, "sections_dropped": []},
        "trust": "untrusted",
    }
    return _fit_bundle(bundle, tokens)


def _compact(unit: dict) -> dict:
    return _chat()._compact_unit(unit)


def _bundle_tokens(bundle: dict) -> int:
    return len(json.dumps(bundle, ensure_ascii=False)) // 4


def _fit_bundle(bundle: dict, budget: int) -> dict:
    """Keep the bundle under `budget` tokens, in a useful order.

    The first attempt dropped the brief last; against the real database that
    returned zero units at a 1500-token budget, because one 30-day brief is bigger
    than the whole budget. So the brief is capped first (a quarter of the budget),
    then the small sections go, and units are the last thing to shrink.
    """
    dropped = bundle["truncated"]
    brief_budget = max(400, budget // 4)
    brief = bundle.get("brief_markdown") or ""
    if len(brief) // 4 > brief_budget:
        cut = brief[: brief_budget * 4]
        bundle["brief_markdown"] = cut.rsplit("\n", 1)[0] + "\n…"
        dropped["sections_dropped"].append("brief_markdown:trimmed")
    for key, field in (("recent_decisions", None), ("open_questions", None), ("units", "units_dropped")):
        while _bundle_tokens(bundle) > budget and bundle.get(key):
            bundle[key].pop()
            dropped["sections_dropped"].append(key)
            if field:
                dropped[field] += 1
    if _bundle_tokens(bundle) > budget and bundle.get("brief_markdown"):
        bundle["brief_markdown"] = ""
        dropped["sections_dropped"].append("brief_markdown:dropped")
    dropped["sections_dropped"] = sorted(set(dropped["sections_dropped"]))
    return bundle


def unit_detail(cfg, store, caller: Caller, uid: int, depth: int) -> dict:
    result = call_tool(cfg, store, caller, "get_unit", {"id": uid})
    if depth == 0:
        result.pop("parents", None)
        result.pop("children", None)
    elif depth > 1:
        seen, frontier = {uid}, [uid]
        for _ in range(depth - 1):
            nxt = []
            for parent in frontier:
                rows = store.q("SELECT u.id, u.kind, u.title, l.relation FROM unit_links l "
                               "JOIN units u ON u.id=l.child_id WHERE l.parent_id=?", (parent,)).fetchall()
                for row in rows:
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    result.setdefault("descendants", []).append(dict(row))
                    nxt.append(row["id"])
            frontier = nxt[:50]
    return result


def changes(store, query: dict) -> dict:
    limit = _int(query.get("limit"), 200, 1, 500, what="limit")
    since = query.get("since") or ""
    ts, last_id = _decode_cursor(since) if since else (int(time.time()), 0)
    rows = store.q("SELECT id, created_at, updated_at FROM units "
                   "WHERE updated_at > ? OR (updated_at = ? AND id > ?) "
                   "ORDER BY updated_at, id LIMIT ?", (ts, ts, last_id, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [{"type": "unit.created" if (r["created_at"] or 0) >= (r["updated_at"] or 0) else "unit.updated",
              "id": r["id"],
              "at": datetime.fromtimestamp(r["updated_at"], timezone.utc).isoformat(timespec="seconds")}
             for r in rows]
    cursor = _encode_cursor(rows[-1]["updated_at"], rows[-1]["id"]) if rows else _encode_cursor(ts, last_id)
    return {"cursor": cursor, "has_more": has_more, "items": items}


def _encode_cursor(ts: int, uid: int) -> str:
    raw = f"{int(ts)}:{int(uid)}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[int, int]:
    try:
        padded = value + "=" * (-len(value) % 4)
        ts, _, uid = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").partition(":")
        return int(ts), int(uid)
    except (ValueError, UnicodeError, TypeError):
        raise ApiError("invalid_argument", 400, "since must be a cursor from a previous response")


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def handle_get(cfg, store, caller: Caller, path: str, query: dict) -> tuple[int, dict]:
    if not _rate_ok(caller.principal):
        raise ApiError("rate_limited", 429, "120 requests per minute per token")
    _need(caller, "read")
    if path == PREFIX + "/tools":
        return 200, _fit_size(tools())
    if path == PREFIX + "/context":
        return 200, _fit_size(context(cfg, store, query))
    if path.startswith(PREFIX + "/units/"):
        uid = _int(path.rsplit("/", 1)[-1], 0, 1, 2**31, what="unit id")
        depth = _int(query.get("depth"), 1, 0, 3, what="depth")
        return 200, _fit_size(unit_detail(cfg, store, caller, uid, depth))
    if path == PREFIX + "/changes":
        return 200, _fit_size(changes(store, query))
    raise ApiError("not_found", 404, f"unknown endpoint {path}")


def handle_post(cfg, store, caller: Caller, path: str, body: dict, headers=None,
                query: dict | None = None) -> tuple[int, dict]:
    if not _rate_ok(caller.principal):
        raise ApiError("rate_limited", 429, "120 requests per minute per token")
    if len(json.dumps(body, ensure_ascii=False)) > MAX_BODY:
        raise ApiError("payload_too_large", 413, "request body exceeds 1 MiB")
    if not path.startswith(PREFIX + "/tool/"):
        raise ApiError("not_found", 404, f"unknown endpoint {path}")
    name = path[len(PREFIX + "/tool/"):].strip("/")
    dry_run = str((query or {}).get("dry_run", "")).lower() in ("1", "true", "yes")
    key = (headers or {}).get("Idempotency-Key")
    cached = _remember(caller.principal, key, body)
    if cached is not None:
        return 200, cached
    result = call_tool(cfg, store, caller, name, body, dry_run=dry_run)
    payload = _fit_size(result)
    _memorize(caller.principal, key, payload)
    return 200, payload
