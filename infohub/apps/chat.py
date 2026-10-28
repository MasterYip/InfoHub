"""In-dashboard chat assistant over the hub (DeepSeek / any OpenAI-compatible chat API).

You type in the dashboard; the assistant can search your info units and messages, list todos
and events, and — when you ask — edit units, evidence, tags and graph connections. It runs
an OpenAI-style tool-calling loop against `ai.base_url`/`ai.model` using the key in `ai.api_key_env`.
Reads are free; writes go through the same unit functions as the rest of the app and are summarized
back to you. Bounded by a per-turn tool-call cap and the daily AI budget.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from ..organize import units as U
from ..organize.ai_organizer import spent_today, _record_usage
from ..organize.providers import api_key, endpoint_opener
from . import brief as brief_mod
from . import server as server_mod

MAX_TOOL_CALLS = 24
TRANSIENT_RETRIES = 2          # extra connection attempts for one model turn
RETRY_DELAY_SECONDS = 1.0
TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException)
NO_TOOL_NUDGE = ("Now answer my original question in plain prose using everything you found. "
                 "Do not call more tools.")

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
- link_units(parent_id, child_id, relation), link_units_bulk(parent_id, child_ids, relation),
  unlink_units(parent_id, child_id, relation): add or
  remove an explicit graph edge. The default relation is part_of; a child can have several parents.
  For a related topic without a containment meaning use relation related_to; all edges are directed
  and cycles are refused. Choose the direction from the user's request, or ask when ambiguous.
- rename_tag(from_tag, to_tag): rename a tag across all units, consolidating duplicates when the
  destination tag already exists. This changes tags, not unit identity or graph edges.

For graph organization, inspect the units and existing edges first, then use link_units_bulk when
one parent should receive several children, or link_units for an individual connection. Shared tags
help filtering but do not create edges. Keep other parents when adding a connection. Use unlink_units
only when asked to remove or move a connection. For duplicate tags,
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
    _tool("link_units_bulk", "Atomically add one parent-to-child edge for several children; preserves other parents and refuses cycles.",
          {"parent_id": {"type": "integer"}, "child_ids": IDS,
           "relation": {"type": "string", "description": "Directed edge label; defaults to part_of."}},
          ["parent_id", "child_ids"]),
    _tool("unlink_units", "Remove only the specified graph edge; preserves units, tags, evidence and other edges.",
          EDGE, ["parent_id", "child_id"]),
    _tool("rename_tag", "Rename/consolidate a tag across ALL units in its namespace. Reserved tags cannot be renamed.",
          {"from_tag": {"type": "string"}, "to_tag": {"type": "string"}}, ["from_tag", "to_tag"]),
]
WRITE_TOOLS = {"create_unit", "update_unit", "link_units", "link_units_bulk", "unlink_units", "rename_tag"}


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
    if name == "link_units_bulk":
        parent = int(args["parent_id"])
        children = list(dict.fromkeys(int(child) for child in (args.get("child_ids") or [])))
        if not children:
            raise ValueError("child_ids must contain at least one unit")
        _require_units(store, [parent, *children])
        relation = args.get("relation", "part_of").strip()
        if not relation or len(relation) > 60:
            raise ValueError("relation must be 1 to 60 characters")
        # Validate every edge before inserting any of them, so one bad child cannot leave a
        # partially applied bulk organization request.
        if parent in children:
            raise ValueError("link would create a cycle or self-link")
        for child in children:
            if child in U.ancestors(store, parent) or child == parent:
                raise ValueError("link would create a cycle or self-link")
        linked = []
        for child in children:
            edge = (parent, child, relation)
            exists = bool(store.q("SELECT 1 FROM unit_links WHERE parent_id=? AND child_id=? AND relation=?", edge).fetchone())
            if not exists and not U.link(store, parent, child, relation):
                raise ValueError("link would create a cycle or self-link")
            if not exists:
                linked.append(child)
                changes.append({"action": "linked", "parent_id": parent, "child_id": child, "relation": relation, "changed": True})
        return {"parent_id": parent, "child_ids": children, "relation": relation,
                "linked": linked, "changed": len(linked)}
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


def _legacy_tool_calls(content: str):
    """Parse DeepSeek's legacy DSML function-call text when no structured tool_calls are returned."""
    text = _normalize_dsml(content)
    if not re.search(r"<\|[^>]{0,40}DSML|invoke\s+name=", text, re.I):
        return text, []
    invocations = list(re.finditer(r"invoke\s+name=[\"']([^\"']+)[\"']", text, re.I))
    if not invocations:
        # A protocol envelope without a parseable call is still protocol, not prose.
        return "", []
    calls = []
    for index, match in enumerate(invocations):
        end = invocations[index + 1].start() if index + 1 < len(invocations) else len(text)
        block = text[match.end():end]
        params = {}
        marks = list(re.finditer(r"parameter\s+name=[\"']([^\"']+)[\"'][^>]*>", block, re.I))
        for pidx, pmark in enumerate(marks):
            stop = marks[pidx + 1].start() if pidx + 1 < len(marks) else len(block)
            value = block[pmark.end():stop]
            # DeepSeek may emit either HTML-looking tags or tokens such as
            # ``<|DSML| | parameter>``.  Both mark the end of a parameter.
            value = re.split(r"(?:<\|[^>]{0,80}DSML[^>]*>|</?\|?parameter[^>]*>)", value,
                             maxsplit=1, flags=re.I)[0]
            value = re.sub(r"<[^>]*>", "", value).strip()
            params[pmark.group(1)] = value
        calls.append({"id": f"legacy-{index}", "type": "function",
                      "function": {"name": match.group(1), "arguments": json.dumps(params, ensure_ascii=False)}})
    # DSML is protocol text, never user-facing assistant prose.
    return "", calls


