"""Borderless WebView2 hosts; the dashboard supplies its own window controls."""
from __future__ import annotations

import ctypes
from ctypes import wintypes


def apply_window_chrome(window, theme="dark", preview=False):
    def install():
        native = window.native
        if native is None:
            return
        from System import Action
        def build():
            if not hasattr(window, "_infohub_chrome"):
                window._infohub_chrome = WindowChrome(window, preview)
            window._infohub_chrome.set_theme(theme)
        if native.InvokeRequired:
            native.Invoke(Action(build))
        else:
            build()
    if window.native is None:
        window.events.before_show += install
    else:
        install()


class WindowChrome:
    def __init__(self, window, preview):
        from System import Action
        from System.Windows.Forms import DockStyle, FormWindowState, Screen
        self.window, self.form = window, window.native
        self.maximized = FormWindowState.Maximized
        self.normal = FormWindowState.Normal
        self.form.MaximizedBounds = Screen.FromHandle(self.form.Handle).WorkingArea
        self.form.webview.Dock = DockStyle.Fill
        self.api = ctypes.windll.user32
        self.api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.SendMessageW.restype = wintypes.LPARAM
        if preview:
            # Deactivate follows activation, so a non-activating hover can stay open.
            def dismiss(*_):
                if not self.form.IsDisposed and not self.form.Disposing:
                    self.form.BeginInvoke(Action(self.form.Close))
            self._dismiss = dismiss
            self.form.Deactivate += dismiss

    def native_drag(self, hit):
        # Native move/resize retains minimum size, multi-monitor DPI and snapping.
        self.window.focus = True
        hwnd = self.form.Handle.ToInt64()
        api = self.api
        api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        api.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        api.SetWindowLongW(hwnd, -20, api.GetWindowLongW(hwnd, -20) & ~0x08000000)
        api.ReleaseCapture()
        api.SendMessageW(hwnd, 0xA1, hit, 0)

    def window_action(self, action):
        from System.Windows.Forms import FormWindowState, Screen
        if action == "minimize":
            self.form.WindowState = FormWindowState.Minimized
        elif action == "maximize":
            self.form.MaximizedBounds = Screen.FromHandle(self.form.Handle).WorkingArea
            self.form.WindowState = self.normal if self.form.WindowState == self.maximized else self.maximized
        elif action == "close":
            self.form.Close()
        elif action == "drag":
            self.native_drag(2)
        elif action.startswith("resize-"):
            hits = {"w": 10, "e": 11, "n": 12, "s": 15, "nw": 13, "ne": 14, "sw": 16, "se": 17}
            if self.form.WindowState != self.maximized:
                self.native_drag(hits[action[7:]])
        return self.form.WindowState == self.maximized

    def set_theme(self, theme):
        from System.Drawing import ColorTranslator
        self.form.BackColor = ColorTranslator.FromHtml("#f6f7f9" if theme == "light" else "#0f141c")
        # Keep a rounded shadow without a visible DWM border.
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        for attribute, number in ((20, int(theme != "light")), (33, 2), (34, -2)):
            value = ctypes.c_int(number)
            dwm.DwmSetWindowAttribute(self.form.Handle.ToInt64(), attribute, ctypes.byref(value), ctypes.sizeof(value))
