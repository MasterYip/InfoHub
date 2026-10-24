"""In-dashboard chat assistant over the hub (DeepSeek / any OpenAI-compatible chat API).

You type in the dashboard; the assistant can search your info units and messages, list todos
and events, and — when you ask — create or update units and re-organize chats it finds. It runs
an OpenAI-style tool-calling loop against `ai.base_url`/`ai.model` using the key in `ai.api_key_env`.
Reads are free; writes go through the same unit functions as the rest of the app and are summarized
back to you. Bounded by a per-turn tool-call cap and the daily AI budget.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from ..organize import units as U
from ..organize.ai_organizer import spent_today, _record_usage
from ..organize.providers import api_key
from . import brief as brief_mod
from . import server as server_mod

MAX_TOOL_CALLS = 12

SYSTEM = """You are the assistant inside "Info Hub", a personal dashboard that organizes the user's
chat messages into info units (todo, event, topic, question, decision, announcement, resource, note).

You can answer questions about what's in the hub and, when the user asks, change it. Use the tools:
- search(query): find units, messages, people, chats and tags matching a word.
- list_units(kind, status, tags, due_within_days, mine, limit): list info units.
- get_unit(id): one unit with its parents, children, tags and the messages behind it.
- brief(days, tags): a Markdown summary of open todos, events and topics.
- create_unit(kind, title, summary, tags, due, message_ids): make a new unit (e.g. a todo the hub
  missed). Attach message_ids from search results as evidence when relevant.
- update_unit(id, status, title, add_tags, remove_tags): change a unit (status: open|done|dropped).

Guidance: prefer reading first. Titles are short main ideas, not sentences. Dates are ISO 8601 with
offset. When the user asks you to "find things the hub missed", search for the topic, look at the
messages, and create units for anything real that has no unit yet. Be concise; after acting, say what
you changed. Never invent facts that aren't in the data.

