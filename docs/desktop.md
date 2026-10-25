# Windows desktop and system tray

Info Hub can run in a dedicated window with a tray icon. It uses the same dashboard,
API and login as the browser. The web interface, `python -m infohub run` and
`scripts/start.ps1` continue to work independently.

## Install and launch

Use Windows 10/11 with Python 3.10+ and the [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/consumer/).
Desktop packages are optional; a server only needs the usual requirements.

```powershell
# In an existing setup:
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\scripts\start-desktop.ps1

# Or install everything on a new machine:
.\scripts\setup.ps1 -Desktop
.\scripts\start-desktop.ps1
```

The launcher uses `pythonw.exe` so no console window stays open. For a console launch:

```powershell
.\.venv\Scripts\python.exe -m infohub desktop
.\.venv\Scripts\python.exe -m infohub desktop --hidden
# An alternative configuration (global --config goes before the command):
.\.venv\Scripts\python.exe -m infohub --config config\other.local.yaml desktop
# The PowerShell launcher also accepts -Config and -Hidden.
```

A second launch brings the existing window forward. Closing the window hides it to
its tray icon; **Quit Info Hub** exits the desktop app. If desktop started the service,
Quit also stops that service. If a service was already running, desktop reuses it and
leaves it running on exit. The browser dashboard stays reachable at the configured
`hub.dashboard` address while that service runs.

## Tray actions and notifications

- **Open Info Hub** (also click the tray icon): show or restore the window.
- **Open latest new unit**: open the most recently notified unit as a floating card.
  Clicking a new-unit notification does the same. If login is required, sign in first.
- **Open in browser**: open the existing web interface in your default browser.
- **Notify on new units**: pause/resume new-unit alerts.
- **Only notify units for me**: limit new-unit alerts to the hub's existing For-me flag.
- **Open logs** and **Quit Info Hub**.

The tray checks the local database every five seconds, including while the window is
hidden. A batch produces one Windows notification showing up to three titles. Existing
units at launch do not trigger a backlog; units created by the service's first sync do.
Only new open units are included; digest containers, completed and dropped units are
excluded. Edits to existing units do not count as new units. Pausing notifications or
filtering them out does not queue them for later replay. Windows notification settings
and Do Not Disturb can suppress delivery.

Defaults can be set in the local YAML file:

```yaml
apps:
  desktop:
    notifications: true
    mine_only: false
    poll_seconds: 5
```

Notification/scope choices made in the tray are saved locally and override these two
YAML defaults on the next launch. This menu controls new-unit notifications. Existing
due-task and upcoming-event reminders keep their separate settings under
`apps.reminders` and in the web Settings page.

Window cookies, login sessions and local storage persist in a separate WebView2 profile,
so browser and desktop sign-ins are independent. Desktop state is stored beside the
configured database in `desktop-<database hash>.*` files/directories; these paths are
Git-ignored. Logs are in `logs/desktop.log` and `logs/desktop-service*.log`.

## Database backups

The service makes automatic database backups while the desktop window is hidden too.
Set their interval (default seven days), run a copy now, and check the last verified copy
in **Settings → Database backups**. See [backup configuration and recovery](backups.md).

## Start at Windows logon

```powershell
.\scripts\install-autostart.ps1 -Desktop
```

This starts hidden in the tray after logon, in your interactive Windows session.
Installing the existing task without `-Desktop` keeps the background web-service
behavior. Re-running either command updates the same **InfoHub** scheduled task.

If startup fails, the app shows an error and writes details to `logs/desktop.log`.
A busy dashboard port is reported instead of starting another service. For WebView2
errors, install/update the Runtime and try the console command above. Non-Windows
machines can continue to use the browser dashboard and the original service commands.