def _message_tool_calls(msg):
    content = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    if calls:
        return content, calls
    return _legacy_tool_calls(content)


def _normalize_dsml(text):
    """DeepSeek sometimes streams the DSML protocol with full-width brackets/bars."""
    return (text or "").replace("｜", "|").replace("＜", "<").replace("＞", ">")


PROTOCOL_MARKER = re.compile(r"<\|[^>]{0,40}dsml|invoke\s+name\s*=", re.I)
_DSML_PARTIAL = re.compile(r"^\|*d?s?m?l?$", re.I)
_INVOKE_MARKER = "invoke name="


def _protocol_index(text):
    match = PROTOCOL_MARKER.search(_normalize_dsml(text))
    return match.start() if match else None


def _legacy_protocol_started(text):
    """Return true when streamed content has entered a legacy DSML call."""
    return _protocol_index(text) is not None


def _stream_safe_prefix(text):
    """The part of streamed prose that cannot turn out to be DSML protocol.

    Holds back a trailing partial marker (``<``, ``<|D``, ``invok``...) so the protocol
    never leaks into the visible answer when its tokens arrive in separate chunks."""
    cut = _protocol_index(text)
    if cut is not None:
        return text[:cut]
    normalized = _normalize_dsml(text).lower()
    start = normalized.rfind("<")
    if start >= 0 and _DSML_PARTIAL.match(normalized[start + 1:]):
        return text[:start]
    for size in range(min(len(normalized), len(_INVOKE_MARKER)), 0, -1):
        if _INVOKE_MARKER.startswith(normalized[-size:]):
            return text[:len(text) - size]
    return text


def _post(ai, messages, tools=True, tool_choice="auto"):
    body = {"model": ai["model"], "max_tokens": int(ai.get("max_tokens", 4000)), "stream": False, "messages": messages}
    if tools:
        body["tools"] = TOOLS
        body["tool_choice"] = tool_choice
    req = urllib.request.Request(ai["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key(ai)}"})
    opener = endpoint_opener(ai)
    attempt = 0
    while True:
        try:
            with opener.open(req, timeout=float(ai.get("timeout_seconds", 120))) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except TRANSIENT_ERRORS:
            attempt += 1
            if attempt > TRANSIENT_RETRIES:
                raise
            time.sleep(RETRY_DELAY_SECONDS * attempt)


def _endpoint_failure(exc, attempts):
    detail = str(exc)[:160] or type(exc).__name__
    tried = f" after {attempts} attempts" if attempts > 1 else ""
    return (f"Couldn't reach the AI endpoint{tried} ({type(exc).__name__}: {detail}). "
            "Check the network, or set ai.proxy: direct in config/hub.local.yaml if the system proxy is unreliable.")


def _fallback_answer(changes):
    if changes:
        return "Done — I applied the requested changes."
    return "I couldn't put together an answer for that one. Please try again, or ask a narrower question."


