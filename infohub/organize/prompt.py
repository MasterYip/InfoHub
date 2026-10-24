"""Shared contract for AI organizers (Claude API worker and external agents).

The system prompt and schema are constants so the prompt prefix stays byte-stable (cacheable).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import units as U

SYSTEM = """You organize one person's chat messages into info units for their personal information hub.

An info unit is ONE main idea, not a sentence: a task to do, an event, a topic people are discussing,
a decision that was made, a question someone needs answered, an announcement, or a useful resource.
Write the title as the main idea in a few words (same language as the chat), e.g.
"组会改到周五下午" / "Reserve GPU node 3 for Friday's training run". Put details in the summary (1-3 sentences).

Input: a JSON object with
- "chat": chat name and type
- "messages": [{"id", "time", "sender", "text"}] for one conversation burst
- "existing_units": recent units from the same chat (id, kind, title, status, due) — reuse them
- "now": current local time

Output rules:
- Create units only for things worth remembering; small talk and stickers produce nothing. An empty list is fine.
- Prefer a few broad units over many small ones. Group everything the group said around one
  subject into a single topic that spans the whole conversation, and attach all of its messages:
  "周五训练任务的 GPU 排期讨论" rather than one unit per reply. A passing remark that adds
  nothing to a topic is not its own unit. Aim for at most about one unit per ten messages.
- kinds: topic | todo | event | decision | question | announcement | resource | note
- Recursion: give each unit a short "ref". Use "parent" to nest units: another ref in your output
  (e.g. todos under the topic they came from) or "unit:<id>" for an existing unit. Use null for top level.
- If a message continues or updates an existing unit, set "existing_id" to that id instead of creating a duplicate.
- If the conversation shows an existing todo was finished or cancelled, put its id in "close_units".
- tags: namespaced strings — topic:<few words>, project:<name>, course:<name>, type:homework, type:deadline.
  Do not add type:<kind> (added automatically). people: display names of the people involved.
- due/start: ISO 8601 local time with offset when a date or time is stated or clearly implied, else null.
- message_ids: ids of the messages that support the unit.
- for_me: true when this concerns the hub's owner personally - addressed to them, assigned to them,
  something they must do or attend, or a direct reply to them. Use false for other people's business.
- Never invent facts that are not in the messages.
"""

_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "existing_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "kind": {"type": "string", "enum": ["topic", "todo", "event", "decision", "question", "announcement", "resource", "note"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "people": {"type": "array", "items": {"type": "string"}},
                    "due": _NULLABLE_STR,
                    "start": _NULLABLE_STR,
                    "status": {"type": "string", "enum": ["open", "done"]},
                    "parent": _NULLABLE_STR,
                    "for_me": {"type": "boolean"},
                    "message_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["ref", "existing_id", "kind", "title", "summary", "tags", "people", "due", "start",
                             "status", "parent", "for_me", "message_ids"],
                "additionalProperties": False,
            },
        },
        "close_units": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["units", "close_units"],
    "additionalProperties": False,
}


def build_input(cfg, store, segment_id: int, max_messages: int) -> tuple[dict, set[int], dict]:
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    seg = dict(store.q("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone())
    rows = store.q("""SELECT id, sent_at, sender_name, text, kind FROM messages WHERE segment_id=?
                      AND kind NOT IN ('sticker','system') ORDER BY sent_at, id""", (segment_id,)).fetchall()
    rows = rows[-max_messages:]
    msgs = [{"id": r["id"], "time": datetime.fromtimestamp(r["sent_at"], tz).strftime("%m-%d %H:%M"),
             "sender": r["sender_name"] or "?", "text": (r["text"] or "")[:800]} for r in rows]
    recent = store.q("""SELECT DISTINCT u.id, u.kind, u.title, u.status, u.due_at FROM units u
                        JOIN unit_messages um ON um.unit_id=u.id JOIN messages m ON m.id=um.message_id
                        WHERE m.source=? AND m.account=? AND m.channel=? AND u.kind!='digest'
                          AND m.sent_at >= ? ORDER BY u.last_activity_at DESC LIMIT 40""",
                     (seg["source"], seg["account"], seg["channel"], seg["start_at"] - 14 * 86400)).fetchall()
    existing = [{"id": r["id"], "kind": r["kind"], "title": r["title"], "status": r["status"],
                 "due": datetime.fromtimestamp(r["due_at"], tz).isoformat() if r["due_at"] else None} for r in recent]
    chat = {"name": seg["channel_name"], "source": seg["source"] + (f":{seg['account']}" if seg["account"] else ""),
            "policy": seg["policy"]}
    payload = {"chat": chat, "now": datetime.now(tz).isoformat(timespec="minutes"),
               "owner_names": cfg["me"]["names"], "messages": msgs, "existing_units": existing}
    return payload, {m["id"] for m in msgs}, seg


def parse(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        raise ValueError("result is not {units: [...]}")
    return data
