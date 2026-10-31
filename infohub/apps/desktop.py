"""Optional Windows desktop shell. The dashboard remains an ordinary authenticated website.

pywebview owns the main GUI thread; pystray and the read-only unit watcher run separately.
Only a service started by this shell is stopped on exit. No HTTP credentials or JS bridge
are used to watch local units. Runtime state lives beside the configured database.
"""
from __future__ import annotations

import hashlib
import ctypes
from ctypes import wintypes
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
from .desktop_chrome import apply_window_chrome

log = logging.getLogger(__name__)


def configure_taskbar_identity():
    # Set before creating any windows so Explorer does not group us under pythonw.exe.
    set_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    set_id.argtypes = [ctypes.c_wchar_p]
    set_id.restype = ctypes.c_long
    result = set_id("InfoHub.Desktop")
    if result < 0:
        raise OSError(f"Could not set Info Hub taskbar identity: HRESULT {result:#x}")


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
    from PIL import Image
    with Image.open(Path(__file__).parent / "web" / "logo.png") as image:
        return image.convert("RGBA")


def preview_placement(cursor, size, work, scale=1):
    """Match the HTML float card's pointer offset, bounded by its monitor."""
    left, top, right, bottom = work
    margin = round(8 * scale)
    left, top, right, bottom = left + margin, top + margin, right - margin, bottom - margin
    width, height = min(size[0], right - left), min(size[1], bottom - top)
    gap = round(14 * scale)
    x, y = cursor[0] + gap, cursor[1] - round(12 * scale)
    if x + width > right:
        x = cursor[0] - width - gap
    return max(left, min(x, right - width)), max(top, min(y, bottom - height)), width, height


