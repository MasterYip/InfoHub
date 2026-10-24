"""Layer 2, deterministic organizer (no AI). Always available.

From new messages it creates units for todos/deadlines, events, announcements, resources and
questions to you, hangs them under a per-chat-per-day container, and applies `organize.tag_rules`.
Calendar events become event units. Closed conversation segments are queued for the AI organizer
(only when AI is enabled and the chat's policy allows it). Titles here are the message text;
the AI organizer later adds main-idea units above them.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

from ..sync.triage import ASK, NOT_A_TASK, OBLIGATION, _matches
from . import units as U

# NOT_A_TASK / ASK / OBLIGATION are shared with triage (single source of truth).
URL = re.compile(r"https?://\S+")
AT = re.compile(r"@\S+\s?")


def _title(text: str, n=80) -> str:
    t = " ".join(AT.sub("", text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _day_container(store, m, tz) -> int:
    day = datetime.fromtimestamp(m["sent_at"], tz).strftime("%Y-%m-%d")
    key = f"day:{m['source']}:{m['account']}:{m['channel']}:{day}"
    uid = U.upsert(store, key, kind="digest", title=f"{m['channel_name']} · {day}", origin="rule", activity_at=m["sent_at"])
    U.add_tags(store, uid, ["type:digest", _source_tag(m), f"channel:{m['channel_name']}"])
    return uid


def _source_tag(m) -> str:
    return f"source:{m['source']}" + (f":{m['account']}" if m["account"] else "")


def detect(m: dict, ocfg: dict) -> list[tuple[str, dict]]:
    text = m["text"] or ""
    if m["kind"] in ("sticker", "system", "image", "voice", "video") or len(text) < 4 or m["from_me"]:
        return []
    f = json.loads(m["flags"] or "{}")
    low = text.lower()
    body = AT.sub("", text)        # "@我 记得..." is an instruction, not "I recall"
    out = []
    has_kw = lambda kws: any(k.lower() in low for k in kws)
    # A todo needs an obligation word, plus either a date or an instruction aimed at the reader -
    # and must not be someone recalling something or a "done" notification.
    if not NOT_A_TASK.search(body):
        obligation = has_kw(OBLIGATION) or has_kw(ocfg.get("todo_keywords", []))
        asked = bool(ASK.search(body))
        if (has_kw(OBLIGATION) and (f.get("date") or asked)) or (obligation and f.get("date") and asked):
            out.append(("todo", {"due_at": f.get("date"), "deadline": bool(f.get("date"))}))
    if has_kw(ocfg["event_keywords"]) and f.get("date") and not NOT_A_TASK.search(body):
        out.append(("event", {"start_at": f.get("date")}))
    if m["is_group"] and any(k in text for k in ocfg["announcement_markers"]) and len(text) >= 15:
        out.append(("announcement", {}))
    if f.get("has_link") and m["policy"] == "watch":
        out.append(("resource", {}))
    if m["mentions_me"] and f.get("question"):
        out.append(("question", {}))
    return out


def ai_covers(cfg: dict, policy: str) -> bool:
    """True when the AI organizer reads this chat, so its broader topics are the main record."""
    ai = cfg["ai"]
    return bool(ai.get("enabled")) and policy in ai.get("policies", [])


def organize_messages(cfg, store) -> dict:
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    ocfg = cfg["organize"]
    last = store.cursor("organize:rule:msg") or 0
    rows = store.q("""SELECT * FROM messages WHERE id > ? AND policy != 'ignore' ORDER BY id LIMIT 20000""", (last,)).fetchall()
    stats = {"messages": len(rows), "units": 0}
    for m in rows:
        last = m["id"]
        found = detect(m, ocfg)
        extra_tags, forced = [], None
        for tr in ocfg.get("tag_rules") or []:
            if _matches(tr.get("when", {}), dict(m)):
                extra_tags += tr.get("tags", [])
                forced = forced or tr.get("create")
        if forced and forced not in [k for k, _ in found]:
            found.append((forced, {"due_at": json.loads(m["flags"] or "{}").get("date")}))
        if ai_covers(cfg, m["policy"]):
            found = [(k, i) for k, i in found if k in cfg["organize"].get("rule_kinds_when_ai", ["todo", "event"])]
        if not found:
            continue
        parent = _day_container(store, m, tz)
        for kind, info in found:
            uid = U.upsert(store, f"rule:{kind}:{m['id']}", kind=kind, title=_title(m["text"]), priority=m["priority"],
                           due_at=info.get("due_at") if kind == "todo" else None,
                           start_at=info.get("start_at") if kind == "event" else None,
                           origin="rule", activity_at=m["sent_at"],
                           for_me=bool(m["mentions_me"] or m["from_me"] or not m["is_group"]))
            tags = [f"type:{kind}", _source_tag(m), f"channel:{m['channel_name']}", *extra_tags]
            if m["sender_name"]:
                tags.append(f"person:{m['sender_name']}")
            if info.get("deadline"):
                tags.append("type:deadline")
            U.add_tags(store, uid, tags)
            U.attach(store, uid, [m["id"]])
            U.link(store, parent, uid)
            stats["units"] += 1
    store.db.commit()
    store.set_cursor("organize:rule:msg", last)
    return stats


def organize_calendar(cfg, store) -> int:
    n = 0
    for e in store.q("SELECT * FROM events").fetchall():
        uid = U.upsert(store, f"cal:{e['source']}:{e['account']}:{e['item_key']}", kind="event", title=e["title"],
                       start_at=e["start_at"], end_at=e["end_at"], origin="sync", activity_at=e["start_at"])
        U.add_tags(store, uid, ["type:event", "type:calendar", f"source:{e['source']}" + (f":{e['account']}" if e["account"] else "")])
        n += 1
    store.db.commit()
    return n


def queue_segments(cfg, store) -> int:
    """Closed segments -> AI jobs (only if AI enabled and the chat policy allows sending text)."""
    ai = cfg["ai"]
    # any closed, not-yet-AI-organized segment; the jobs table dedups via ON CONFLICT
    rows = store.q("SELECT id, policy, n FROM segments WHERE closed=1 AND ai_done=0").fetchall()
    queued, now = 0, int(time.time())
    for s in rows:
        if ai["enabled"] or ai.get("queue_for_agents"):
            if s["policy"] in ai["policies"] and s["n"] >= int(ai["min_messages"]):
                queued += store.q("""INSERT INTO jobs(kind, ref_id, status, created_at, updated_at) VALUES('segment',?,'pending',?,?)
                                     ON CONFLICT(kind, ref_id) DO UPDATE SET status=CASE WHEN jobs.status IN ('done','failed')
                                     THEN 'pending' ELSE jobs.status END, updated_at=excluded.updated_at""",
                                  (s["id"], now, now)).rowcount
        store.q("UPDATE segments SET rule_done=1 WHERE id=?", (s["id"],))
    store.db.commit()
    return queued


def run(cfg, store) -> dict:
    if not cfg["organize"]["rule_organizer"]:
        return {"skipped": True}
    out = organize_messages(cfg, store)
    out["calendar_units"] = organize_calendar(cfg, store)
    out["queued_for_ai"] = queue_segments(cfg, store)
    return out
