"""Layer 1 entry point: one sync round over every enabled source. Never calls an AI service.

Sources are discovered as plugins (see infohub/sources/). The runner itself knows nothing
about any source specifically; it just drives each enabled source's sync().
"""
from __future__ import annotations

import time

from .preprocess import assign_segments, close_segments


def _recorder(store, out, new_ids):
    def record(step, stats, ids=(), started=None):
        started = started or int(time.time())
        status = "waiting" if stats.get("waiting") else ("error" if stats.get("error") else "ok")
        store.log("sync", step, started, status, stats.get("stored", len(ids)), stats,
                  error=stats.get("waiting") or stats.get("error"))
        out[step] = stats
        new_ids.extend(ids or [])
    return record


def sync_once(cfg, store, only: str | None = None) -> dict:
    from ..sources import discover
    overrides = store.overrides()
    out, new_ids = {}, []
    record = _recorder(store, out, new_ids)
    for name, mod in discover().items():
        if only and only != name:
            continue
        if not cfg["sources"].get(name, {}).get("enabled"):
            continue
        try:
            mod.sync(cfg, store, overrides, record)
        except Exception as exc:                 # a broken source never stops the others
            record(name, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    gap, cap = int(cfg["segment_gap_minutes"]), int(cfg["segment_max_messages"])
    # also pick up messages stored by an interrupted earlier run, so none is left unorganized
    orphans = [r[0] for r in store.q("SELECT id FROM messages WHERE segment_id IS NULL ORDER BY id LIMIT 5000")]
    out["segments"] = {"new": assign_segments(store, sorted(set(new_ids) | set(orphans)), gap, cap),
                       "closed": close_segments(store, gap, max_messages=cap),
                       "recovered": len(set(orphans) - set(new_ids))}
    store.prune(int(cfg["hub"]["retention_days"]))
    return out