def _plain_answer(ai, messages):
    """One extra turn that must produce prose; returns (text, usage)."""
    try:
        # Keep the tools declared but forbid calls: some OpenAI-compatible models otherwise
        # answer with a bare DSML call (or nothing at all) when the tool loop ends.
        data = _post(ai, messages + [{"role": "user", "content": NO_TOOL_NUDGE}],
                     tools=True, tool_choice="none")
    except Exception:
        return "", None
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text, _calls = _message_tool_calls(msg)
    return (text or "").strip(), data.get("usage") or None


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
        clean_content, calls = _message_tool_calls(msg)
        messages.append({"role": "assistant", "content": clean_content, "tool_calls": calls or None})
        if not calls or force_answer:
            reply = clean_content or ("Done." if changes else "I couldn't find anything to answer that.")
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
    with endpoint_opener(ai).open(req, timeout=float(ai.get("timeout_seconds", 120))) as r:
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
    if name in ("link_units", "link_units_bulk", "unlink_units"):
        if name == "link_units_bulk":
            return f"linking {len(args.get('child_ids') or [])} units under {args.get('parent_id')}…"
        return f"{'linking' if name == 'link_units' else 'unlinking'} units {args.get('parent_id')} → {args.get('child_id')}…"
    if name == "rename_tag":
        return f"renaming tag {args.get('from_tag')} → {args.get('to_tag')}…"
    return f"{name}…"


def chat_stream(cfg, store, lock, history: list[dict]):
    """Generator of Server-Sent-Event dicts: {type: delta|status|error|done, ...}.

    Network calls to the model run without `lock`; only the hub's DB tool calls acquire it, so a long
    chat never blocks the rest of the server. The answer streams token-by-token, the model's legacy
    DSML protocol never leaks into the visible text, a dropped connection is retried before any text
    was shown, and the turn always ends with a visible answer instead of an empty bubble."""
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
        sent, sent_len, legacy_mode = [], 0, False
        attempt = 0
        while True:
            try:
                for chunk in _post_stream(ai, messages, tools=not force_answer):
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    ch = (chunk.get("choices") or [{}])[0]
                    delta = ch.get("delta") or {}
                    if delta.get("reasoning_content") and not parts:
                        # DeepSeek's thinking stream: show progress while the model plans and
                        # reads tool results, so a long turn is not a silent pause.
                        yield {"type": "reasoning", "text": delta["reasoning_content"]}
                    if delta.get("content"):
                        parts.append(delta["content"])
                        raw = "".join(parts)
                        if not legacy_mode and _legacy_protocol_started(raw):
                            legacy_mode = True
                        if not legacy_mode:
                            safe = _stream_safe_prefix(raw)
                            if len(safe) > sent_len:
                                text = safe[sent_len:]
                                sent_len = len(safe)
                                sent.append(text)
                                yield {"type": "delta", "text": text}
                    for tc in delta.get("tool_calls") or []:
                        slot = tcs.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                break
            except urllib.error.HTTPError as exc:
                yield {"type": "error", "text": f"HTTP {exc.code} from the AI endpoint"}; return
            except TRANSIENT_ERRORS as exc:
                # Nothing was read yet, so retrying the turn cannot duplicate streamed text.
                if parts or tcs or attempt >= TRANSIENT_RETRIES:
                    yield {"type": "error", "text": _endpoint_failure(exc, attempt + 1)}; return
                attempt += 1
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
            except Exception as exc:
                yield {"type": "error", "text": f"{type(exc).__name__}: {str(exc)[:120]}"}; return
        if usage:
            with lock:
                cost += _record_usage(store, ai, {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0),
                                                  "cache_read": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0), "cache_write": 0})
        raw_content = "".join(parts)
        clean_content, legacy_calls = _legacy_tool_calls(raw_content)
        calls = list(tcs.values())
        if not calls and legacy_calls:
            calls = [{"id": c["id"], "name": c["function"]["name"],
                      "args": c["function"]["arguments"]} for c in legacy_calls]
        messages.append({"role": "assistant", "content": clean_content,
                         "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}} for c in calls] or None})
        if not calls or force_answer:
            visible = "".join(sent)
            if not legacy_mode and len(raw_content) > sent_len:
                text = raw_content[sent_len:]
                sent.append(text)
                yield {"type": "delta", "text": text}
                visible += text
            if not visible.strip() and clean_content:
                yield {"type": "delta", "text": clean_content}
                visible = clean_content
            if not visible.strip():
                # The model returned no prose at all (protocol-only or reasoning-only output).
                # Ask once more for a plain answer, then fall back to a readable message so the
                # chat can never finish with an empty assistant bubble.
                retry_messages = messages[:-1] if calls else messages
                text, extra_usage = _plain_answer(ai, retry_messages)
                if extra_usage:
                    with lock:
                        cost += _record_usage(store, ai, {"input": extra_usage.get("prompt_tokens", 0),
                                                          "output": extra_usage.get("completion_tokens", 0),
                                                          "cache_read": (extra_usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                                                          "cache_write": 0})
                yield {"type": "delta", "text": text or _fallback_answer(changes)}
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
