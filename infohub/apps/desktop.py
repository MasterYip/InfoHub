"""Optional Windows desktop shell. The dashboard remains an ordinary authenticated website.

pywebview owns the main GUI thread; pystray and the read-only unit watcher run separately.
Only a service started by this shell is stopped on exit. No HTTP credentials or JS bridge
are used to watch local units. Runtime state lives beside the configured database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from ..config import ROOT

log = logging.getLogger(__name__)


def dashboard_url(cfg):
    d = cfg["hub"]["dashboard"]
    host = d["bind"]
    host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in host:
        host = f"[{host}]"
    scheme = "https" if d.get("tls_cert") and d.get("tls_key") else "http"
    return f"{scheme}://{host}:{int(d['port'])}/"


def viewer_url(cfg):
    v = cfg["hub"].get("viewer") or {}
    host = v.get("bind", "127.0.0.1")
    host = "127.0.0.1" if host in ("0.0.0.0", "") else "::1" if host == "::" else host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{int(v.get('port', 8766))}/"


def state_root(database):
    db = Path(database).resolve()
    key = hashlib.sha256(str(db).casefold().encode()).hexdigest()[:12]
    return db.parent / f"desktop-{key}"


class DesktopInstance:
    """One shell per database. A second launch asks the first to show its window."""
    def __init__(self, database):
        self.base = state_root(database)
        self.request = self.base.with_suffix(".open")
        self.file = None

    def acquire(self):
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.base.with_suffix(".lock").open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            self.request.touch()
            return False
        self.request.unlink(missing_ok=True)
        return True

    def requested(self):
        if self.request.exists():
            self.request.unlink(missing_ok=True)
            return True
        return False

    def close(self):
        if self.file:
            self.file.close()  # OS releases the lock even after an unclean exit
            self.file = None


class UnitWatcher:
    """Track IDs created after launch, including first-sync units, without writing the DB."""
    def __init__(self, database):
        self.database = Path(database)
        self.cursor = 0
        if self.database.exists():
            with closing(self.connect()) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='units'").fetchone():
                    self.cursor = db.execute("SELECT coalesce(max(id),0) FROM units").fetchone()[0]

    def connect(self):
        db = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def poll(self, *, enabled=True, mine_only=False):
        if not self.database.exists():
            return None
        # sqlite3's connection context manager does not close connections.
        db = self.connect()
        try:
            high = db.execute("SELECT coalesce(max(id),0) FROM units").fetchone()[0]
            low = self.cursor
            # Resets/replaced databases should not permanently strand the cursor.
            if high < low:
                self.cursor = high
                return None
            where = "id>? AND id<=? AND status='open' AND kind!='digest'"
            if mine_only:
                where += " AND for_me=1"
            count = db.execute(f"SELECT count(*) FROM units WHERE {where}", (low, high)).fetchone()[0]
            rows = db.execute(f"SELECT id, title FROM units WHERE {where} ORDER BY id DESC LIMIT 3",
                              (low, high)).fetchall() if count and enabled else []
            self.cursor = high  # disabled/filtered units never become a notification backlog
            if not rows:
                return None
            title = "1 new info unit" if count == 1 else f"{count} new info units"
            body = "\n".join((r["title"] or "Untitled")[:70] for r in rows)
            if count > len(rows):
                body += f"\n+{count - len(rows)} more"
            return {"title": title, "body": body[:240], "count": count, "latest_id": rows[0]["id"]}
        finally:
            db.close()


class ManagedService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.url = dashboard_url(cfg)
        self.process = None
        self.logs = []

    def healthy(self):
        try:
            # Never send a loopback request through a configured HTTP proxy.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.url + "api/health", timeout=1) as response:
                data = json.loads(response.read(4096))
            return isinstance(data, dict) and data.get("ok") is True and data.get("app", "infohub") == "infohub"
        except (OSError, ValueError):
            return False

    def start(self, timeout=30):
        if self.healthy():
            return  # reuse an independently managed service; never terminate it
        address = urlsplit(self.url)
        try:
            connection = socket.create_connection((address.hostname, address.port), timeout=1)
        except OSError:
            pass
        else:
            connection.close()
            raise RuntimeError(f"{self.url} is occupied but not a healthy Info Hub. Check the service or port.")
        logs = ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.logs = [(logs / name).open("ab") for name in ("desktop-service.log", "desktop-service.err.log")]
        command = [sys.executable, "-X", "utf8", "-m", "infohub", "--config",
                   str(Path(self.cfg["_path"]).resolve()), "run"]
        self.process = subprocess.Popen(command, cwd=ROOT, stdout=self.logs[0], stderr=self.logs[1],
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Info Hub service exited during startup. See logs/desktop-service.err.log.")
            if self.healthy():
                return
            time.sleep(0.2)
        raise RuntimeError("Info Hub did not start in time. See logs/desktop-service.err.log.")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in self.logs:
            stream.close()
        self.logs = []


def icon_image():
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((2, 2, 62, 62), radius=15, fill="#2563eb")
    draw.line([(18, 20), (43, 18), (32, 44), (18, 20)], fill="#bfdbfe", width=4)
    for x, y in [(18, 20), (43, 18), (32, 44)]:
        draw.ellipse((x-6, y-6, x+6, y+6), fill="white")
    return im


class DesktopApp:
    def __init__(self, cfg, instance, watcher, webview, pystray, *, hidden=False, title="Info Hub"):
        self.cfg, self.instance, self.watcher = cfg, instance, watcher
        self.webview, self.pystray = webview, pystray
        self.url = dashboard_url(cfg)
        self.options = {"notifications": True, "mine_only": False, **cfg["apps"].get("desktop", {})}
        self.options["poll_seconds"] = max(1, min(300, int(self.options.get("poll_seconds", 5))))
        self.preferences = state_root(cfg["hub"]["database"]).with_suffix(".local.json")
        if self.preferences.exists():
            try:
                saved = json.loads(self.preferences.read_text(encoding="utf-8"))
                self.options.update({k: v for k, v in saved.items() if k in ("notifications", "mine_only") and isinstance(v, bool)})
            except (OSError, ValueError, AttributeError):
                log.warning("Could not read desktop preferences", exc_info=True)
        self.stopping = threading.Event()
        self.latest_id, self.unread = None, 0
        self.icon = None
        self.worker = None
        self.window = webview.create_window(title, self.url, width=1280, height=860, min_size=(760, 540),
                                             hidden=hidden, text_select=True)
        self.window.events.closing += self.on_closing

    def on_closing(self):
        if self.stopping.is_set():
            return True
        self.window.hide()
        return False

    def open_window(self, *_):
        self.window.show()
        self.window.restore()
        self.unread = 0
        if self.icon:
            self.icon.title = "Info Hub"

    def open_latest(self, *_):
        self.open_window()
        if self.latest_id:
            # IDs come from SQLite, not arbitrary JavaScript; no Python bridge is exposed.
            self.window.evaluate_js(f"if (typeof openUnit === 'function') void openUnit({int(self.latest_id)})")

    def open_browser(self, *_):
        webbrowser.open(self.url)

    def reload_window(self, *_):
        # pywebview disables browser accelerator keys (F5/Ctrl+R) unless debug is on,
        # so give the tray an explicit way to pick up a redeployed dashboard.
        self.open_window()
        self.window.load_url(self.url)

    def open_viewer(self, *_):
        webbrowser.open(viewer_url(self.cfg))

    def toggle(self, key):
        self.options[key] = not self.options[key]
        data = {k: self.options[k] for k in ("notifications", "mine_only")}
        try:
            tmp = self.preferences.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.preferences)
        except OSError:
            log.exception("Could not save desktop preferences")

    def quit(self, *_):
        self.stopping.set()
        self.window.destroy()

    def monitor(self):
        interval = max(1, int(self.options.get("poll_seconds", 5)))
        next_poll = 0
        while not self.stopping.wait(0.5):
            try:
                if self.instance.requested():
                    self.open_window()
                if time.monotonic() < next_poll:
                    continue
                next_poll = time.monotonic() + interval
                batch = self.watcher.poll(enabled=self.options["notifications"], mine_only=self.options["mine_only"])
                if batch:
                    self.latest_id = batch["latest_id"]
                    self.unread += batch["count"]
                    self.icon.title = f"Info Hub - {self.unread} new units"
                    self.icon.update_menu()
                    self.icon.notify(batch["body"], batch["title"])
            except Exception:
                log.exception("Desktop notification check failed")

    def start_tray(self):
        app = self
        class WindowsIcon(self.pystray.Icon):
            def _on_notify(self, wparam, lparam):
                # Windows NIN_BALLOONUSERCLICK (WM_USER + 5). pystray 0.19.5 ignores it.
                if lparam == 0x405:
                    app.open_latest()
                else:
                    super()._on_notify(wparam, lparam)
        item = self.pystray.MenuItem
        self.icon = WindowsIcon("infohub", icon_image(), "Info Hub", self.pystray.Menu(
            item("Open Info Hub", self.open_window, default=True),
            item("Reload panel", self.reload_window),
            item("Open latest new unit", self.open_latest, enabled=lambda _: self.latest_id is not None),
            item("Open in browser", self.open_browser),
            item("Open viewer only", self.open_viewer),
            self.pystray.Menu.SEPARATOR,
            item("Notify on new units", lambda: self.toggle("notifications"), checked=lambda _: self.options["notifications"]),
            item("Only notify units for me", lambda: self.toggle("mine_only"), checked=lambda _: self.options["mine_only"]),
            item("Open logs", lambda: os.startfile(str(ROOT / "logs"))),
            self.pystray.Menu.SEPARATOR,
            item("Quit Info Hub", self.quit)))
        def setup(icon):
            icon.visible = True
            self.worker = threading.Thread(target=self.monitor, name="infohub-notifications", daemon=True)
            self.worker.start()
        try:
            self.icon.run(setup=setup)
        except Exception:
            log.exception("System tray failed")
            self.quit()  # a window that closes to tray must never be left without a tray

    def run(self):
        tray = threading.Thread(target=self.start_tray, name="infohub-tray", daemon=True)
        profile = state_root(self.cfg["hub"]["database"]).with_suffix(".webview")
        icon_path = profile.with_suffix(".ico")
        icon_image().save(icon_path, format="ICO", sizes=[(16, 16), (32, 32), (64, 64)])
        try:
            # pywebview runs the tray bootstrap off its main GUI thread.
            self.webview.start(tray.start, gui="edgechromium", private_mode=False, storage_path=str(profile), icon=str(icon_path))
        finally:
            self.stopping.set()
            if self.icon:
                self.icon.stop()
            if tray.ident:
                tray.join(timeout=5)
            if self.worker:
                self.worker.join(timeout=3)


def run(cfg, *, hidden=False):
    if os.name != "nt":
        raise SystemExit("The desktop shell currently supports Windows. Use `python -m infohub run` for the web app.")
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logs / "desktop.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    instance = DesktopInstance(cfg["hub"]["database"])
    service = ManagedService(cfg)
    try:
        if not instance.acquire():
            return
        try:
            import webview
            import pystray
            from PIL import Image  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("Install desktop support: .venv\\Scripts\\python.exe -m pip install -r requirements-desktop.txt") from exc
        watcher = UnitWatcher(cfg["hub"]["database"])  # baseline before starting the first sync
        service.start()
        DesktopApp(cfg, instance, watcher, webview, pystray, hidden=hidden).run()
    except Exception as exc:
        log.exception("Desktop startup failed")
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(exc) + "\n\nSee logs/desktop.log for details.", "Info Hub", 0x10)
        raise SystemExit(1) from exc
    finally:
        service.stop()
        instance.close()
