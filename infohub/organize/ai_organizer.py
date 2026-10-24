"""Layer 2, AI organizer: turns queued conversation segments into main-idea info units.

Isolation from Layer 1:
- runs from its own loop/thread (or `python -m infohub organize --ai`), never inside sync;
- every failure stays in the `jobs` table (retry with backoff, or failed with the reason);
- a daily USD budget stops calls; without a key it simply reports and idles.
Only segments whose chat policy is listed in `ai.policies` are ever queued (see rule_organizer).
The provider (DeepSeek / any OpenAI-compatible endpoint, or Claude) is chosen in config: see providers.py.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta, timezone

from . import prompt, providers
from . import units as U


def spent_today(store) -> float:
    row = store.q("SELECT coalesce(sum(cost_usd),0) FROM llm_usage WHERE day=?", (date.today().isoformat(),)).fetchone()
    return float(row[0])


def _record_usage(store, ai: dict, usage: dict) -> float:
    p = ai.get("price_usd_per_mtok") or {}
    pin, pout = float(p.get("input", 0)), float(p.get("output", 0))
    pcr = float(p.get("cache_read", pin * 0.1))
    cost = (usage.get("input", 0) * pin + usage.get("output", 0) * pout + usage.get("cache_read", 0) * pcr
            + usage.get("cache_write", 0) * pin * 1.25) / 1e6
    store.q("""INSERT INTO llm_usage VALUES(?,?,1,?,?,?,?,?) ON CONFLICT(day, model) DO UPDATE SET
               requests=requests+1, input_tokens=input_tokens+excluded.input_tokens, output_tokens=output_tokens+excluded.output_tokens,
               cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens, cache_write_tokens=cache_write_tokens+excluded.cache_write_tokens,
               cost_usd=cost_usd+excluded.cost_usd""",
            (date.today().isoformat(), ai["model"], usage.get("input", 0), usage.get("output", 0),
             usage.get("cache_read", 0), usage.get("cache_write", 0), cost))
    store.db.commit()
    return cost


def _finish(store, job_id, status, error=None, delay=0):
    store.q("UPDATE jobs SET status=?, last_error=?, attempts=attempts+1, not_before=?, updated_at=? WHERE id=?",
            (status, error, int(time.time()) + delay, int(time.time()), job_id))
    store.db.commit()


def day_parent(store, seg) -> int | None:
    row = store.q("SELECT id FROM units WHERE key LIKE ? ORDER BY id DESC LIMIT 1",
                  (f"day:{seg['source']}:{seg['account']}:{seg['channel']}:%",)).fetchone()
    return row["id"] if row else None


def process_job(cfg, store, provider, job) -> dict:
    ai = cfg["ai"]
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    limit = int(ai["max_messages_per_call"])
    if job["attempts"] and "max_tokens" in (job["last_error"] or ""):
        limit = max(20, limit // (2 ** job["attempts"]))     # answer did not fit: send a smaller slice
    payload, allowed, seg = prompt.build_input(cfg, store, job["ref_id"], limit)
    if not payload["messages"]:
        _finish(store, job["id"], "done")
        return {"empty": True}
    backoff = min(3600, 60 * 2 ** job["attempts"])
    try:
        reply = provider.complete(payload)
    except providers.ProviderError as exc:
        if exc.fatal:
            raise
        _finish(store, job["id"], "pending" if exc.retryable else "failed", str(exc)[:200], backoff if exc.retryable else 0)
        return {"retry": str(exc)} if exc.retryable else {"failed": str(exc)}
    cost = _record_usage(store, ai, reply.usage)
    if reply.stop == "max_tokens" and job["attempts"] < 3:
        _finish(store, job["id"], "pending", "max_tokens", 60)
        return {"retry": "max_tokens", "cost": cost}
    if reply.stop != "end":
        _finish(store, job["id"], "failed", reply.stop)
        return {"failed": reply.stop, "cost": cost}
    try:
        result = prompt.parse(reply.text)
    except (ValueError, json.JSONDecodeError) as exc:
        _finish(store, job["id"], "pending", f"invalid JSON: {exc}"[:200], backoff)
        return {"retry": "json", "cost": cost}
    source_tag = "source:" + seg["source"] + (f":{seg['account']}" if seg["account"] else "")
    applied = U.apply_result(store, result, origin="ai", job_key=f"ai:seg{seg['id']}", tz=tz,
                             allowed_message_ids=allowed, default_parent=day_parent(store, seg),
                             base_tags=[source_tag, f"channel:{seg['channel_name']}"])
    store.q("UPDATE segments SET ai_done=1 WHERE id=?", (seg["id"],))
    _finish(store, job["id"], "done")
    return {**applied, "cost": cost}


def requeue(cfg, store, days: float) -> int:
    """Queue already-closed segments from the last `days` (e.g. right after enabling AI)."""
    since, now, n = int(time.time() - days * 86400), int(time.time()), 0
    for s in store.q("SELECT id, policy, n FROM segments WHERE closed=1 AND ai_done=0 AND end_at>=?", (since,)).fetchall():
        if s["policy"] in cfg["ai"]["policies"] and s["n"] >= int(cfg["ai"]["min_messages"]):
            n += store.q("""INSERT INTO jobs(kind, ref_id, status, created_at, updated_at) VALUES('segment',?,'pending',?,?)
                            ON CONFLICT(kind, ref_id) DO NOTHING""", (s["id"], now, now)).rowcount
    store.db.commit()
    return n


def run(cfg, store, provider=None) -> dict:
    ai = cfg["ai"]
    if not ai["enabled"]:
        return {"skipped": "ai disabled"}
    if spent_today(store) >= float(ai["daily_budget_usd"]):
        return {"skipped": "daily budget reached", "spent": round(spent_today(store), 4)}
    jobs = store.q("""SELECT * FROM jobs WHERE kind='segment' AND status='pending' AND not_before<=?
                      ORDER BY id LIMIT ?""", (int(time.time()), int(ai["max_jobs_per_run"]))).fetchall()
    if not jobs:
        return {"jobs": 0}
    try:
        provider = provider or providers.make(ai)
    except providers.ProviderError as exc:
        return {"error": str(exc)}
    out = {"jobs": 0, "created": 0, "updated": 0, "closed": 0, "retry": 0, "failed": 0, "cost": 0.0}
    for job in jobs:
        if spent_today(store) >= float(ai["daily_budget_usd"]):
            out["stopped"] = "budget"
            break
        try:
            r = process_job(cfg, store, provider, job)
        except providers.ProviderError as exc:  # fatal: auth/model/balance -> stop this run, jobs stay pending
            out["error"] = str(exc)
            break
        out["jobs"] += 1
        for k in ("created", "updated", "closed"):
            out[k] += r.get(k, 0)
        out["retry"] += "retry" in r
        out["failed"] += "failed" in r
        out["cost"] += r.get("cost", 0.0)
    out["cost"] = round(out["cost"], 5)
    return out
