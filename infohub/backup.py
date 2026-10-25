"""Consistent online SQLite backups, verified before publication, with a persistent schedule.

Copies are independent SQLite files, not raw copies of a live WAL-mode database. All
completed copies are retained. The manifest advances only after integrity verification,
SHA-256 calculation, and atomic publication. No source database writes are performed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT

log = logging.getLogger(__name__)
CHECK_SECONDS = 60
TIMEOUT_SECONDS = 900


@contextmanager
def _exclusive(path):
    """An OS-released lock protects scheduling across separate service/CLI processes."""
    with path.open("a+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class BackupManager:
    def __init__(self, cfg):
        self.database = Path(cfg["hub"]["database"]).resolve()
        options = {"enabled": True, "interval_days": 7, "directory": "data/backups",
                   **cfg["hub"].get("backup", {})}
        self.options = options
        self._validate(options)
        base = Path(options["directory"])
        key = hashlib.sha256(str(self.database).casefold().encode()).hexdigest()[:12]
        self.folder = (base if base.is_absolute() else ROOT / base) / key
        self.manifest = self.folder / "latest.json"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._requested = False
        self._running = False
        self._error = None
        self._thread = None

    @staticmethod
    def _validate(options):
        if not isinstance(options["enabled"], bool):
            raise ValueError("hub.backup.enabled must be true or false")
        days = options["interval_days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise ValueError("hub.backup.interval_days must be a whole number from 1 to 3650")

    def configure(self, changes):
        with self._lock:
            options = {**self.options, **changes}
            self._validate(options)
            self.options = options
        self._wake.set()

    def _latest(self):
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            name = data["filename"]
            if Path(name).name != name or not name.endswith(".sqlite3"):
                return None
            path = self.folder / name
            if not path.is_file() or path.stat().st_size != data["bytes"]:
                return None
            if not isinstance(data["completed_at"], (int, float)) or len(data["sha256"]) != 64:
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def status(self):
        latest = self._latest()
        with self._lock:
            enabled, days = self.options["enabled"], self.options["interval_days"]
            running, error = self._running, self._error
            requested = self._requested
        return {"enabled": enabled, "interval_days": days, "directory": str(self.folder),
                "running": running, "queued": requested, "last_error": error,
                "last_backup": {**latest, "path": str(self.folder / latest["filename"])} if latest else None,
                "next_due_at": latest["completed_at"] + days * 86400 if latest and enabled else None,
                "copies": sum(1 for _ in self.folder.glob("*.sqlite3"))}

    def request(self):
        with self._lock:
            if self._running or self._requested:
                return False
            self._requested = True
        self._wake.set()
        return True

    def _copy(self, now=None):
        if not self.database.is_file():
            raise FileNotFoundError(f"Database not found: {self.database}")
        token = uuid.uuid4().hex[:12]
        temporary = self.folder / f".pending-{token}.db"
        deadline = time.monotonic() + TIMEOUT_SECONDS
        def check(*_):
            if self._stop.is_set():
                raise InterruptedError("Backup stopped during shutdown")
            if time.monotonic() > deadline:
                raise TimeoutError("Database backup exceeded its time limit")
        try:
            uri = self.database.as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as source, \
                 closing(sqlite3.connect(temporary)) as target:
                source.backup(target, pages=256, progress=check, sleep=0.05)
                target.set_progress_handler(lambda: int(self._stop.is_set() or time.monotonic() > deadline), 10000)
                findings = [r[0] for r in target.execute("PRAGMA integrity_check")]
                if findings != ["ok"]:
                    raise RuntimeError("Backup integrity check failed")
            digest = hashlib.sha256()
            with temporary.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    check()
                    digest.update(block)
            completed = int(time.time() if now is None else now)
            stamp = datetime.fromtimestamp(completed, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"hub-{stamp}-{token}.sqlite3"
            result = {"filename": filename, "completed_at": completed, "bytes": temporary.stat().st_size,
                      "sha256": digest.hexdigest(), "integrity": "ok"}
            temporary.replace(self.folder / filename)
            index = self.folder / f".latest-{token}.tmp"
            try:
                with index.open("w", encoding="utf-8") as stream:
                    json.dump(result, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                index.replace(self.manifest)
            finally:
                index.unlink(missing_ok=True)
            return {**result, "path": str(self.folder / filename)}
        finally:
            temporary.unlink(missing_ok=True)

    def run_once(self, *, force=False, now=None):
        with self._lock:
            if self._running:
                return {"skipped": "backup already running"}
            self._running = True
            options = self.options.copy()
        try:
            if not force and not options["enabled"]:
                return {"skipped": "automatic backups disabled"}
            self.folder.mkdir(parents=True, exist_ok=True)
            with _exclusive(self.folder / "backup.lock") as acquired:
                if not acquired:
                    return {"skipped": "another process is backing up this database"}
                latest = self._latest()
                current = time.time() if now is None else now
                if not force and latest and current < latest["completed_at"] + options["interval_days"] * 86400:
                    return {"skipped": "not due"}
                result = self._copy(now)
            with self._lock:
                self._error = None
            log.info("Database backup verified: %s", result["path"])
            return result
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            with self._lock:
                self._error = message
            log.exception("Database backup failed")
            return {"error": message}
        finally:
            with self._lock:
                self._running = False

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                force, self._requested = self._requested, False
            self.run_once(force=force)
            self._wake.wait(CHECK_SECONDS)
            self._wake.clear()

    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._loop, name="infohub-backups", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
