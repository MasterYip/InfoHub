"""Layer 3 app: a Markdown brief of the info graph for other agents (agent-team tasks) or for you.

  python -m infohub brief [--days 7] [--tags course:ai-engineering,...]   -> stdout Markdown
  GET /api/brief?days=7&tags=...
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from ..organize import units as U


def _fmt(ts, tz):
    return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M") if ts else ""


def brief(cfg, store, days: int = 7, tags=()) -> str:
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    now = int(time.time())
    since = now - days * 86400
    out = [f"# Info brief ({datetime.now(tz):%Y-%m-%d %H:%M}, last {days} days"
           + (f", tags: {', '.join(tags)}" if tags else "") + ")", ""]

    def line(u):
        when = _fmt(u.get("due_at") or u.get("start_at"), tz)
        tg = " ".join(t for t in u["tags"] if not t.startswith(("type:", "source:", "person:")))
        return f"- [{u['id']}] {u['title']}" + (f" — **{when}**" if when else "") + (f"  `{tg}`" if tg else "")

    todos = U.query(store, tags, kind="todo", status="open", order="due", limit=100)
    out += ["## Open todos", *([line(u) for u in todos] or ["- none"]), ""]
    events = [u for u in U.query(store, tags, kind="event", status="open", order="due", limit=100)
              if (u["start_at"] or 0) >= now - 3600]
    out += ["## Upcoming events", *([line(u) for u in events] or ["- none"]), ""]
    for kind, head in (("decision", "Decisions"), ("topic", "Active topics"), ("question", "Questions to answer"),
                       ("announcement", "Announcements")):
        items = U.query(store, tags, kind=kind, since=since, limit=40)
        if items:
            out += [f"## {head}"]
            for u in items:
                out.append(line(u) + (f"\n  {u['summary']}" if u.get("summary") else ""))
            out.append("")
    out.append("Unit ids can be expanded with GET /api/unit/<id>?depth=2.")
    return "\n".join(out)
