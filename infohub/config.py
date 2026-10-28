"""Load config/hub.local.yaml (private) over built-in defaults."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "hub": {
        "timezone_offset_hours": 8,
        "poll_minutes": 5,
        "initial_backfill_hours": 24,
        "dashboard": {"bind": "127.0.0.1", "port": 8765,
                      "allowed_hosts": [],        # extra Host values, e.g. ["203.0.113.5:18765", "hub.example.com"]; "*" = any
                      "tls_cert": "", "tls_key": ""},   # PEM files -> HTTPS (and Secure cookies)
        "viewer": {"enabled": False, "bind": "127.0.0.1", "port": 8766},  # anonymous, read-only Explorer snapshot
        "auth": {"mode": "auto",                  # auto: login required once a user exists | on | off
                 "users_file": "config/users.local.json",
                 "session_days": 30,
                 "cookie_secure": False,          # force Secure cookies (set true when HTTPS terminates at a proxy)
                 "trust_forwarded_for": False},   # use X-Forwarded-For for login throttling behind your own proxy
        "database": "data/hub.db",
        "backup": {"enabled": True, "interval_days": 7, "directory": "data/backups"},
        "retention_days": 90,
    },
    "me": {"names": []},            # your display names, for @mention detection and the "mine" filter
    # ---------------- Layer 1: sync (no AI) ----------------
    # Source config and per-source policy defaults come from the plugins in infohub/sources/
    # (merged in load()). Deleting a source folder removes it cleanly.
    "sources": {},
    "rules": [],                      # priority rules (Layer 1 triage)
    # Default policy per chat, from its own properties. First match wins; anything you set
    # by hand always wins. Per-source rule lists come from each source plugin.
    "policy_defaults": {},        # per-source rule lists come from each source plugin
    "segment_gap_minutes": 12,        # a pause this long ends a conversation -> it is organized (configurable)
    "segment_max_messages": 80,       # and a segment never grows past this, so nothing is truncated
    # ---------------- Layer 2: reorganization ----------------
    "organize": {
        "rule_organizer": True,
        # In chats the AI organizes, the rules would add one small unit per message next to the
        # AI's broader topics. There they only keep the time-critical kinds as a safety net.
        "rule_kinds_when_ai": ["todo", "event"],
        "polish": True,               # let the AI rewrite rule-made titles into brief, plain ones       # deterministic units: todos, events, announcements, resources, questions
        "todo_keywords": ["作业", "截止", "DDL", "ddl", "deadline", "提交", "请于", "务必", "记得", "due", "todo", "TODO", "交给", "完成"],
        "event_keywords": ["会议", "meeting", "组会", "讲座", "seminar", "答辩", "报告会", "活动", "面试", "开会", "talk"],
        "announcement_markers": ["@所有人", "@All", "【通知】", "通知", "公告", "notice"],
        "tag_rules": [],              # [{when: {...}, tags: [course:xxx, type:homework]}]
    },
    "ai": {
        "enabled": False,             # Layer 2 AI organizer; Layer 1 and 3 work without it
        "provider": "openai_compatible",   # openai_compatible (DeepSeek etc.) | anthropic
        "base_url": "https://api.deepseek.com",
        "proxy": "direct",            # direct (never use the system proxy) | auto (OS settings) | http://host:port
        "model": "deepseek-flash",
        "api_key_env": "DEEPSEEK_API_KEY",  # name of the environment variable holding the key
        "price_usd_per_mtok": {"input": 0.3, "output": 1.2, "cache_read": 0.03},  # for the budget; set to your provider's prices
        "effort": "low",              # anthropic only
        "max_tokens": 16000,
        "timeout_seconds": 180,
        "policies": ["watch"],        # which channel policies may send text to the API
        "min_messages": 2,
        "max_messages_per_call": 150,
        "daily_budget_usd": 1.0,
        "interval_minutes": 15,
        "max_jobs_per_run": 20,
        "polish_policies": ["watch", "store"],   # whose titles may be sent for polishing
        "polish_batch": 25,
        "queue_for_agents": False,    # queue segments for external agents even when the API worker is off
    },
    # ---------------- scheduled external agents (Codex, Claude Code, ...) ----------------
    "agent": {
        "command": "",                # e.g. 'codex exec --full-auto' ; the prompt file path is appended
        "prompt": "agents/scheduled-run.md",
        "max_jobs": 30,
        "lease_minutes": 60,          # exported jobs return to the queue if no result arrives in time
    },
    # ---------------- Layer 3: applications ----------------
    "apps": {
        "desktop": {"notifications": True, "mine_only": False, "poll_seconds": 5},
        "today_scope": "mine",        # Today tab default: mine | all
        "reminders": {"enabled": True, "toast": True, "todo_lead_hours": 24, "event_lead_minutes": 60},
        "views": [],                  # saved tag filters shown as dashboard tabs
    },
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


SETTINGS_FILE = ROOT / "config" / "settings.local.json"

# Keys the settings page may write (everything else stays in the YAML file, with its comments).
EDITABLE = ["hub.backup.enabled", "hub.backup.interval_days", "me.names", "hub.poll_minutes", "hub.retention_days", "apps.today_scope",
            "apps.reminders.enabled", "apps.reminders.toast", "apps.reminders.todo_lead_hours",
            "apps.reminders.event_lead_minutes", "organize.polish", "ai.enabled", "ai.model",
            "ai.daily_budget_usd", "ai.interval_minutes", "ai.policies", "ai.polish_policies",
            "segment_max_messages", "segment_gap_minutes"]


def read_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def write_settings(flat: dict) -> dict:
    """Save a flat {"ai.enabled": true} mapping; unknown keys are refused."""
    bad = [k for k in flat if k not in EDITABLE]
    if bad:
        raise ValueError(f"not editable: {', '.join(sorted(bad))}")
    if "hub.backup.enabled" in flat and not isinstance(flat["hub.backup.enabled"], bool):
        raise ValueError("Backup enabled must be true or false")
    if "hub.backup.interval_days" in flat:
        days = flat["hub.backup.interval_days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise ValueError("Backup interval must be a whole number of days from 1 to 3650")
    data = {**read_settings(), **flat}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
    return data


def _nest(flat: dict) -> dict:
    out: dict = {}
    for key, value in flat.items():
        node = out
        *parents, last = key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[last] = value
    return out


def load(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config" / "hub.local.yaml"
    if not path.exists():
        raise SystemExit(f"config not found: {path}. Copy config/hub.example.yaml to config/hub.local.yaml.")
    cfg = copy.deepcopy(DEFAULTS)
    from .sources import discover                     # lazy: avoids an import cycle
    for name, mod in discover().items():
        cfg["sources"][name] = copy.deepcopy(getattr(mod, "DEFAULT_CONFIG", {}))
        if getattr(mod, "POLICY_DEFAULTS", None):
            cfg["policy_defaults"][name] = copy.deepcopy(mod.POLICY_DEFAULTS)
    cfg = _merge(cfg, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    cfg = _merge(cfg, _nest(read_settings()))        # settings page overrides the YAML file
    cfg["_path"] = str(path)
    db = Path(cfg["hub"]["database"])
    cfg["hub"]["database"] = str(db if db.is_absolute() else ROOT / db)
    return cfg
