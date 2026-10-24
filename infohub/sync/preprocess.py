"""Layer 1 preprocessing: flags per message and conversation segments. Deterministic, no AI.

A segment is a burst of messages in one chat with no pause longer than `segment_gap_minutes`.
Segments are the unit of work for Layer 2 (rule and AI organizers).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from ..dates import find_datetime

URL = re.compile(r"https?://[^\s<>\"']+")


def flags(text: str, kind: str, sent_at: int, tz_hours: int) -> dict:
    text = text or ""
    dt, has_time = find_datetime(text, datetime.fromtimestamp(sent_at, timezone(timedelta(hours=tz_hours))))
    return {"has_link": bool(URL.search(text)) or kind in ("link/file", "file", "share_chat"),
            "date": int(dt.timestamp()) if dt else None, "has_time": has_time,
            "question": any(c in text for c in ("?", "？", "吗", "么")),
            "length": len(text)}


def assign_segments(store, message_ids: list[int], gap_minutes: int, max_messages: int = 80) -> int:
    """Attach newly stored messages to an existing open segment of their chat or start a new one.

    A segment is also closed once it reaches `max_messages`, so one busy day becomes several
    segments: every message is organized, and no single request is too large for the model.
    """
    if not message_ids:
        return 0
    gap = gap_minutes * 60
    marks = ",".join("?" * len(message_ids))
    rows = store.q(f"""SELECT id, source, account, channel, channel_name, policy, sent_at FROM messages
                       WHERE id IN ({marks}) ORDER BY source, account, channel, sent_at""", message_ids).fetchall()
    created, now = 0, int(time.time())
    for r in rows:
        seg = store.q("""SELECT id, start_at, end_at FROM segments WHERE source=? AND account=? AND channel=?
                         AND end_at >= ? AND start_at <= ? AND n < ? ORDER BY end_at DESC LIMIT 1""",
                      (r["source"], r["account"], r["channel"], r["sent_at"] - gap, r["sent_at"] + gap, int(max_messages))).fetchone()
        if seg:
            store.q("""UPDATE segments SET start_at=min(start_at,?), end_at=max(end_at,?), n=n+1, closed=0,
                       rule_done=0, policy=?, channel_name=?, updated_at=? WHERE id=?""",
                    (r["sent_at"], r["sent_at"], r["policy"], r["channel_name"], now, seg["id"]))
            sid = seg["id"]
        else:
            sid = store.q("""INSERT INTO segments(source,account,channel,channel_name,policy,start_at,end_at,n,updated_at)
                             VALUES(?,?,?,?,?,?,?,1,?)""",
                          (r["source"], r["account"], r["channel"], r["channel_name"], r["policy"], r["sent_at"], r["sent_at"], now)).lastrowid
            created += 1
        store.q("UPDATE messages SET segment_id=? WHERE id=?", (sid, r["id"]))
    store.db.commit()
    return created


def close_segments(store, gap_minutes: int, now: int | None = None, max_messages: int | None = None) -> int:
    """Close a segment once it has been quiet for `gap_minutes`, or once it is full
    (`n >= max_messages`) and so can no longer grow — the latter keeps busy chats
    organized promptly instead of waiting for a lull that may not come."""
    now = int(now or time.time())
    cur = store.q("""UPDATE segments SET closed=1, updated_at=? WHERE closed=0 AND
                     (end_at < ? OR (? IS NOT NULL AND n >= ?))""",
                  (now, now - gap_minutes * 60, max_messages, max_messages))
    store.db.commit()
    return cur.rowcount


def resplit(store, max_messages: int = 80) -> dict:
    """Split existing oversized segments into chunks of `max_messages` so none is truncated."""
    out = {"split": 0, "new_segments": 0}
    rows = store.q("SELECT * FROM segments WHERE n > ? ORDER BY id", (int(max_messages),)).fetchall()
    now = int(time.time())
    for seg in rows:
        ids = [r["id"] for r in store.q("SELECT id FROM messages WHERE segment_id=? ORDER BY sent_at, id", (seg["id"],))]
        if len(ids) <= max_messages:
            continue
        out["split"] += 1
        for start in range(max_messages, len(ids), max_messages):
            chunk = ids[start:start + max_messages]
            marks = ",".join("?" * len(chunk))
            span = store.q(f"SELECT min(sent_at) a, max(sent_at) b FROM messages WHERE id IN ({marks})", chunk).fetchone()
            sid = store.q("""INSERT INTO segments(source,account,channel,channel_name,policy,start_at,end_at,n,closed,updated_at)
                             VALUES(?,?,?,?,?,?,?,?,1,?)""",
                          (seg["source"], seg["account"], seg["channel"], seg["channel_name"], seg["policy"],
                           span["a"], span["b"], len(chunk), now)).lastrowid
            store.db.executemany("UPDATE messages SET segment_id=? WHERE id=?", [(sid, i) for i in chunk])
            out["new_segments"] += 1
        head = ids[:max_messages]
        marks = ",".join("?" * len(head))
        span = store.q(f"SELECT min(sent_at) a, max(sent_at) b FROM messages WHERE id IN ({marks})", head).fetchone()
        store.q("UPDATE segments SET n=?, start_at=?, end_at=?, ai_done=0, updated_at=? WHERE id=?",
                (len(head), span["a"], span["b"], now, seg["id"]))
    store.db.commit()
    return out
