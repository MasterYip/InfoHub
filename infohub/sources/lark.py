"""Lark / Feishu collector via the official lark-cli (user identity, read-only commands).

Several organizations are supported: each `sources.lark.accounts[]` entry names a lark-cli
profile (one app + OAuth login per organization). One-time per machine and organization:
    lark-cli config init --new --name <profile>
    lark-cli --profile <profile> auth login --domain im,calendar,task,contact
Chats are listed by recent activity; a poll walks them in that order and stops after
`stop_after_quiet` chats in a row with nothing new.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..config import ROOT
from ..sync.preprocess import flags
from ..sync.triage import channel_policy, classify, mentions_me

NAME = "lark"
DEFAULT_CONFIG = {
    "enabled": False,
    "cli": "lark-cli",
    "accounts": [{"name": "lark", "profile": None}],   # one entry per Lark/Feishu organization
    "default_policy": "store",
    "watch": {"channels": [], "people": []},
    "digest_only": [],
    "ignore": {"channels": [], "name_patterns": [], "muted": True},
    "calendar_days": 7,
    "max_chats_per_poll": 60,
    "stop_after_quiet": 6,
    "parallel": 4,
}
POLICY_DEFAULTS = [
    {"when": {"is_group": False}, "policy": "watch"},
    {"when": {"chat_mode": "topic"}, "policy": "watch"},
    {"when": {"name_matches": ["*课*", "*班*", "*class*", "*course*"]}, "policy": "watch"},
    {"when": {"muted": True}, "policy": "ignore"},
    {"policy": "watch"},
]


class LarkError(RuntimeError):
    pass


def _cli(cfg) -> list[str]:
    exe = cfg["sources"]["lark"]["cli"]
    # The npm .cmd -> Node wrapper spawns another console executable without
    # windowsHide. Call its installed binary directly for background sync.
    native = ROOT / "node_modules" / "@larksuite" / "cli" / "bin" / "lark-cli.exe"
    if exe == "lark-cli" and os.name == "nt" and native.is_file():
        return [str(native)]
    local = ROOT / "node_modules" / ".bin" / ("lark-cli.cmd" if os.name == "nt" else "lark-cli")
    if exe == "lark-cli" and local.exists():
        exe = str(local)
    return [exe]


def run(cfg, account: dict | None, *args, timeout=120) -> dict:
    prof = ["--profile", account["profile"]] if account and account.get("profile") else []
    proc = subprocess.run(_cli(cfg) + list(args) + prof, capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        data = json.loads(proc.stdout or proc.stderr or "{}")
    except json.JSONDecodeError:
        raise LarkError(f"lark-cli returned non-JSON (exit {proc.returncode})") from None
    if not data.get("ok", proc.returncode == 0):
        err = data.get("error", {})
        raise LarkError(f"{err.get('type')}/{err.get('subtype')}: {err.get('message', '')[:160]}")
    return data


def _tz(cfg):
    return timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))


def _ts(value: str, tz) -> int:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=tz).timestamp())
        except ValueError:
            pass
    if value.isdigit():
        v = int(value)
        return v // 1000 if v > 10**11 else v
    return int(datetime.fromisoformat(value).timestamp())


def chats(cfg, account, exclude_muted=False) -> list[dict]:
    args = ["im", "+chat-list", "--as", "user", "--types", "group,p2p", "--sort", "active_time",
            "--page-size", "100", "--page-all", "--page-limit", "10"]
    return run(cfg, account, *(args + (["--exclude-muted"] if exclude_muted else [])))["data"]["chats"]


def messages(cfg, account, chat_id: str, start_iso: str) -> list[dict]:
    data = run(cfg, account, "im", "+chat-messages-list", "--as", "user", "--chat-id", chat_id, "--start", start_iso,
               "--order", "asc", "--no-reactions", "--page-all", "--page-limit", "10")
    return data["data"].get("messages") or []


def poll_account(cfg, store, overrides, account: dict, now=None) -> tuple[dict, list[int]]:
    scfg = cfg["sources"]["lark"]
    acct = account["name"]
    tz, tzh = _tz(cfg), cfg["hub"]["timezone_offset_hours"]
    now = int(now or time.time())
    key = f"lark:{acct}"
    since = store.cursor(key) or now - int(cfg["hub"]["initial_backfill_hours"]) * 3600
    start_iso = datetime.fromtimestamp(since - 180, tz).isoformat(timespec="seconds")
    all_chats = chats(cfg, account)
    muted = set()
    if scfg["ignore"].get("muted"):
        try:
            muted = {c["chat_id"] for c in all_chats} - {c["chat_id"] for c in chats(cfg, account, exclude_muted=True)}
        except LarkError:
            pass
    stats = {"chats": len(all_chats), "checked": 0, "stored": 0, "ignored_chats": 0}
    plan = []
    for c in all_chats[: int(scfg["max_chats_per_poll"])]:
        name = c.get("name") or c["chat_id"]
        is_group = c.get("chat_mode") != "p2p"
        is_muted = c["chat_id"] in muted
        facts = {"is_group": is_group, "muted": is_muted, "chat_mode": c.get("chat_mode"),
                 "name": name, "last_at": None}
        policy = channel_policy("lark", c["chat_id"], name, scfg, overrides, muted=is_muted, facts=facts, cfg=cfg)
        store.upsert_channel("lark", acct, c["chat_id"], name, is_group, policy,
                             muted=is_muted, chat_mode=c.get("chat_mode"))
        if policy == "ignore":
            stats["ignored_chats"] += 1
        else:
            plan.append((c, name, policy, is_group))
    newest, quiet, new_ids = since, 0, []
    batch = int(scfg.get("parallel", 4))
    with ThreadPoolExecutor(max_workers=batch) as pool:
        for i in range(0, len(plan), batch):  # walk in activity order, stop when it goes quiet
            chunk = plan[i:i + batch]
            results = list(pool.map(lambda p: messages(cfg, account, p[0]["chat_id"], start_iso), chunk))
            for (c, name, policy, is_group), msgs in zip(chunk, results):
                stats["checked"] += 1
                msgs = [m for m in msgs if not m.get("deleted")]
                quiet = quiet + 1 if not msgs else 0
                items = []
                for m in msgs:
                    sent = _ts(m["create_time"], tz)
                    newest = max(newest, sent)
                    snd = m.get("sender") or {}
                    text = m.get("content") or f"[{m.get('msg_type')}]"
                    it = {"source": "lark", "account": acct, "item_key": m["message_id"], "channel": c["chat_id"],
                          "channel_name": name, "is_group": is_group, "sender": snd.get("id"), "sender_name": snd.get("name"),
                          "sent_at": sent, "kind": m.get("msg_type"), "text": text, "link": m.get("message_app_link"),
                          "mentions_me": mentions_me(text, cfg["me"]["names"]), "policy": policy,
                          "flags": flags(text, m.get("msg_type") or "", sent, tzh)}
                    items.append(classify(it, cfg["rules"], scfg, overrides))
                store.upsert_channel("lark", acct, c["chat_id"], name, is_group, policy, None,
                                     max([x["sent_at"] for x in items], default=None))
                ids = store.add_messages(items)
                new_ids += ids
                stats["stored"] += len(ids)
            if quiet >= int(scfg.get("stop_after_quiet", 6)):
                break
    store.db.commit()
    store.set_cursor(key, min(newest, now))
    return stats, new_ids


def calendar(cfg, store, account: dict) -> dict:
    tz = _tz(cfg)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=int(cfg["sources"]["lark"]["calendar_days"]))
    try:
        data = run(cfg, account, "calendar", "+agenda", "--as", "user", "--start", today.isoformat(), "--end", end.isoformat())
    except LarkError as exc:
        if "missing_scope" in str(exc):          # organization has not approved calendar access (yet)
            return {"events": 0, "waiting": "calendar permission not granted; re-run auth login after admin approval"}
        raise
    raw = data.get("data")
    evs = raw if isinstance(raw, list) else (raw or {}).get("events") or (raw or {}).get("items") or []

    def _t(v):
        if isinstance(v, dict):
            v = v.get("timestamp") or v.get("date_time") or v.get("date")
        return _ts(str(v), tz) if v else None

    out = []
    for e in evs:
        loc = e.get("location")
        out.append({"item_key": e.get("event_id") or e.get("id") or str(len(out)),
                    "title": e.get("summary") or e.get("title") or "(no title)",
                    "start_at": _t(e.get("start_time") or e.get("start") or {}), "end_at": _t(e.get("end_time") or e.get("end") or {}),
                    "location": loc.get("name") if isinstance(loc, dict) else loc, "link": e.get("app_link")})
    store.replace_events("lark", account["name"], out)
    return {"events": len(out)}


def refresh_channels(cfg, store, account: dict) -> dict:
    """Re-read mute state and chat mode for every chat of one organization."""
    all_chats = chats(cfg, account)
    muted = set()
    if cfg["sources"]["lark"]["ignore"].get("muted"):
        try:
            muted = {c["chat_id"] for c in all_chats} - {c["chat_id"] for c in chats(cfg, account, exclude_muted=True)}
        except LarkError:
            pass
    for c in all_chats:
        store.q("""UPDATE channels SET channel_name=?, is_group=?, muted=?, chat_mode=?
                   WHERE source='lark' AND account=? AND channel=?""",
                (c.get("name") or c["chat_id"], int(c.get("chat_mode") != "p2p"), int(c["chat_id"] in muted),
                 c.get("chat_mode"), account["name"], c["chat_id"]))
    store.db.commit()
    return {"refreshed": len(all_chats), "muted": len(muted)}


def sync(cfg, store, overrides, record) -> None:
    for acct in cfg["sources"]["lark"]["accounts"]:
        started = int(time.time())
        try:
            stats, ids = poll_account(cfg, store, overrides, acct)
            record(f"lark:{acct['name']}", stats, ids, started)
        except Exception as exc:
            record(f"lark:{acct['name']}", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, (), started)
        if acct.get("calendar", True):
            started = int(time.time())
            try:
                record(f"lark-calendar:{acct['name']}", calendar(cfg, store, acct), (), started)
            except Exception as exc:
                record(f"lark-calendar:{acct['name']}", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, (), started)


def refresh(cfg, store, record) -> None:
    for acct in cfg["sources"]["lark"]["accounts"]:
        started = int(time.time())
        try:
            record(f"lark-refresh:{acct['name']}", refresh_channels(cfg, store, acct), (), started)
        except Exception as exc:
            record(f"lark-refresh:{acct['name']}", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, (), started)


def doctor(cfg) -> dict:
    out = {}
    for acct in cfg["sources"]["lark"]["accounts"]:
        try:
            out[acct["name"]] = {"chats": len(chats(cfg, acct))}
        except Exception as exc:
            out[acct["name"]] = {"error": str(exc)[:200]}
    return out
