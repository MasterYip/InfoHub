"""Small, persistent scheduler for user-authored assistant prompts."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from ..config import load
from ..store import Store

TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MODES = {"daily", "interval"}


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("schedule must be an object")
    name = str(data.get("name") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    mode = str(data.get("mode") or "daily").lower()
    if not name or len(name) > 120:
        raise ValueError("name must be 1 to 120 characters")
    if not prompt or len(prompt) > 12000:
        raise ValueError("prompt must be 1 to 12000 characters")
    if mode not in MODES:
        raise ValueError("mode must be daily or interval")
    enabled = bool(data.get("enabled", True))
    daily_time = str(data.get("daily_time") or "09:00")
    interval = data.get("interval_minutes", 60)
    if mode == "daily":
        if not TIME_RE.fullmatch(daily_time):
            raise ValueError("daily_time must use HH:MM")
        interval = None
    else:
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= int(interval) <= 10080:
            raise ValueError("interval_minutes must be from 1 to 10080")
        interval = int(interval)
        daily_time = None
    return {"name": name, "enabled": enabled, "mode": mode, "daily_time": daily_time,
            "interval_minutes": interval, "prompt": prompt}


def next_run(data: dict, now: int | None = None, tz: timezone | None = None) -> int:
    now = int(now or time.time())
    if not data["enabled"]:
        return 0
    if data["mode"] == "interval":
        return now + int(data["interval_minutes"]) * 60
    tz = tz or timezone.utc
    hour, minute = map(int, data["daily_time"].split(":"))
    current = datetime.fromtimestamp(now, tz)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return int(target.timestamp())


def save(store, raw: dict, *, sid: int | None = None, now: int | None = None, tz: timezone | None = None) -> dict:
    data = validate(raw)
    if sid is not None and not store.schedule(sid):
        raise ValueError("schedule not found")
    return store.save_schedule(data, next_run(data, now=now, tz=tz), sid=sid)


def run_due(cfg, store, now: int | None = None) -> dict:
    """Run a bounded batch. Each prompt is an ordinary assistant turn with full tool access."""
    now = int(now or time.time())
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    out = {"checked": 0, "completed": 0, "failed": 0}
    for item in store.due_schedules(now):
        out["checked"] += 1
        if not store.mark_schedule_running(item["id"], now):
            continue
        current = load(cfg.get("_path"))
        try:
            from . import chat
            result = chat.chat(current, store, [{"role": "user", "content": item["prompt"]}])
            error = result.get("error") if isinstance(result, dict) else "assistant returned no result"
            status = "error" if error else "ok"
            out["failed" if error else "completed"] += 1
            schedule = validate(item)
            finished = int(time.time())
            store.finish_schedule(item["id"], next_run=next_run(schedule, now=finished, tz=tz), status=status,
                                  error=str(error)[:500] if error else None, now=finished)
        except Exception as exc:
            out["failed"] += 1
            schedule = validate(item)
            finished = int(time.time())
            store.finish_schedule(item["id"], next_run=next_run(schedule, now=finished, tz=tz), status="error",
                                  error=f"{type(exc).__name__}: {str(exc)[:450]}", now=finished)
    return out


def loop(cfg):
    store = Store(cfg["hub"]["database"])
    try:
        while True:
            try:
                run_due(cfg, store)
            except Exception:
                # Keep the service alive; the next pass can recover from transient database/API failures.
                pass
            time.sleep(15)
    finally:
        store.db.close()
