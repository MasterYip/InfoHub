"""Online backups, recovery, persisted scheduling, failures and authenticated HTTP settings."""
import copy
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infohub import backup, config
from infohub.apps import auth, server
from infohub.organize import units as U
from infohub.store import Store


class Backups(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["hub"]["database"] = str(self.root / "hub.db")
        self.cfg["hub"]["backup"]["directory"] = str(self.root / "backups")
        self.store = Store(self.cfg["hub"]["database"])
        self.manager = backup.BackupManager(self.cfg)
        self.now = 1800000000

    def tearDown(self):
        self.manager.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def test_backup_restores_committed_wal_data_and_graph(self):
        self.store.q("PRAGMA wal_autocheckpoint=0")
        parent = U.upsert(self.store, None, kind="topic", title="Research plan")
        child = U.upsert(self.store, None, kind="todo", title="Review the plan")
        U.add_tags(self.store, child, ["topic:research", "type:todo"])
        U.link(self.store, parent, child)
        self.store.db.commit()
        self.assertTrue(Path(self.cfg["hub"]["database"] + "-wal").exists())
        result = self.manager.run_once(now=self.now)
        self.assertEqual(result["integrity"], "ok")
        path = Path(result["path"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result["sha256"])
        restored = Store(self.root / "restored.db")
        snapshot = sqlite3.connect(path)
        try:
            snapshot.backup(restored.db)
            self.assertEqual(restored.q("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(U.get(restored, child)["parents"][0]["id"], parent)
            self.assertEqual(U.get(restored, child)["title"], "Review the plan")
            self.assertIn("topic:research", U.get(restored, child)["tags"])
            self.assertEqual(restored.q("SELECT count(*) FROM units_fts WHERE units_fts MATCH 'Review'").fetchone()[0], 1)
        finally:
            snapshot.close()
            restored.db.close()
        # The backup itself is independently readable with no source WAL/SHM files.
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertEqual(self.manager.status()["last_backup"]["path"], str(path))

    def test_weekly_schedule_survives_restart_and_retains_all_copies(self):
        first = self.manager.run_once(now=self.now)
        original = Path(first["path"]).read_bytes()
        reloaded = backup.BackupManager(self.cfg)
        self.assertEqual(reloaded.run_once(now=self.now + 6 * 86400)["skipped"], "not due")
        second = reloaded.run_once(now=self.now + 7 * 86400)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(Path(first["path"]).read_bytes(), original)
        self.assertEqual(reloaded.status()["copies"], 2)
        reloaded.configure({"interval_days": 1})
        self.assertEqual(reloaded.status()["next_due_at"], self.now + 8 * 86400)
        self.assertEqual(reloaded.run_once(now=self.now + 8 * 86400)["integrity"], "ok")
        self.assertEqual(reloaded.status()["copies"], 3)

    def test_disabled_backups_allow_manual_copy_and_missing_database_is_not_created(self):
        self.manager.configure({"enabled": False})
        self.assertIn("skipped", self.manager.run_once(now=self.now))
        self.assertFalse(self.manager.folder.exists())
        self.assertEqual(self.manager.run_once(force=True, now=self.now)["integrity"], "ok")
        self.assertIsNone(self.manager.status()["next_due_at"])
        cfg = copy.deepcopy(self.cfg)
        cfg["hub"]["database"] = str(self.root / "does-not-exist.db")
        missing = backup.BackupManager(cfg)
        with self.assertLogs(backup.log, level="ERROR"):
            self.assertIn("error", missing.run_once(force=True))
        self.assertFalse(Path(cfg["hub"]["database"]).exists())

    def test_cancelled_copy_preserves_previous_verified_backup_and_retries(self):
        first = self.manager.run_once(now=self.now)
        manifest = self.manager.manifest.read_bytes()
        self.manager._stop.set()
        with self.assertLogs(backup.log, level="ERROR"):
            result = self.manager.run_once(force=True, now=self.now + 1)
        self.assertIn("error", result)
        self.assertEqual(self.manager.manifest.read_bytes(), manifest)
        self.assertEqual(self.manager.status()["last_backup"]["path"], first["path"])
        self.assertIsNotNone(self.manager.status()["last_error"])
        self.assertEqual(list(self.manager.folder.glob(".pending-*")), [])
        self.manager._stop.clear()
        self.assertEqual(self.manager.run_once(force=True, now=self.now + 2)["integrity"], "ok")
        self.assertIsNone(self.manager.status()["last_error"])

    def test_failed_integrity_check_never_publishes_a_backup(self):
        real_connect = sqlite3.connect
        class BadIntegrity(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == "PRAGMA integrity_check":
                    return [("injected integrity failure",)]
                return super().execute(sql, *args)
        def connect(path, **kwargs):
            if not kwargs.get("uri"):
                kwargs["factory"] = BadIntegrity
            return real_connect(path, **kwargs)
        with patch.object(backup.sqlite3, "connect", side_effect=connect), self.assertLogs(backup.log, level="ERROR"):
            result = self.manager.run_once(force=True)
        self.assertIn("integrity check failed", result["error"])
        self.assertFalse(self.manager.manifest.exists())
        self.assertEqual(list(self.manager.folder.glob("*.sqlite3")), [])
        self.assertEqual(list(self.manager.folder.glob(".pending-*")), [])

    def test_cross_process_lock_and_missing_snapshot_trigger_fresh_copy(self):
        self.manager.folder.mkdir(parents=True)
        with backup._exclusive(self.manager.folder / "backup.lock") as acquired:
            self.assertTrue(acquired)
            self.assertIn("another process", self.manager.run_once()["skipped"])
        first = self.manager.run_once(now=self.now)
        Path(first["path"]).unlink()
        self.assertIsNone(self.manager.status()["last_backup"])
        self.assertEqual(self.manager.run_once(now=self.now + 1)["integrity"], "ok")

    def test_worker_wakes_for_manual_backup_and_live_settings(self):
        self.manager.configure({"enabled": False})
        self.manager.start()
        self.assertTrue(self.manager.request())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.manager.status()["last_backup"]:
            time.sleep(0.02)
        state = self.manager.status()
        self.assertIsNotNone(state["last_backup"])
        self.assertFalse(state["enabled"])
        self.manager.configure({"enabled": True, "interval_days": 2})
        self.assertEqual(self.manager.status()["next_due_at"], state["last_backup"]["completed_at"] + 2 * 86400)

    def test_settings_reject_invalid_interval_without_partial_write(self):
        with patch.object(config, "SETTINGS_FILE", self.root / "settings.local.json"):
            config.write_settings({"hub.backup.enabled": True, "hub.backup.interval_days": 7})
            original = config.SETTINGS_FILE.read_bytes()
            for invalid in [0, -1, True, "7", None, 1.5, 3651]:
                with self.assertRaises(ValueError):
                    config.write_settings({"hub.backup.enabled": False, "hub.backup.interval_days": invalid})
                self.assertEqual(config.SETTINGS_FILE.read_bytes(), original)
            with self.assertRaises(ValueError):
                config.write_settings({"hub.backup.enabled": "false"})


class BackupHTTP(unittest.TestCase):
    def test_authenticated_api_live_interval_and_manual_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = copy.deepcopy(config.DEFAULTS)
            cfg["hub"]["database"] = str(root / "hub.db")
            cfg["hub"]["backup"].update(enabled=False, directory=str(root / "backups"))
            cfg["hub"]["auth"].update(mode="on", users_file=str(root / "users.local.json"))
            cfg["hub"]["dashboard"]["port"] = 0
            token = auth.add_token(cfg, "backup-test")
            captured, ready = [], threading.Event()
            factory = server.ThreadingHTTPServer
            def capture(address, handler):
                httpd = factory(address, handler)
                captured.append(httpd)
                ready.set()
                return httpd
            store = Store(cfg["hub"]["database"])
            with patch.object(config, "SETTINGS_FILE", root / "settings.local.json"), \
                 patch.object(server, "ThreadingHTTPServer", side_effect=capture):
                thread = threading.Thread(target=server.serve, args=(cfg, store, lambda: {}), daemon=True)
                thread.start()
                self.assertTrue(ready.wait(3))
                httpd = captured[0]
                def req(method, path, body=None, authorized=True, origin=None):
                    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                    headers = {"Host": "127.0.0.1:0", "Content-Type": "application/json"}
                    if authorized:
                        headers["Authorization"] = "Bearer " + token
                    if origin:
                        headers["Origin"] = origin
                    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                    response = connection.getresponse()
                    status, raw = response.status, response.read()
                    connection.close()
                    return status, json.loads(raw)
                try:
                    self.assertEqual(req("GET", "/api/backups", authorized=False)[0], 401)
                    self.assertEqual(req("POST", "/api/backups", {}, origin="https://elsewhere.example")[0], 403)
                    settings = req("GET", "/api/settings")[1]
                    self.assertEqual(settings["values"]["hub.backup.interval_days"], 7)
                    self.assertEqual(req("POST", "/api/settings", {"ai.daily_budget_usd": 2.5})[0], 200)
                    self.assertEqual(req("GET", "/api/settings")[1]["values"]["ai.daily_budget_usd"], 2.5)
                    self.assertNotIn("hub.backup.interval_days", settings["restart_needed_for"])
                    self.assertEqual(req("POST", "/api/settings", {"hub.backup.interval_days": 0})[0], 400)
                    self.assertEqual(req("POST", "/api/settings", {"hub.backup.interval_days": 1,
                                                                         "hub.backup.enabled": True})[0], 200)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        state = req("GET", "/api/backups")[1]
                        if state["last_backup"] and not state["running"]:
                            break
                        time.sleep(0.02)
                    self.assertEqual(state["interval_days"], 1)
                    self.assertIsNotNone(state["last_backup"])
                    self.assertEqual(req("GET", "/api/settings")[1]["values"]["hub.backup.interval_days"], 1)
                    self.assertEqual(req("POST", "/api/backups", {})[0], 202)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        state = req("GET", "/api/backups")[1]
                        if state["copies"] == 2 and not state["running"]:
                            break
                        time.sleep(0.02)
                    self.assertEqual(state["copies"], 2)
                    self.assertEqual(req("GET", "/api/health")[0], 200)
                finally:
                    httpd.shutdown()
                    thread.join(timeout=5)
                    store.db.close()
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
