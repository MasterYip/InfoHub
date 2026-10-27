"""Policy (store / watch / digest / ignore) per channel and priority (P0-P3) per item.

Order of precedence:
  1. dashboard overrides (mute -> ignore, watch -> watch, vip sender -> P0)
  2. per-source lists in config (watch / digest_only / ignore)
  3. `rules:` list, evaluated top to bottom; best priority wins, labels accumulate
"""
from __future__ import annotations

from fnmatch import fnmatch

PRIORITIES = ["P0", "P1", "P2", "P3"]
NEEDS_YOU = {"P0", "P1"}


def channel_policy(source: str, channel: str, name: str, scfg: dict, overrides: dict, muted: bool = False,
                   facts: dict | None = None, cfg: dict | None = None) -> str:
    """Explicit settings win; otherwise the per-source defaults decide (see sync/policy.py)."""
    if (source, channel) in overrides.get("mute", set()):
        return "ignore"
    if (source, channel) in overrides.get("watch", set()) or channel in scfg.get("watch", {}).get("channels", []):
        return "watch"
    if (source, channel) in overrides.get("digest", set()) or channel in scfg.get("digest_only", []):
        return "digest"
    ign = scfg.get("ignore", {})
    if channel in ign.get("channels", []) or any(fnmatch(name or "", p) for p in ign.get("name_patterns", [])):
        return "ignore"
    if ign.get("official_accounts") and channel.startswith("gh_"):
        return "ignore"
    if ign.get("muted") and muted:
        return "ignore"
    if facts is not None and cfg is not None:
        from .policy import default_policy
        return default_policy(source, facts, cfg)
    return scfg.get("default_policy", "store")


# Shared task/obligation signals (also used by the rule organizer). "记得" usually means
# "I recall", not "remember to", so recollections and done-notices are excluded.
import re as _re
NOT_A_TASK = _re.compile(r"(我|你|他|她|不|还|没|都|也|很|真|只|谁|个|中|上)\s*记得|记不得|记得[很好挺太超不是]"
                         r"|已(经)?(提交|完成|结束|审批|发|交)|审批进展|谢谢|感谢", _re.I)
ASK = _re.compile(r"(^|[。！!？?；;，,\n\s])(请|麻烦|务必|必须|记得|别忘|不要忘|帮我|帮忙|需要你|大家)", _re.I)
OBLIGATION = ("截止", "ddl", "deadline", "due", "请于", "务必", "交给", "提交", "作业", "报名", "填写", "上交", "交表")
_AT = _re.compile(r"@\S+\s?")


_TRIVIAL = _re.compile(r"^[\s。.!！?？~、,，]*(ok|okk|好|好的|好滴|收到|可以|对|对的|是的|嗯+|哈+|行|没有|不是|谢谢|感谢|\d{1,2}[:：点]?\d{0,2}[?？]?|👍|[\U0001F000-\U0001FAFF\u2600-\u27BF]+)[\s。.!！?？~、,，]*$", _re.I)


def trivial(it: dict) -> bool:
    """A sticker, system line, or a short acknowledgement that should never be its own alert."""
    if it.get("kind") in ("sticker", "system", "image", "voice", "video"):
        return True
    text = (it.get("text") or "").strip()
    return len(text) < 6 or bool(_TRIVIAL.match(text))


def actionable(it: dict) -> bool:
    """A message is actionable when it states an obligation plus a date or an instruction aimed
    at the reader, and is not someone recalling something or a completed-notice."""
    import json as _json
    text = it.get("text") or ""
    body = _AT.sub("", text)
    if NOT_A_TASK.search(body):
        return False
    low = body.lower()
    f = it.get("flags")
    if isinstance(f, str):
        try:
            f = _json.loads(f)
        except (ValueError, TypeError):
            f = {}
    has_date = bool(f.get("date")) if isinstance(f, dict) else False
    has_obligation = any(k.lower() in low for k in OBLIGATION)
    return has_obligation and (has_date or bool(ASK.search(body)))


