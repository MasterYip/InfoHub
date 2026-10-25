"""Layer 3 app: reminders for due todos and upcoming events (Windows toast notifications).

Each unit is reminded once (`units.reminded_at`). Text is passed to PowerShell through
environment variables, never interpolated into the script.
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

PS = r"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$null = $t.Item(0).AppendChild($x.CreateTextNode($env:INFOHUB_T1))
$null = $t.Item(1).AppendChild($x.CreateTextNode($env:INFOHUB_T2))
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def toast(title: str, body: str) -> bool:
    if os.name != "nt":
        return False
    env = {**os.environ, "INFOHUB_T1": title[:120], "INFOHUB_T2": body[:240]}
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS], env=env,
                       capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


def due(cfg, store, now=None) -> list[dict]:
    rc = cfg["apps"]["reminders"]
    now = int(now or time.time())
    rows = store.q("""SELECT id, kind, title, due_at, start_at FROM units WHERE status='open' AND reminded_at IS NULL AND (
                        (kind='todo' AND due_at IS NOT NULL AND due_at BETWEEN ? AND ?) OR
                        (kind='event' AND start_at IS NOT NULL AND start_at BETWEEN ? AND ?))
                      ORDER BY coalesce(due_at, start_at)""",
                   (now - 3600, now + int(rc["todo_lead_hours"]) * 3600,
                    now - 600, now + int(rc["event_lead_minutes"]) * 60)).fetchall()
    return [dict(r) for r in rows]


def run(cfg, store, now=None, notify=toast) -> dict:
    rc = cfg["apps"]["reminders"]
    if not rc["enabled"]:
        return {"skipped": True}
    tz = timezone(timedelta(hours=cfg["hub"]["timezone_offset_hours"]))
    sent = 0
    for u in due(cfg, store, now):
        when = datetime.fromtimestamp(u["due_at"] or u["start_at"], tz).strftime("%m-%d %H:%M")
        label = "Due" if u["kind"] == "todo" else "Starts"
        ok = notify(f"{label} {when}", u["title"]) if rc["toast"] else True
        if ok:
            store.q("UPDATE units SET reminded_at=? WHERE id=?", (int(time.time()), u["id"]))
            sent += 1
    store.db.commit()
    return {"reminded": sent}
