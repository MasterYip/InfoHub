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
    def test_taskbar_identity_is_distinct_from_python(self):
        setter = MagicMock(return_value=0)
        with patch.object(desktop.ctypes, "windll", create=True) as native:
            native.shell32.SetCurrentProcessExplicitAppUserModelID = setter
            desktop.configure_taskbar_identity()
            setter.assert_called_once_with("InfoHub.Desktop")
            setter.return_value = -1
            with self.assertRaises(OSError):
                desktop.configure_taskbar_identity()

    def setUp(self):
        self.chrome_patch = patch.object(desktop, "apply_window_chrome")
        self.chrome = self.chrome_patch.start()
        self.addCleanup(self.chrome_patch.stop)
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

    def test_preview_is_top_level_resizable_and_has_no_dashboard_bridge(self):
        app = self.app()
        main = app.window
        main.get_current_url.return_value = app.url + "?tab=chat"
        api = self.view.create_window.call_args.kwargs["js_api"]
        preview = MagicMock()
        resize_event = preview.events.resized
        self.view.create_window.return_value = preview
        self.assertEqual(api.open_preview("https://example.com/page"), {"ok": True})
        args = self.view.create_window.call_args
        self.assertEqual(args.args[1], "https://example.com/page")
        self.assertTrue(args.kwargs["resizable"])
        self.assertNotIn("js_api", args.kwargs)
        self.assertTrue(args.kwargs["frameless"])
        self.assertFalse(args.kwargs["easy_drag"])
        self.assertTrue(args.kwargs["hidden"])
        preview.show.assert_called_once()
        self.assertIs(app._preview_window, preview)
        resized = resize_event.__iadd__.call_args.args[0]
        resized(850, 600)
        self.assertEqual(app._preview_size, {"width": 850, "height": 600})
        self.assertEqual(json.loads(app._preview_preferences.read_text()), app._preview_size)
        for url in ["file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com", "https://example.com/\n"]:
            self.assertEqual(api.open_preview(url), {"ok": False})
        main.get_current_url.return_value = "https://example.com"
        self.assertEqual(api.open_preview("https://example.com/page"), {"ok": False})
        app.quit()
        preview.destroy.assert_called_once()
        main.destroy.assert_called_once()

    def test_hover_preview_stays_near_link_and_does_not_steal_focus(self):
        app = self.app()
        app.window.get_current_url.return_value = app.url
        pointer = MagicMock()
        pointer.position.return_value = (200, 150)
        with patch.object(desktop, "PreviewPointer", return_value=pointer), patch.object(desktop.threading, "Thread"):
            result = desktop.PreviewAPI(app).open_preview("https://example.com", {
                "left": -10, "top": -5, "right": 40, "bottom": 15, "scale": 2})
        self.assertTrue(result["ok"])
        options = self.view.create_window.call_args.kwargs
        self.assertFalse(options["focus"])
        self.assertTrue(options["on_top"])
        self.assertTrue(options["resizable"])
        self.assertTrue(options["hidden"])
        pointer.show_at.assert_called_once_with(app._preview_window, (200, 150), 2)
        self.assertTrue(app._preview_hover)
        desktop.PreviewAPI(app).close_hover_preview()
        self.assertIsNone(app._preview_window)

    def test_hover_tracking_crosses_gap_and_closes_after_leaving(self):
        app = self.app()
        preview = MagicMock()
        preview.focus = False
        app._preview_window, app._preview_hover = preview, True
        pointer = MagicMock()
        pointer.handle.side_effect = lambda w: 1 if w is app.window else 2
        pointer.api.GetForegroundWindow.return_value = 1
        pointer.bounds.return_value = (50, 0, 200, 200)
        pointer.position.side_effect = [(10, 10), (45, 10), (100, 10), (400, 400), (400, 400)]
        with patch.object(app.stopping, "wait", return_value=False), \
             patch.object(desktop.time, "monotonic", side_effect=[0, .1, .2, .4, .7, 1.3]):
            desktop.PreviewAPI(app)._watch_hover(preview, pointer, (0, 0, 40, 20))
        pointer.place.assert_not_called()
        pointer.allow_focus.assert_called_once_with(preview)
        preview.destroy.assert_called_once()
        self.assertIsNone(app._preview_window)

    def test_hover_placement_is_clamped_on_offset_monitors(self):
        self.assertEqual(desktop.preview_placement((100, 50), (500, 400), (0, 0, 1920, 1080)),
                         (114, 38, 500, 400))
        self.assertEqual(desktop.preview_placement((-80, 800), (700, 500), (-1920, 0, 0, 1080)),
                         (-794, 572, 700, 500))
        self.assertEqual(desktop.preview_placement((100, 50), (500, 400), (0, 0, 1920, 1080), 2),
                         (128, 26, 500, 400))

    def test_hover_position_is_set_before_nonactivating_show(self):
        pointer = desktop.PreviewPointer.__new__(desktop.PreviewPointer)
        pointer.api = MagicMock()
        window = MagicMock()
        window.native.Handle.ToInt64.return_value = 42
        window.native.Invoke.side_effect = lambda callback: callback()
        calls = []
        pointer.place = MagicMock(side_effect=lambda *_: calls.append("place"))
        pointer.api.ShowWindow.side_effect = lambda *_: calls.append("show")
        system = MagicMock()
        system.Action.side_effect = lambda callback: callback
        with patch.dict("sys.modules", {"System": system}):
            pointer.show_at(window, (100, 200), 2)
        self.assertEqual(calls, ["place", "show"])
        pointer.place.assert_called_once_with(window, (100, 200), 2)
        pointer.api.ShowWindow.assert_called_once_with(42, 4)
        window.native.Show.assert_not_called()

    def test_theme_updates_both_windows_from_dashboard_only(self):
        app = self.app()
        self.assertTrue(self.view.create_window.call_args.kwargs["frameless"])
        app.window.get_current_url.return_value = app.url
        app._preview_window = MagicMock()
        api = desktop.PreviewAPI(app)
        self.assertEqual(api.set_theme("light"), {"ok": True})
        self.chrome.assert_any_call(app.window, "light")
        self.chrome.assert_any_call(app._preview_window, "light", preview=True)
        self.assertEqual(app._theme, "light")
        self.assertEqual(api.set_theme("arbitrary"), {"ok": False})
        app.window.get_current_url.return_value = "https://example.com"
        self.assertEqual(api.set_theme("dark"), {"ok": False})

    def test_window_controls_are_limited_to_dashboard(self):
        app = self.app()
        api = desktop.PreviewAPI(app)
        app.window.get_current_url.return_value = "https://example.com"
        self.assertEqual(api.window_action("close"), {"ok": False})
        app.window.get_current_url.return_value = app.url
        self.assertEqual(api.window_action("resize-invalid"), {"ok": False})
        system = MagicMock()
        system.Action.side_effect = lambda callback: callback
        app.window.native.Invoke.side_effect = lambda callback: callback()
        app.window._infohub_chrome.window_action.return_value = True
        with patch.dict("sys.modules", {"System": system}):
            self.assertEqual(api.window_action("maximize"), {"ok": True, "maximized": True})
        app.window._infohub_chrome.window_action.assert_called_once_with("maximize")

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
