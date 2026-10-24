"""Interface for external / scheduled agents (Codex, Claude Code, agent-team workers).

Lease protocol (same contract as the built-in AI organizer, same apply path):
  python -m infohub agent next [--limit 20]      -> JSON {instructions, schema, jobs: [{job_id, input}], lease_until}
  python -m infohub agent submit results.json    -> apply {"results": [{"job_id", "result": {units, close_units}}]}
                                                    (also accepts JSON Lines, or "-" for stdin)
  python -m infohub agent context [--days 7]     -> JSON with a Markdown brief + counts, for other tasks
HTTP: GET /api/agent/next?limit=20, POST /api/agent/submit, GET /api/agent/context
Leased jobs return to the queue after `agent.lease_minutes` if no result arrives.
File variants kept for batch use: `agent export --out jobs.jsonl` / `agent import results.jsonl`.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta, timezone
from pathlib import Path

from . import prompt
from . import units as U
from .ai_organizer import day_parent


def reclaim(store) -> int:
    """Expired leases go back to 'pending'."""
    n = store.q("UPDATE jobs SET status='pending', updated_at=? WHERE status='exported' AND not_before < ?",
                (int(time.time()), int(time.time()))).rowcount
    store.db.commit()
    return n


def lease(cfg, store, limit: int | None = None) -> dict:
    reclaim(store)
    limit = int(limit or cfg["agent"]["max_jobs"])
    until = int(time.time()) + int(cfg["agent"]["lease_minutes"]) * 60
    jobs = []
    for job in store.q("SELECT * FROM jobs WHERE kind='segment' AND status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall():
        payload, _, _ = prompt.build_input(cfg, store, job["ref_id"], int(cfg["ai"]["max_messages_per_call"]))
        if not payload["messages"]:
            store.q("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (int(time.time()), job["id"]))
            continue
        store.q("UPDATE jobs SET status='exported', not_before=?, updated_at=? WHERE id=?", (until, int(time.time()), job["id"]))
        jobs.append({"job_id": job["id"], "input": payload})
    store.db.commit()
    return {"instructions": prompt.SYSTEM, "schema": prompt.SCHEMA, "jobs": jobs, "lease_until": until,
            "submit": 'python -m infohub agent submit <file>  with {"results": [{"job_id": <id>, "result": <object matching schema>}]}'}


def _records(text: str) -> list:
    text = text.strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data.get("results", [data] if "job_id" in data else [])
            return data
        except json.JSONDecodeError:
            pass
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append(None)
    return out


def submit(cfg, store, text: str, origin: str = "agent") -> dict:
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    out = {"applied": 0, "rejected": 0, "created": 0, "updated": 0, "closed": 0, "errors": []}
    for rec in _records(text):
        try:
            job = store.q("SELECT * FROM jobs WHERE id=?", (int(rec["job_id"]),)).fetchone()
            result = rec["result"] if isinstance(rec["result"], dict) else prompt.parse(rec["result"])
            if not job or job["status"] == "done" or not isinstance(result.get("units"), list):
                raise ValueError("unknown or finished job, or malformed result")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            out["rejected"] += 1
            out["errors"].append(str(exc)[:120])
            continue
        _, allowed, seg = prompt.build_input(cfg, store, job["ref_id"], 10**6)
        src = "source:" + seg["source"] + (f":{seg['account']}" if seg["account"] else "")
        r = U.apply_result(store, result, origin=origin, job_key=f"{origin}:seg{seg['id']}", tz=tz,
                           allowed_message_ids=allowed, default_parent=day_parent(store, seg),
                           base_tags=[src, f"channel:{seg['channel_name']}"])
        store.q("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (int(time.time()), job["id"]))
        store.q("UPDATE segments SET ai_done=1 WHERE id=?", (seg["id"],))
        out["applied"] += 1
        for k in ("created", "updated", "closed"):
            out[k] += r[k]
    store.db.commit()
    out["errors"] = out["errors"][:10]
    return out


def export(cfg, store, path: Path, limit: int = 20) -> dict:
    data = lease(cfg, store, limit)
    with open(path, "w", encoding="utf-8") as fh:
        for j in data["jobs"]:
            fh.write(json.dumps({"job_id": j["job_id"], "instructions": data["instructions"], "schema": data["schema"],
                                 "input": j["input"]}, ensure_ascii=False) + "\n")
    return {"exported": len(data["jobs"]), "file": str(path), "lease_until": data["lease_until"]}


def import_results(cfg, store, path: Path) -> dict:
    return submit(cfg, store, Path(path).read_text(encoding="utf-8"))


def reset(store) -> int:
    n = store.q("UPDATE jobs SET status='pending', not_before=0 WHERE status='exported'").rowcount
    store.db.commit()
    return n