def _matches(when: dict, it: dict) -> bool:
    text = (it.get("text") or "").casefold()
    for key, want in when.items():
        if key == "source" and it["source"] not in ([want] if isinstance(want, str) else want):
            return False
        if key == "channels" and it.get("channel") not in want:
            return False
        if key == "channel_names" and not any(fnmatch(it.get("channel_name") or "", p) for p in want):
            return False
        if key == "people" and it.get("sender") not in want and it.get("sender_name") not in want:
            return False
        if key == "keywords" and not any(k.casefold() in text for k in want):
            return False
        if key == "mentions_me" and bool(it.get("mentions_me")) != bool(want):
            return False
        if key == "policy" and it.get("policy") not in ([want] if isinstance(want, str) else want):
            return False
        if key == "is_group" and bool(it.get("is_group")) != bool(want):
            return False
        if key == "actionable" and bool(actionable(it)) != bool(want):
            return False
    return True


def classify(it: dict, rules: list[dict], scfg: dict, overrides: dict) -> dict:
    """Fill priority / labels / rule on an item whose policy is already set."""
    policy = it["policy"]
    best, labels, names = ("P2" if policy == "watch" else "P3"), [], []
    watched_people = scfg.get("watch", {}).get("people", [])
    watch_overrides = overrides.get("watch", set()) | overrides.get("watch_person", set())
    watched_override = ((it.get("source"), it.get("sender")) in watch_overrides or
                        (it.get("source"), it.get("sender_name")) in watch_overrides)
    if it.get("sender") in watched_people or it.get("sender_name") in watched_people or watched_override:
        # A watched sender is a person-level Watch even when the surrounding chat is Store.
        policy = it["policy"] = "watch"
        best = "P1"; names.append("watch-person")
    if it.get("mentions_me"):
        best = min(best, "P1"); labels.append("mention"); names.append("mention")
    for rule in rules:
        if _matches(rule.get("when", {}), it):
            names.append(rule.get("name", "rule"))
            if rule.get("priority"):
                best = min(best, rule["priority"])
            labels += rule.get("extract", []) + rule.get("labels", [])
            if rule.get("summarize_only"):
                best = max(best, "P2")
    if (it["source"], it.get("sender")) in overrides.get("vip", set()) and not trivial(it):
        best = min(best, "P1"); names.append("vip")
    if policy == "digest":
        best = max(best, "P2")  # digest-only channels never interrupt
    if it.get("from_me"):
        best = "P3"
    it.update(priority=best, labels=sorted(set(labels)), rule=",".join(names) or None)
    return it


# @-all style mentions: an announcement to everyone in the group is also "for you".
ALL_MENTIONS = ("@所有人", "@全体成员", "@everyone", "@all", "@All", "@ALL", "@频道成员", "@channel")


def mentions_all(text: str) -> bool:
    return any(a in (text or "") for a in ALL_MENTIONS)


def mentions_me(text: str, my_names: list[str]) -> bool:
    return mentions_all(text) or any(f"@{n}" in (text or "") for n in my_names if n)


def reclassify_recent(cfg, store, days: float = 14) -> dict:
    """Recompute priority on recent messages after the rules change; downgraded ones leave the lane."""
    import json as _json
    since = int(__import__("time").time() - days * 86400)
    overrides = store.overrides()
    rows = store.q("""SELECT id, source, account, channel, channel_name, is_group, sender, sender_name,
                        kind, text, flags, policy, mentions_me, from_me, priority FROM messages
                      WHERE sent_at >= ?""", (since,)).fetchall()
    changed = 0
    for r in rows:
        it = dict(r)
        scfg = cfg["sources"].get(r["source"], {})
        classify(it, cfg["rules"], scfg, overrides)
        if it["priority"] != r["priority"]:
            store.q("UPDATE messages SET priority=?, rule=? WHERE id=?", (it["priority"], it["rule"], r["id"]))
            changed += 1
    store.db.commit()
    return {"checked": len(rows), "reclassified": changed}
