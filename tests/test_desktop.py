"""Desktop lifecycle and notification tests; GUI packages are optional for the test suite."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from infohub.apps import desktop
from infohub.config import DEFAULTS
from infohub.organize import units as U
from infohub.store import Store


class Desktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["hub"]["database"] = str(self.root / "hub.db")
        self.cfg["_path"] = str(self.root / "config with spaces.yaml")
        self.store = Store(self.cfg["hub"]["database"])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def unit(self, title, kind="topic", **fields):
        uid = U.upsert(self.store, None, kind=kind, title=title, **fields)
        self.store.db.commit()
        return uid

    def app(self, **kwargs):
        self.view = MagicMock()
        self.window = self.view.create_window.return_value
        return desktop.DesktopApp(self.cfg, MagicMock(), desktop.UnitWatcher(self.cfg["hub"]["database"]),
                                   self.view, MagicMock(), **kwargs)

    def test_new_units_batch_without_existing_units_or_replays(self):
        self.unit("Existing unit")
        watcher = desktop.UnitWatcher(self.cfg["hub"]["database"])
        self.assertIsNone(watcher.poll())
        self.unit("New task", kind="todo")
        latest = self.unit("New topic", for_me=True)
        self.unit("Container", kind="digest")
        self.unit("Dropped", status="dropped")
        self.unit("Done", status="done")
        batch = watcher.poll()
        self.assertEqual((batch["count"], batch["latest_id"]), (2, latest))
        self.assertIn("New task", batch["body"])
        self.assertNotIn("Existing", batch["body"])
        self.assertIsNone(watcher.poll())
        self.unit("While paused")
        self.assertIsNone(watcher.poll(enabled=False))
        self.assertIsNone(watcher.poll())
        self.unit("Not for me")
        mine = self.unit("For me", for_me=True)
        self.assertEqual(watcher.poll(mine_only=True)["latest_id"], mine)
        self.assertIsNone(watcher.poll(mine_only=False))
        db = watcher.connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM units")
        finally:
            db.close()

    def test_first_sync_and_reset_database(self):
        path = self.root / "fresh.db"
        watcher = desktop.UnitWatcher(path)
        self.assertIsNone(watcher.poll())
        self.assertFalse(path.exists())
        path.touch()  # an existing empty file should still be initialized by the service
        watcher = desktop.UnitWatcher(path)
        store = Store(path)
        try:
            for i in range(6):
                U.upsert(store, None, kind="topic", title=f"New unit {i}")
            store.db.commit()
            batch = watcher.poll()
            self.assertEqual(batch["count"], 6)
            self.assertIn("+3 more", batch["body"])
            store.q("DELETE FROM units")
            store.db.commit()
            self.assertIsNone(watcher.poll())
            U.upsert(store, None, kind="topic", title="After reset")
            store.db.commit()
            self.assertEqual(watcher.poll()["count"], 1)
        finally:
            store.db.close()

    def test_second_launch_requests_existing_window_and_lock_is_released(self):
        one = desktop.DesktopInstance(self.cfg["hub"]["database"])
        two = desktop.DesktopInstance(self.cfg["hub"]["database"])
        try:
            self.assertTrue(one.acquire())
            self.assertFalse(two.acquire())
            self.assertTrue(one.requested())
            self.assertFalse(one.requested())
            two.close()
            self.assertFalse(two.acquire())
            one.close()
            self.assertTrue(two.acquire())
            self.assertFalse(two.requested())
        finally:
            one.close()
            two.close()

    def test_service_ownership_and_command_paths(self):
        service = desktop.ManagedService(self.cfg)
        with patch.object(service, "healthy", return_value=True), patch.object(desktop.subprocess, "Popen") as spawn:
            service.start()
            service.stop()
            spawn.assert_not_called()
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(service, "healthy", side_effect=[False, True]), \
             patch.object(desktop.socket, "create_connection", side_effect=ConnectionRefusedError), \
             patch.object(desktop, "ROOT", self.root), patch.object(desktop.subprocess, "Popen", return_value=process) as spawn:
            service.start()
            self.assertEqual(spawn.call_args.args[0][-2:], [self.cfg["_path"], "run"])
            streams = list(service.logs)
            service.stop()
            process.terminate.assert_called_once()
            self.assertTrue(all(s.closed for s in streams))

    def test_busy_port_and_failed_start_do_not_orphan_owned_service(self):
        service = desktop.ManagedService(self.cfg)
        with patch.object(service, "healthy", return_value=False), \
             patch.object(desktop.socket, "create_connection") as connect, patch.object(desktop.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                service.start()
            connect.return_value.close.assert_called_once()
            spawn.assert_not_called()
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(service, "healthy", return_value=False), \
             patch.object(desktop.socket, "create_connection", side_effect=ConnectionRefusedError), \
             patch.object(desktop, "ROOT", self.root), patch.object(desktop.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "in time"):
                service.start(timeout=0)
            service.stop()
            process.terminate.assert_called_once()
            self.assertFalse(service.logs)

    def test_window_close_hides_and_quit_exits_preferences_persist(self):
        app = self.app(hidden=True)
        self.assertTrue(self.view.create_window.call_args.kwargs["hidden"])
        self.assertFalse(app.on_closing())
        self.window.hide.assert_called_once()
        self.window.destroy.assert_not_called()
        app.latest_id = 42
        app.open_latest()
        self.window.show.assert_called_once()
        self.window.restore.assert_called_once()
        self.assertIn("openUnit(42)", self.window.evaluate_js.call_args.args[0])
        app.toggle("notifications")
        app.toggle("mine_only")
        second = self.app()
        self.assertFalse(second.options["notifications"])
        self.assertTrue(second.options["mine_only"])
        app.quit()
        self.assertTrue(app.on_closing())
        app.window.destroy.assert_called_once()

    def test_reload_window_loads_the_dashboard_again(self):
        app = self.app()
        app.reload_window()
        self.window.show.assert_called_once()
        self.window.restore.assert_called_once()
        self.window.load_url.assert_called_once_with(app.url)

    def test_monitor_notifies_and_balloon_click_opens_latest(self):
        app = self.app()
        self.unit("New topic")
        app.icon = MagicMock()
        app.instance.requested.return_value = True
        app.stopping = MagicMock()
        app.stopping.wait.side_effect = [False, True]
        app.monitor()
        app.icon.notify.assert_called_once_with("New topic", "1 new info unit")
        app.window.show.assert_called_once()
        self.assertIsNotNone(app.latest_id)
        app.open_latest = MagicMock()
        captured = []
        class Icon:
            def __init__(self, *args):
                captured.append(self)
            def run(self, setup):
                pass
            def _on_notify(self, wparam, lparam):
                self.other_event = lparam
        app.pystray.Icon = Icon
        with patch.object(desktop, "icon_image", return_value=object()):
            app.start_tray()
        captured[0]._on_notify(0, 0x405)
        app.open_latest.assert_called_once()
        captured[0]._on_notify(0, 123)
        self.assertEqual(captured[0].other_event, 123)

    def test_urls_and_health_response_identity(self):
        self.assertEqual(desktop.dashboard_url(self.cfg), "http://127.0.0.1:8765/")
        self.assertEqual(desktop.viewer_url(self.cfg), "http://127.0.0.1:8766/")
        self.cfg["hub"]["dashboard"].update(bind="0.0.0.0", port=9000)
        self.assertEqual(desktop.dashboard_url(self.cfg), "http://127.0.0.1:9000/")
        self.cfg["hub"]["dashboard"].update(bind="::", tls_cert="cert", tls_key="key")
        self.assertEqual(desktop.dashboard_url(self.cfg), "https://[::1]:9000/")
        service = desktop.ManagedService(self.cfg)
        for body, expected in [({"ok": True, "app": "infohub"}, True), ({"ok": True}, True),
                               ({"ok": True, "app": "other"}, False), ([], False)]:
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(body).encode()
            with patch.object(desktop.urllib.request, "build_opener", return_value=opener):
                self.assertEqual(service.healthy(), expected)


if __name__ == "__main__":
    unittest.main()
