"""Rewrite rule-made unit titles into brief, plain ones (Layer 2, optional).

The rule organizer titles a unit with the message text, which reads like a chat line.
This pass sends a batch of such titles to the configured provider and gets back a short
main-idea title plus a one-line summary. It is cheap (one call per `ai.polish_batch` units),
never invents facts, and skips chats whose policy is not in `ai.polish_policies`.
Failures leave the unit untouched, so the hub keeps working without it.
"""
from __future__ import annotations

import json

from . import providers
from .ai_organizer import _record_usage, spent_today

KINDS = ("todo", "event", "announcement", "question", "resource", "decision", "note")

SYSTEM = """You rewrite chat-derived cards for a personal information dashboard.

For each item you get: id, kind, the raw chat text, and the chat name.
Return a short title and a one-line summary, in the same language as the text.

title: the main idea only, at most 20 characters for Chinese or 8 words for English.
  Drop greetings, emoji, filler, names of senders and "@" mentions. No trailing punctuation.
  Examples: "这一批今晚截止" / "周三组会改到下午" / "Submit scholarship form".
summary: one short sentence with who/what/when if the text says so; "" when the title says it all.
kind: correct the kind when the guess is wrong. A "todo" must be something the reader is
  actually asked or expected to do, and an "event" something they could attend, both still open.
  Ordinary conversation, opinions, someone recalling something, a status update, an advert or an
  automated notification is a "note" (or "resource" for a shared link, "question" for a question).

Never invent facts that are not in the text. Keep every id you were given.
Reply with one JSON object: {"items": [{"id": <int>, "title": "...", "summary": "..."}]}"""

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "title": {"type": "string"}, "summary": {"type": "string"},
                       "kind": {"type": "string", "enum": list(KINDS)}},
        "required": ["id", "title", "summary", "kind"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}



def pending(store, cfg, limit: int) -> list[dict]:
    """Rule-made units that still carry a raw chat line as their title."""
    policies = cfg["ai"]["polish_policies"]
    marks = ",".join("?" * len(policies))
    rows = store.q(f"""SELECT DISTINCT u.id, u.kind, u.title, m.channel_name FROM units u
                       JOIN unit_messages um ON um.unit_id=u.id JOIN messages m ON m.id=um.message_id
                       WHERE u.origin='rule' AND u.polished=0 AND u.kind IN ({','.join('?' * len(KINDS))})
                         AND m.policy IN ({marks}) ORDER BY u.last_activity_at DESC LIMIT ?""",
                   (*KINDS, *policies, int(limit))).fetchall()
    return [dict(r) for r in rows]


def run(cfg, store, provider=None) -> dict:
    ai = cfg["ai"]
    if not (ai["enabled"] and cfg["organize"].get("polish", True)):
        return {"skipped": "polish off"}
    if spent_today(store) >= float(ai["daily_budget_usd"]):
        return {"skipped": "daily budget reached"}
    items = pending(store, cfg, int(ai["polish_batch"]))
    if not items:
        return {"polished": 0}
    try:
        provider = provider or providers.make(ai)
    except providers.ProviderError as exc:
        return {"error": str(exc)}
    payload = {"items": [{"id": i["id"], "kind": i["kind"], "chat": i["channel_name"], "text": (i["title"] or "")[:400]}
                         for i in items]}
    try:
        reply = provider.complete(payload, system=SYSTEM, schema=SCHEMA)
    except providers.ProviderError as exc:
        return {"error": str(exc)}
    cost = _record_usage(store, ai, reply.usage)
    if reply.stop != "end":
        return {"error": reply.stop, "cost": cost}
    try:
        data = json.loads(reply.text)
        rows = data["items"] if isinstance(data, dict) else data
    except (ValueError, KeyError, TypeError):
        return {"error": "invalid JSON", "cost": cost}
    known = {i["id"] for i in items}
    done = 0
    for r in rows if isinstance(rows, list) else []:
        try:
            uid, title = int(r["id"]), (r.get("title") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        if uid not in known or not title:
            continue
        kind = r.get("kind") if r.get("kind") in KINDS else None
        store.q("""UPDATE units SET title=?, summary=coalesce(nullif(?,''), summary), kind=coalesce(?, kind),
                   polished=1 WHERE id=?""", (title[:120], (r.get("summary") or "").strip()[:400], kind, uid))
        if kind and kind not in ("todo", "event"):
            store.q("DELETE FROM unit_tags WHERE unit_id=? AND tag IN ('type:todo','type:event','type:deadline')", (uid,))
            store.q("INSERT OR IGNORE INTO unit_tags VALUES(?,?)", (uid, f"type:{kind}"))
        done += 1
    store.q(f"UPDATE units SET polished=1 WHERE id IN ({','.join('?' * len(known))})", tuple(known))  # don't retry forever
    store.db.commit()
    return {"polished": done, "of": len(items), "cost": round(cost, 5)}
