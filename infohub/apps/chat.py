"""In-dashboard chat assistant over the hub (DeepSeek / any OpenAI-compatible chat API).

You type in the dashboard; the assistant can search your info units and messages, list todos
and events, and — when you ask — edit units, evidence, tags and graph connections. It runs
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
- search(query), list_units(...), get_unit(id), get_messages(message_ids), brief(days, tags): read
  units, messages and summaries. get_unit returns IDs and relationships for parents and children.
- get_graph(tags, kind, status, query, limit): inspect graph nodes and explicit edges, including
  one hop of neighbors. An edge's source is its parent and target is its child.
- list_tags(prefix, limit): discover the actual tags and how many units use each one.
- create_unit(..., parent_ids, message_ids): create a unit with evidence and any existing parents.
- update_unit(id, ...): edit kind, title, summary, status, priority, dates, personal scope, tags or
  message evidence. Use null to clear a date. Adding type:<kind> reclassifies the unit.
- link_units(parent_id, child_id, relation), unlink_units(parent_id, child_id, relation): add or
  remove an explicit graph edge. The default relation is part_of; a child can have several parents.
  For a related topic without a containment meaning use relation related_to; all edges are directed
  and cycles are refused. Choose the direction from the user's request, or ask when ambiguous.
- rename_tag(from_tag, to_tag): rename a tag across all units, consolidating duplicates when the
  destination tag already exists. This changes tags, not unit identity or graph edges.

For graph organization, inspect the units and existing edges first, then use link_units for real
connections. Shared tags help filtering but do not create edges. Keep other parents when adding a
connection. Use unlink_units only when asked to remove or move a connection. For duplicate tags,
inspect list_tags before rename_tag. Never claim a change succeeded when a tool returns an error.

Counts: msgs includes all descendant branches; repeated message attachments and shared branches count again.
direct_msgs counts evidence attached directly to that unit.

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

UNIT_FIELDS = {
    "kind": {"type": "string", "enum": U.KINDS}, "title": {"type": "string"},
    "summary": {"type": "string"}, "status": {"type": "string", "enum": ["open", "done", "dropped"]},
    "priority": {"type": ["string", "null"], "enum": ["P0", "P1", "P2", "P3", None]},
    "due": {"type": ["string", "null"], "description": "ISO 8601 date/time; null clears it."},
    "start": {"type": ["string", "null"]}, "end": {"type": ["string", "null"]},
    "for_me": {"type": "boolean"},
}
TAGS = {"type": "array", "items": {"type": "string"}}
IDS = {"type": "array", "items": {"type": "integer"}}
FILTERS = {
    "kind": {"type": "string", "enum": U.KINDS}, "status": {"type": "string"}, "tags": TAGS,
    "query": {"type": "string"}, "mine": {"type": "boolean"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
}
EDGE = {"parent_id": {"type": "integer"}, "child_id": {"type": "integer"},
        "relation": {"type": "string", "description": "Directed edge label; defaults to part_of."}}


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


TOOLS = [
    _tool("search", "Search units, messages, people, chats, tags.", {"query": {"type": "string"}}, ["query"]),
    _tool("list_units", "List info units.", {**FILTERS, "due_within_days": {"type": "number"}}),
    _tool("get_unit", "Read a unit with evidence and parent/child IDs and relationships.",
          {"id": {"type": "integer"}}, ["id"]),
    _tool("get_messages", "Read full message evidence by IDs (up to 10).", {"message_ids": IDS}, ["message_ids"]),
    _tool("brief", "Markdown summary of todos/events/topics.", {"days": {"type": "integer"}, "tags": TAGS}),
    _tool("get_graph", "Inspect directed edges, up to limit matching nodes and limit neighbors; use get_unit for all incident edges.", FILTERS),
    _tool("list_tags", "List existing tags and unit counts. Prefix example: topic:.",
          {"prefix": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _tool("create_unit", "Create an info unit, optionally under several existing parents, with evidence.",
          {**UNIT_FIELDS, "tags": TAGS, "parent_ids": IDS, "message_ids": IDS}, ["kind", "title"]),
    _tool("update_unit", "Edit a unit, reclassify its kind, add/remove tags or attach/detach evidence.",
          {**UNIT_FIELDS, "id": {"type": "integer"}, "add_tags": TAGS, "remove_tags": TAGS,
           "message_ids": IDS, "remove_message_ids": IDS}, ["id"]),
    _tool("link_units", "Add a directed parent-to-child graph edge. Preserves other parents; refuses cycles.",
          EDGE, ["parent_id", "child_id"]),
    _tool("unlink_units", "Remove only the specified graph edge; preserves units, tags, evidence and other edges.",
          EDGE, ["parent_id", "child_id"]),
    _tool("rename_tag", "Rename/consolidate a tag across ALL units in its namespace. Reserved tags cannot be renamed.",
          {"from_tag": {"type": "string"}, "to_tag": {"type": "string"}}, ["from_tag", "to_tag"]),
]
WRITE_TOOLS = {"create_unit", "update_unit", "link_units", "unlink_units", "rename_tag"}


def _tz(cfg):
    return timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))


def _compact_unit(u):
    return {"id": u["id"], "kind": u["kind"], "title": u["title"], "status": u["status"],
            "due": u.get("due_at"), "start": u.get("start_at"), "end": u.get("end_at"),
            "priority": u.get("priority"), "for_me": bool(u.get("for_me")), "tags": [t for t in u.get("tags", []) if not t.startswith("source:")],
            "msgs": u.get("n_messages"), "direct_msgs": u.get("n_direct_messages"),
            "subtopics": u.get("n_descendants")}


def run_tool(cfg, store, name, args, changes):
    """Apply each write atomically and report changes only after a successful commit."""
    if not isinstance(args, dict):
        return {"error": "tool arguments must be an object"}
    try:
        if name in WRITE_TOOLS:
            pending = []
            with store.db:
                result = _run_tool(cfg, store, name, args, pending)
            changes.extend(pending)
            return result
        return _run_tool(cfg, store, name, args, [])
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": str(exc)}


def _require_units(store, ids):
    for uid in ids:
        if not store.q("SELECT 1 FROM units WHERE id=?", (int(uid),)).fetchone():
            raise ValueError(f"unit {uid} not found")


def _message_ids(store, ids):
    mids = list(dict.fromkeys(int(m) for m in ids or []))
    for mid in mids:
        if not store.q("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone():
            raise ValueError(f"message {mid} not found")
    return mids


def _fields(args, tz):
    fields = {k: args[k] for k in ("kind", "title", "summary", "status", "priority") if k in args}
    if "kind" in fields and fields["kind"] not in U.KINDS:
        raise ValueError("bad kind")
    if "status" in fields and fields["status"] not in ("open", "done", "dropped"):
        raise ValueError("bad status")
    if "priority" in fields and fields["priority"] not in (None, "P0", "P1", "P2", "P3"):
        raise ValueError("bad priority")
    if "title" in fields:
        fields["title"] = fields["title"].strip()[:200]
        if not fields["title"]:
            raise ValueError("title required")
    for k in ("due", "start", "end"):
        if k in args:
            value = U.iso_to_epoch(args[k], tz)
            if args[k] is not None and value is None:
                raise ValueError(f"invalid {k}: use an ISO 8601 date/time or null")
            fields[k + "_at"] = value
    return fields


def _run_tool(cfg, store, name, args, changes):
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
        due_before = int(time.time() + float(args["due_within_days"]) * 86400) if args.get("due_within_days") else None
        us = U.query(store, tags=args.get("tags") or (), kind=args.get("kind"), status=args.get("status"),
                     q=args.get("query"), due_before=due_before, order="due" if args.get("due_within_days") else "activity",
                     limit=max(1, min(50, int(args.get("limit", 30)))), mine=bool(args.get("mine")))
        return [_compact_unit(u) for u in us]
    if name == "get_unit":
        u = U.get(store, int(args["id"]))
        if not u:
            return {"error": "not found"}
        return {**_compact_unit(u), "summary": u.get("summary"),
                "parents": u.get("parents", []),
                "children": [dict(r) for r in store.q(
                    "SELECT u.id, u.kind, u.title, l.relation FROM unit_links l JOIN units u ON u.id=l.child_id "
                    "WHERE l.parent_id=?", (u["id"],))],
                "messages": [{"id": m["id"], "sender": m["sender_name"], "text": (m["text"] or "")[:200]} for m in u.get("messages", [])]}
    if name == "brief":
        return {"markdown": brief_mod.brief(cfg, store, int(args.get("days", 7)), args.get("tags") or ())}
    if name == "get_messages":
        mids = _message_ids(store, args["message_ids"])
        if len(mids) > 10:
            raise ValueError("read at most 10 messages at a time")
        return [dict(store.q("SELECT id, channel_name, sender_name, sent_at, text, link FROM messages WHERE id=?",
                             (mid,)).fetchone()) for mid in mids]
    if name == "get_graph":
        return server_mod.graph(store, include_context=True, tags=args.get("tags") or (), kind=args.get("kind"),
                                status=args.get("status"), q=args.get("query"), mine=bool(args.get("mine")),
                                limit=max(1, min(50, int(args.get("limit", 20)))))
    if name == "list_tags":
        return U.tag_counts(store, prefix=args.get("prefix"), limit=max(1, min(100, int(args.get("limit", 50)))))
    if name in ("create_unit", "update_unit"):
        fields = _fields(args, tz)
        mids = _message_ids(store, args.get("message_ids"))
        remove_mids = _message_ids(store, args.get("remove_message_ids"))
        creating = name == "create_unit"
        tags = [U.norm_tag(t) for t in (args.get("tags" if creating else "add_tags") or [])]
        tags = [t for t in tags if t]
        type_kinds = [t.split(":", 1)[1] for t in tags if t.startswith("type:") and t.split(":", 1)[1] in U.KINDS]
        if type_kinds and "kind" not in fields:
            fields["kind"] = type_kinds[-1]
        kind = fields.get("kind")
        if creating:
            parents = list(dict.fromkeys(int(p) for p in args.get("parent_ids") or []))
            _require_units(store, parents)
            uid = U.upsert(store, None, origin="user", **fields)
            for parent in parents:
                if not U.link(store, parent, uid):
                    raise ValueError("link would create a cycle")
        else:
            uid = int(args["id"])
            _require_units(store, [uid])
            U.update(store, uid, **fields)
        if kind:
            store.q("DELETE FROM unit_tags WHERE unit_id=? AND tag LIKE 'type:%'", (uid,))
            tags = [t for t in tags if not t.startswith("type:")] + [f"type:{kind}"]
        U.add_tags(store, uid, tags)
        # Kind tags are managed above; obligation tags such as type:homework remain editable.
        U.remove_tags(store, uid, [t for t in args.get("remove_tags") or []
                                 if U.norm_tag(t) not in {f"type:{k}" for k in U.KINDS}])
        U.attach(store, uid, mids)
        for mid in remove_mids:
            store.q("DELETE FROM unit_messages WHERE unit_id=? AND message_id=?", (uid, mid))
        U.mark_for_me(store, uid, cfg.get("me", {}).get("names", []))
        if "for_me" in args:
            store.q("UPDATE units SET for_me=? WHERE id=?", (int(bool(args["for_me"])), uid))
        action = "created" if creating else "updated"
        changes.append({"action": action, "id": uid, "fields": list(fields)})
        return {action: uid}
    if name in ("link_units", "unlink_units"):
        parent, child = int(args["parent_id"]), int(args["child_id"])
        _require_units(store, [parent, child])
        relation = args.get("relation", "part_of").strip()
        if not relation or len(relation) > 60:
            raise ValueError("relation must be 1 to 60 characters")
        edge = (parent, child, relation)
        exists = bool(store.q("SELECT 1 FROM unit_links WHERE parent_id=? AND child_id=? AND relation=?", edge).fetchone())
        if name == "link_units":
            if not U.link(store, parent, child, relation):
                raise ValueError("link would create a cycle or self-link")
            changed, action = not exists, "linked"
        else:
            store.q("DELETE FROM unit_links WHERE parent_id=? AND child_id=? AND relation=?", edge)
            changed, action = exists, "unlinked"
        result = {"parent_id": parent, "child_id": child, "relation": relation, "changed": changed}
        if changed:
            changes.append({"action": action, **result})
        return result
    if name == "rename_tag":
        old, new = U.norm_tag(args["from_tag"]), U.norm_tag(args["to_tag"])
        if not old or not new:
            raise ValueError("both tags are required")
        ns = old.split(":", 1)[0]
        if ns in ("type", "source", "status") or new.split(":", 1)[0] != ns:
            raise ValueError("rename within the same non-reserved namespace; use update_unit for kind changes")
        count = store.q("SELECT count(*) FROM unit_tags WHERE tag=?", (old,)).fetchone()[0] if old != new else 0
        if count:
            store.q("INSERT OR IGNORE INTO unit_tags SELECT unit_id, ? FROM unit_tags WHERE tag=?", (new, old))
            store.q("DELETE FROM unit_tags WHERE tag=?", (old,))
            changes.append({"action": "renamed_tag", "from_tag": old, "to_tag": new, "units": count})
        return {"from_tag": old, "to_tag": new, "units": count}
    return {"error": f"unknown tool {name}"}


def _tool_content(result):
    content = json.dumps(result, ensure_ascii=False)
    if len(content) <= 16000:
        return content
    return json.dumps({"truncated": True, "preview": content[:14000],
                       "hint": "Narrow filters/limits or request specific unit/message IDs."}, ensure_ascii=False)


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
                             "content": _tool_content(result)})
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
    if not isinstance(args, dict):
        return f"{name}…"
    if name == "search":
        return f"searching “{args.get('query', '')}”…"
    if name == "get_unit":
        return f"reading unit {args.get('id')}…"
    if name == "create_unit":
        return f"creating a unit: {args.get('title', '')[:40]}…"
    if name == "update_unit":
        return f"updating unit {args.get('id')}…"
    if name in ("link_units", "unlink_units"):
        return f"{'linking' if name == 'link_units' else 'unlinking'} units {args.get('parent_id')} → {args.get('child_id')}…"
    if name == "rename_tag":
        return f"renaming tag {args.get('from_tag')} → {args.get('to_tag')}…"
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
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": _tool_content(result)})
    yield {"type": "done", "changes": changes, "cost": round(cost, 5)}