class PreviewPointer:
    """Native hit testing also works while the pointer is inside a remote WebView."""

    def __init__(self):
        self.api = ctypes.windll.user32
        self.api.GetForegroundWindow.restype = wintypes.HWND
        self.api.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        self.api.MonitorFromPoint.restype = wintypes.HANDLE
        self.api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]

    def position(self):
        point = wintypes.POINT()
        self.api.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def work_area(self):
        class Monitor(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        info = Monitor(); info.size = ctypes.sizeof(info)
        handle = self.api.MonitorFromPoint(wintypes.POINT(*self.position()), 2)
        if not self.api.GetMonitorInfoW(handle, ctypes.byref(info)):
            raise OSError("Monitor information unavailable")
        return info.work.left, info.work.top, info.work.right, info.work.bottom

    @staticmethod
    def handle(window):
        return window.native.Handle.ToInt64()

    def bounds(self, window):
        rect = wintypes.RECT()
        self.api.GetWindowRect(self.handle(window), ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def place(self, window, cursor, scale=1):
        bounds = self.bounds(window)
        box = preview_placement(cursor, (bounds[2] - bounds[0], bounds[3] - bounds[1]), self.work_area(), scale)
        self.api.SetWindowPos(self.handle(window), -1, *box, 0x10)  # topmost, no activation

    def show_at(self, window, cursor, scale=1):
        from System import Action
        def reveal():
            self.place(window, cursor, scale)
            self.api.ShowWindow(self.handle(window), 4)  # SW_SHOWNOACTIVATE
        window.native.Invoke(Action(reveal))

    def allow_focus(self, window):
        window.focus = True
        hwnd = self.handle(window)
        self.api.SetWindowLongW(hwnd, -20, self.api.GetWindowLongW(hwnd, -20) & ~0x08000000)


class PreviewAPI:
    """Expose only window creation to the local dashboard, never to preview websites."""

    def __init__(self, app):
        self._app = app

    def window_action(self, action):
        app = self._app
        current, expected = urlsplit(app.window.get_current_url() or ""), urlsplit(app.url)
        if (current.scheme, current.netloc) != (expected.scheme, expected.netloc) or current.path not in ("/", "/index.html"):
            return {"ok": False}
        allowed = {"minimize", "maximize", "close", "drag",
                   "resize-n", "resize-s", "resize-e", "resize-w",
                   "resize-ne", "resize-nw", "resize-se", "resize-sw"}
        if action not in allowed:
            return {"ok": False}
        from System import Action
        result = {}
        def perform():
            result.update(ok=True, maximized=app.window._infohub_chrome.window_action(action))
        app.window.native.Invoke(Action(perform))
        return result

    def set_theme(self, theme):
        app = self._app
        current, expected = urlsplit(app.window.get_current_url() or ""), urlsplit(app.url)
        if (current.scheme, current.netloc) != (expected.scheme, expected.netloc) or current.path not in ("/", "/index.html"):
            return {"ok": False}
        if theme not in ("dark", "light"):
            return {"ok": False}
        app._theme = theme
        apply_window_chrome(app.window, theme)
        if app._preview_window is not None:
            apply_window_chrome(app._preview_window, theme, preview=True)
        return {"ok": True}

    def open_preview(self, url, hover=None):
        app = self._app
        current = urlsplit(app.window.get_current_url() or "")
        expected = urlsplit(app.url)
        if (current.scheme, current.netloc) != (expected.scheme, expected.netloc) or current.path not in ("/", "/index.html"):
            return {"ok": False}
        try:
            target = urlsplit(url)
            if target.scheme not in ("http", "https") or not target.hostname or target.username or target.password:
                return {"ok": False}
            if any(ord(c) < 32 for c in url):
                return {"ok": False}
        except (TypeError, ValueError):
            return {"ok": False}
        if hover is not None and app._preview_hover and app._preview_window is not None and app._preview_url == url:
            return {"ok": True}
        pointer, source, anchor, scale = None, None, None, 1
        if hover is not None:
            try:
                values = [float(hover[k]) for k in ("left", "top", "right", "bottom", "scale")]
                if not all(-10000 < n < 10000 for n in values) or not 0.25 <= values[4] <= 8:
                    return {"ok": False}
                pointer = PreviewPointer()
                x, y = pointer.position()
                anchor = (x, y)
                scale = values[4]
                source = tuple(int(base + n * scale) for base, n in zip((x, y, x, y), values[:4]))
            except (KeyError, TypeError, ValueError, OSError):
                return {"ok": False}
        app._preview_generation += 1
        generation = app._preview_generation
        previous, app._preview_window = app._preview_window, None
        app._preview_hover = hover is not None
        if previous is not None:
            previous.destroy()
        preview = app.webview.create_window("Preview - " + target.hostname, url, **app._preview_size,
                                           min_size=(360, 260), resizable=True, text_select=True,
                                           focus=hover is None, on_top=hover is not None,
                                           hidden=True, frameless=True, easy_drag=False, shadow=True,
                                           background_color="#f6f7f9" if app._theme == "light" else "#0f141c")
        apply_window_chrome(preview, app._theme, preview=True)
        if generation != app._preview_generation:
            preview.destroy()
            return {"ok": False}
        app._preview_window = preview
        app._preview_url = url
        app._preview_hover = hover is not None
        def closed():
            if app._preview_window is preview:
                app._preview_window = None
        preview.events.closed += closed
        def resized(width, height):
            app._preview_size = {"width": int(width), "height": int(height)}
            try:
                app._preview_preferences.write_text(json.dumps(app._preview_size), encoding="utf-8")
            except OSError:
                log.warning("Could not save preview size")
        preview.events.resized += resized
        if pointer:
            pointer.show_at(preview, anchor, scale)
            threading.Thread(target=self._watch_hover, args=(preview, pointer, source),
                             name="infohub-preview-hover", daemon=True).start()
        else:
            preview.show()
        return {"ok": True}

    def close_hover_preview(self):
        app = self._app
        if app._preview_hover:
            app._preview_generation += 1
            preview, app._preview_window = app._preview_window, None
            app._preview_hover = False
            if preview is not None:
                preview.destroy()

    def _watch_hover(self, preview, pointer, source):
        app = self._app
        try:
            if not preview.events.shown.wait(10) or app._preview_window is not preview:
                return
            last_inside = time.monotonic()
            contains = lambda r, p: r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]
            while app._preview_window is preview and not app.stopping.wait(0.1):
                point, now = pointer.position(), time.monotonic()
                foreground = pointer.api.GetForegroundWindow()
                if foreground not in (pointer.handle(app.window), pointer.handle(preview)):
                    break
                in_preview = contains(pointer.bounds(preview), point)
                if in_preview and not preview.focus:
                    pointer.allow_focus(preview)
                dragging = foreground == pointer.handle(preview) and pointer.api.GetAsyncKeyState(1) & 0x8000
                if in_preview or contains(source, point) or dragging:
                    last_inside = now
                elif now - last_inside >= 0.8:
                    break
            if app._preview_window is preview:
                self.close_hover_preview()
        except Exception:
            if app._preview_window is preview:
                log.exception("Preview hover tracking failed")
                self.close_hover_preview()


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
        self._preview_window = None
        self._preview_hover = False
        self._preview_url = None
        self._preview_generation = 0
        self._theme = "dark"
        self._preview_preferences = state_root(cfg["hub"]["database"]).with_suffix(".preview.local.json")
        self._preview_size = {"width": 720, "height": 520}
        try:
            saved = json.loads(self._preview_preferences.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key, minimum in (("width", 360), ("height", 260)):
                    if isinstance(saved.get(key), int):
                        self._preview_size[key] = max(minimum, min(3840, saved[key]))
        except (OSError, ValueError):
            pass
        self.window = webview.create_window(title, self.url, width=1280, height=860, min_size=(760, 540),
                                             hidden=hidden, text_select=True, js_api=PreviewAPI(self),
                                             frameless=True, easy_drag=False, shadow=True, background_color="#0f141c")
        apply_window_chrome(self.window, self._theme)
        self.window.events.closing += self.on_closing

    def on_closing(self):
        if self.stopping.is_set():
            return True
        PreviewAPI(self).close_hover_preview()
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
        if self._preview_window is not None:
            self._preview_window.destroy()
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
        icon_image().save(icon_path, format="ICO", sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
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
        configure_taskbar_identity()
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