Cite your sources so the user can jump straight to them. When your answer refers to a specific
info unit, group/chat, or person, add an inline reference the app turns into a clickable chip:
- a unit as [[u:<id>]] right after you mention it (e.g. "weekly sync moved to Friday [[u:142]]"),
- a group or chat as [[g:<exact chat name>]] (e.g. "[[g:Robotics Lab]]"),
- a person as [[p:<display name>]] (e.g. "[[p:Alex]]").
Use ids and names exactly as they appear in tool results; never invent an id. Put the reference next
to the thing it points at, not in a separate list. You may use light Markdown (bold, lists, `code`,
fenced code blocks) in your answers."""

TOOLS = [
    {"type": "function", "function": {"name": "search", "description": "Search units, messages, people, chats, tags.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "list_units", "description": "List info units.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string"}, "status": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
            "due_within_days": {"type": "number"}, "mine": {"type": "boolean"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "get_unit", "description": "One unit with its evidence.",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "brief", "description": "Markdown summary of todos/events/topics.",
        "parameters": {"type": "object", "properties": {"days": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}}}}},
    {"type": "function", "function": {"name": "create_unit", "description": "Create a new info unit.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string"}, "title": {"type": "string"}, "summary": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}}, "due": {"type": "string"},
            "message_ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["kind", "title"]}}},
    {"type": "function", "function": {"name": "update_unit", "description": "Update a unit.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"}, "status": {"type": "string"}, "title": {"type": "string"},
            "add_tags": {"type": "array", "items": {"type": "string"}}, "remove_tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["id"]}}},
]


def _tz(cfg):
    return timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))


def _compact_unit(u):
    return {"id": u["id"], "kind": u["kind"], "title": u["title"], "status": u["status"],
            "due": u.get("due_at"), "tags": [t for t in u.get("tags", []) if not t.startswith("source:")],
            "msgs": u.get("n_messages")}


def run_tool(cfg, store, name, args, changes):
    tz = _tz(cfg)
    if name == "search":
        d = server_mod.search_all(store, args.get("query", ""), limit=15)
        return {"units": [_compact_unit(u) for u in d["units"]],
                "messages": [{"id": m["id"], "chat": m["channel_name"], "sender": m["sender_name"],
                              "text": (m["text"] or "")[:200]} for m in d["messages"][:15]],
                "people": [{"name": p["sender_name"], "n": p["n"]} for p in d["people"][:10]],
                "chats": [{"name": c["channel_name"], "policy": c["policy"], "n": c["n"]} for c in d["channels"][:10]],
                "tags": [{"tag": t["tag"], "n": t["n"]} for t in d["tags"][:15]]}
    if name == "list_units":
        since = time.time() - float(args["due_within_days"]) * 86400 if args.get("due_within_days") else None
        due_before = int(time.time() + float(args["due_within_days"]) * 86400) if args.get("due_within_days") else None
        us = U.query(store, tags=args.get("tags") or (), kind=args.get("kind"), status=args.get("status"),
                     due_before=due_before, order="due" if args.get("due_within_days") else "activity",
                     limit=int(args.get("limit", 30)), mine=bool(args.get("mine")))
        return [_compact_unit(u) for u in us]
    if name == "get_unit":
        u = U.get(store, int(args["id"]))
        if not u:
            return {"error": "not found"}
        return {**_compact_unit(u), "summary": u.get("summary"),
                "parents": [p["title"] for p in u.get("parents", [])],
                "children": [c["title"] for c in u.get("children", [])],
                "messages": [{"id": m["id"], "sender": m["sender_name"], "text": (m["text"] or "")[:200]} for m in u.get("messages", [])]}
    if name == "brief":
        return {"markdown": brief_mod.brief(cfg, store, int(args.get("days", 7)), args.get("tags") or ())}
    if name == "create_unit":
        kind = args["kind"] if args["kind"] in U.KINDS else "note"
        uid = U.upsert(store, None, kind=kind, title=args["title"][:200], summary=args.get("summary"),
                       due_at=U.iso_to_epoch(args.get("due"), tz), origin="user")
        U.add_tags(store, uid, [f"type:{kind}"] + list(args.get("tags") or []))
        mids = [int(m) for m in args.get("message_ids") or []
                if store.q("SELECT 1 FROM messages WHERE id=?", (int(m),)).fetchone()]
        U.attach(store, uid, mids)
        U.mark_for_me(store, uid)
        store.db.commit()
        changes.append({"action": "created", "id": uid, "title": args["title"][:80]})
        return {"created": uid}
    if name == "update_unit":
        uid = int(args["id"])
        if not store.q("SELECT 1 FROM units WHERE id=?", (uid,)).fetchone():
            return {"error": "not found"}
        status = args.get("status") if args.get("status") in ("open", "done", "dropped") else None
        U.update(store, uid, **{k: v for k, v in {"status": status, "title": args.get("title")}.items() if v})
        U.add_tags(store, uid, args.get("add_tags"))
        U.remove_tags(store, uid, args.get("remove_tags"))
        store.db.commit()
        changes.append({"action": "updated", "id": uid, "status": status})
        return {"updated": uid}
    return {"error": f"unknown tool {name}"}


def _post(ai, messages, tools=True):
    body = {"model": ai["model"], "max_tokens": int(ai.get("max_tokens", 4000)), "stream": False, "messages": messages}
    if tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
    req = urllib.request.Request(ai["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key(ai)}"})
    with urllib.request.urlopen(req, timeout=float(ai.get("timeout_seconds", 120))) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(cfg, store, history: list[dict]) -> dict:
    """history: [{role, content}]. Returns {reply, changes, usage_cost} or {error}."""
    ai = cfg["ai"]
    if ai.get("provider") != "openai_compatible":
        return {"error": "the chat assistant currently needs ai.provider: openai_compatible (e.g. DeepSeek)."}
    if not api_key(ai):
        return {"error": f"set the {ai.get('api_key_env')} environment variable to use chat."}
    if spent_today(store) >= float(ai["daily_budget_usd"]):
        return {"error": "daily AI budget reached."}
    tz = _tz(cfg)
    now = datetime.now(tz).isoformat(timespec="minutes")
    messages = [{"role": "system", "content": SYSTEM + f"\nCurrent time: {now}."}]
    messages += [{"role": m["role"], "content": m.get("content", "")} for m in history if m.get("role") in ("user", "assistant")]
    changes, cost = [], 0.0
    for step in range(MAX_TOOL_CALLS + 1):
        force_answer = step == MAX_TOOL_CALLS      # last step: no more tools, just answer
        try:
            data = _post(ai, messages, tools=not force_answer)
        except urllib.error.HTTPError as exc:
            return {"error": f"HTTP {exc.code} from the AI endpoint"}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        u = data.get("usage") or {}
        cost += _record_usage(store, ai, {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0),
                                          "cache_read": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0), "cache_write": 0})
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg.get("tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls or force_answer:
            reply = msg.get("content") or ("Done." if changes else "I couldn't find anything to answer that.")
            return {"reply": reply, "changes": changes, "cost": round(cost, 5)}
        for call in calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = run_tool(cfg, store, fn.get("name"), args, changes)
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)[:4000]})
    return {"reply": "(stopped after too many steps)", "changes": changes, "cost": round(cost, 5)}


def _post_stream(ai, messages, tools=True):
    """Yield the raw streamed chunks (parsed JSON) from an OpenAI-compatible /chat/completions."""
    body = {"model": ai["model"], "max_tokens": int(ai.get("max_tokens", 4000)), "stream": True,
            "messages": messages, "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
    req = urllib.request.Request(ai["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key(ai)}"})
    with urllib.request.urlopen(req, timeout=float(ai.get("timeout_seconds", 120))) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue


def _tool_label(name, args):
    if name == "search":
        return f"searching “{args.get('query', '')}”…"
    if name == "get_unit":
        return f"reading unit {args.get('id')}…"
    if name == "create_unit":
        return f"creating a unit: {args.get('title', '')[:40]}…"
    if name == "update_unit":
        return f"updating unit {args.get('id')}…"
    return f"{name}…"


def chat_stream(cfg, store, lock, history: list[dict]):
    """Generator of Server-Sent-Event dicts: {type: delta|status|error|done, ...}.

    Network calls to the model run without `lock`; only the hub's DB tool calls acquire it, so a long
    chat never blocks the rest of the server. Streams the assistant's text token-by-token."""
    ai = cfg["ai"]
    if ai.get("provider") != "openai_compatible":
        yield {"type": "error", "text": "chat needs ai.provider: openai_compatible (e.g. DeepSeek)."}; return
    if not api_key(ai):
        yield {"type": "error", "text": f"set the {ai.get('api_key_env')} environment variable to use chat."}; return
    with lock:
        over_budget = spent_today(store) >= float(ai["daily_budget_usd"])
    if over_budget:
        yield {"type": "error", "text": "daily AI budget reached."}; return
    tz = _tz(cfg)
    now = datetime.now(tz).isoformat(timespec="minutes")
    messages = [{"role": "system", "content": SYSTEM + f"\nCurrent time: {now}."}]
    messages += [{"role": m["role"], "content": m.get("content", "")} for m in history if m.get("role") in ("user", "assistant")]
    changes, cost = [], 0.0
    for step in range(MAX_TOOL_CALLS + 1):
        force_answer = step == MAX_TOOL_CALLS
        parts, tcs, usage = [], {}, None
        try:
            for chunk in _post_stream(ai, messages, tools=not force_answer):
                if chunk.get("usage"):
                    usage = chunk["usage"]
                ch = (chunk.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
                    yield {"type": "delta", "text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    slot = tcs.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
        except urllib.error.HTTPError as exc:
            yield {"type": "error", "text": f"HTTP {exc.code} from the AI endpoint"}; return
        except Exception as exc:
            yield {"type": "error", "text": f"{type(exc).__name__}: {str(exc)[:120]}"}; return
        if usage:
            with lock:
                cost += _record_usage(store, ai, {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0),
                                                  "cache_read": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0), "cache_write": 0})
        calls = list(tcs.values())
        messages.append({"role": "assistant", "content": "".join(parts),
                         "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}} for c in calls] or None})
        if not calls or force_answer:
            yield {"type": "done", "changes": changes, "cost": round(cost, 5)}; return
        for c in calls:
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "status", "text": _tool_label(c["name"], args)}
            with lock:
                try:
                    result = run_tool(cfg, store, c["name"], args, changes)
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result, ensure_ascii=False)[:4000]})
    yield {"type": "done", "changes": changes, "cost": round(cost, 5)}
