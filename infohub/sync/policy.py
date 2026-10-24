"""Default chat policy from the chat's own properties (Layer 1).

Rules are evaluated in order per source; the first match wins. Conditions:
  is_group, muted, chat_mode ("group"|"p2p"|"topic"), name_matches [glob...],
  active_within_days N, min_messages N
A rule with no `when` is the fallback. Anything you set by hand (dashboard or config
lists) always wins over these defaults - see triage.channel_policy.
"""
from __future__ import annotations

import time
from fnmatch import fnmatch

POLICIES = ("watch", "store", "digest", "ignore")

def matches(when: dict, facts: dict, now: int) -> bool:
    for key, want in (when or {}).items():
        if key == "is_group" and bool(facts.get("is_group")) != bool(want):
            return False
        if key == "muted":
            if facts.get("muted") is None or bool(facts["muted"]) != bool(want):
                return False
        if key == "chat_mode" and facts.get("chat_mode") != want:
            return False
        if key == "name_matches" and not any(fnmatch(facts.get("name") or "", p) for p in want):
            return False
        if key == "active_within_days":
            last = facts.get("last_at") or 0
            if not last or last < now - float(want) * 86400:
                return False
        if key == "min_messages" and (facts.get("recent") or 0) < int(want):
            return False
    return True


def default_policy(source: str, facts: dict, cfg: dict, now: int | None = None) -> str:
    rules = (cfg.get("policy_defaults") or {}).get(source) or []
    now = int(now or time.time())
    for rule in rules:
        if matches(rule.get("when"), facts, now):
            policy = rule.get("policy")
            return policy if policy in POLICIES else "store"
    return cfg["sources"].get(source, {}).get("default_policy", "store")


def channel_facts(row, cfg: dict) -> dict:
    """Build facts from a stored channels row."""
    muted = row["muted"] if "muted" in row.keys() else None
    return {"is_group": row["is_group"], "muted": None if muted is None else bool(muted),
            "chat_mode": row["chat_mode"] if "chat_mode" in row.keys() else None,
            "name": row["channel_name"], "last_at": row["last_at"], "recent": row["recent"] if "recent" in row.keys() else None}


def plan(store, cfg, only_source: str | None = None) -> list[dict]:
    """What the defaults would change. Uses the same decision path as live sync, so
    dashboard choices, config lists and the official-account rule all still apply."""
    from .triage import channel_policy
    overrides = store.overrides()
    now = int(time.time())
    since = now - 30 * 86400
    rows = store.q("""SELECT c.*, (SELECT count(*) FROM messages m WHERE m.source=c.source AND m.account=c.account
                        AND m.channel=c.channel AND m.sent_at>=?) AS recent FROM channels c""", (since,)).fetchall()
    out = []
    for r in rows:
        if only_source and r["source"] != only_source:
            continue
        scfg = cfg["sources"].get(r["source"], {})
        facts = channel_facts(r, cfg)
        want = channel_policy(r["source"], r["channel"], r["channel_name"], scfg, overrides,
                              muted=bool(facts.get("muted")), facts=facts, cfg=cfg)
        if want != r["policy"]:
            out.append({"source": r["source"], "account": r["account"], "channel": r["channel"],
                        "channel_name": r["channel_name"], "is_group": r["is_group"], "muted": r["muted"],
                        "recent": r["recent"], "last_at": r["last_at"], "from": r["policy"], "to": want})
    return out


def apply(store, cfg, only_source: str | None = None, dry_run: bool = True) -> dict:
    changes = plan(store, cfg, only_source)
    summary: dict[str, int] = {}
    for ch in changes:
        summary[f"{ch['from']} -> {ch['to']}"] = summary.get(f"{ch['from']} -> {ch['to']}", 0) + 1
        if not dry_run:
            store.q("UPDATE channels SET policy=? WHERE source=? AND account=? AND channel=?",
                    (ch["to"], ch["source"], ch["account"], ch["channel"]))
            store.q("UPDATE messages SET policy=? WHERE source=? AND account=? AND channel=?",
                    (ch["to"], ch["source"], ch["account"], ch["channel"]))
    if not dry_run:
        store.db.commit()
    return {"changes": len(changes), "applied": not dry_run, "summary": summary,
            "sample": [{k: c[k] for k in ("channel_name", "from", "to", "recent")} for c in changes[:15]]}
