"""Background commands must not attach/create a Windows console from the tray app."""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from infohub.apps import reminders
from infohub.sources import lark


class BackgroundProcesses(unittest.TestCase):
    def test_lark_native_launcher_preferred_and_custom_commands_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "node_modules/@larksuite/cli/bin/lark-cli.exe"
            native.parent.mkdir(parents=True)
            native.touch()
            shim = root / "node_modules/.bin/lark-cli.cmd"
            shim.parent.mkdir(parents=True)
            shim.touch()
            cfg = {"sources": {"lark": {"cli": "lark-cli"}}}
            with patch.object(lark, "ROOT", root), patch.object(lark, "os", types.SimpleNamespace(name="nt")):
                self.assertEqual(lark._cli(cfg), [str(native)])
                cfg["sources"]["lark"]["cli"] = "custom-cli.exe"
                self.assertEqual(lark._cli(cfg), ["custom-cli.exe"])
            cfg["sources"]["lark"]["cli"] = "lark-cli"
            posix = root / "node_modules/.bin/lark-cli"
            posix.touch()
            with patch.object(lark, "ROOT", root), patch.object(lark, "os", types.SimpleNamespace(name="posix")):
                self.assertEqual(lark._cli(cfg), [str(posix)])

    @unittest.skipUnless(os.name == "nt", "Windows console behavior")
    def test_lark_background_command_has_no_console_or_interactive_stdin(self):
        # Exercise the actual collector subprocess path with a synthetic executable.
        cfg = {"sources": {"lark": {"cli": sys.executable}}}
        code = ("import ctypes,json,sys; "
                "print(json.dumps({'ok':True,'console':ctypes.windll.kernel32.GetConsoleWindow(),"
                "'stdin':sys.stdin.read()}))")
        result = lark.run(cfg, None, "-c", code, timeout=10)
        self.assertEqual(result["console"], 0)
        self.assertEqual(result["stdin"], "")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell console behavior")
    def test_reminder_powershell_has_no_console(self):
        # Substitute a harmless console probe so no notification is displayed by this test.
        script = r'''Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class HubConsoleProbe { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); }'
[HubConsoleProbe]::GetConsoleWindow().ToInt64().ToString() | Set-Content -LiteralPath $env:INFOHUB_CONSOLE_PROBE
'''
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "probe.txt"
            with patch.object(reminders, "PS", script), patch.dict(os.environ, {"INFOHUB_CONSOLE_PROBE": str(output)}):
                self.assertTrue(reminders.toast("Synthetic title", "Synthetic body"))
            self.assertEqual(output.read_text(encoding="utf-8-sig").strip(), "0")


if __name__ == "__main__":
    unittest.main()
