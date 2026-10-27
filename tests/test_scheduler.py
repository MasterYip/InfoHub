"""User-authored assistant schedules use the shared SQLite database."""
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

from infohub.apps import scheduler
from infohub.store import Store


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "hub.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_validation_and_next_run(self):
        daily = scheduler.validate({"name": "Morning", "mode": "daily", "daily_time": "09:30",
                                    "prompt": "Review the large topics"})
        self.assertEqual(scheduler.next_run(daily, now=1704067200, tz=timezone.utc), 1704101400)
        interval = scheduler.validate({"name": "Sweep", "mode": "interval", "interval_minutes": 15,
                                       "prompt": "Maintain topics"})
        self.assertEqual(scheduler.next_run(interval, now=1000), 1900)
        with self.assertRaises(ValueError):
            scheduler.validate({"name": "bad", "mode": "daily", "daily_time": "25:00", "prompt": "x"})

    def test_crud_and_due_claim_are_persistent(self):
        row = scheduler.save(self.store, {"name": "Sweep", "mode": "interval", "interval_minutes": 5,
                                         "prompt": "Maintain topics"}, now=1000)
        self.assertEqual(row["last_status"], "never")
        self.assertEqual(self.store.schedules()[0]["id"], row["id"])
        self.assertEqual([r["id"] for r in self.store.due_schedules(now=row["next_run"])], [row["id"]])
        self.assertTrue(self.store.mark_schedule_running(row["id"], row["next_run"]))
        self.assertEqual(self.store.due_schedules(now=row["next_run"]), [])
        self.store.finish_schedule(row["id"], next_run=2000, status="ok", now=1500)
        self.assertEqual(self.store.schedule(row["id"])["last_run"], 1500)
        self.assertTrue(self.store.delete_schedule(row["id"]))
        self.assertEqual(self.store.schedules(), [])

    def test_due_run_calls_assistant_and_records_status(self):
        row = scheduler.save(self.store, {"name": "Sweep", "mode": "interval", "interval_minutes": 5,
                                         "prompt": "Maintain the large topics"}, now=900)
        cfg = {"_path": None, "hub": {"timezone_offset_hours": 8}}
        with patch("infohub.apps.scheduler.load", return_value=cfg), \
             patch("infohub.apps.chat.chat", return_value={"reply": "done", "changes": []}) as run:
            result = scheduler.run_due(cfg, self.store, now=row["next_run"])
        self.assertEqual(result, {"checked": 1, "completed": 1, "failed": 0})
        run.assert_called_once_with(cfg, self.store, [{"role": "user", "content": "Maintain the large topics"}])
        self.assertEqual(self.store.schedule(row["id"])["last_status"], "ok")


if __name__ == "__main__":
    unittest.main()
