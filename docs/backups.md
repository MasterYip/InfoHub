# Database backups

Automatic database backups are enabled by default, every **7 days**. The first backup
runs when the dashboard service starts if no successful backup exists. The service
checks the persisted schedule every minute, including while the desktop window is
hidden. An overdue backup runs on the next service start after downtime. Both
`python -m infohub run` and `python -m infohub serve` run the backup worker.

## Settings

Open **Settings → Database backups** in the browser or desktop window:

- Enable or pause automatic backups.
- Change **Back up every N days** (whole days, from 1 to 3650; default 7).
- Use **Back up now** for an immediate copy, including while automatic backups are paused.
- See the last verified backup, its size, next due time, location, copy count and any error.

Backup settings apply immediately without a restart. Backups run in a separate thread
so the dashboard remains usable. A failed backup leaves the previous successful copy
and its schedule intact; automatic attempts retry on the next minute's check. There is
no automatic deletion of completed backups.

The default destination is `data/backups/<database hash>/`. Each database has its own
folder. Copy files are named `hub-<UTC timestamp>-<unique suffix>.sqlite3`; `latest.json`
records the latest successful copy's completion time, size and SHA-256 hash. Runtime
backups are Git-ignored and excluded from the public repository.

To use a different location, set it in `config/hub.local.yaml` and restart:

```yaml
hub:
  backup:
    enabled: true
    interval_days: 7
    directory: D:/InfoHubBackups
```

Relative directories resolve from the repository root. Copies in the default local
folder protect against accidental edits or database damage. A separate drive or an
independently backed-up destination is needed to preserve them after loss of this drive.
These copies include the database; configuration, source credentials and other files
outside it are not included.

## Manual command

```powershell
.\.venv\Scripts\python.exe -m infohub backup
# For scripts: make a copy only if the configured schedule is due.
.\.venv\Scripts\python.exe -m infohub backup --if-due
```

The command reports the snapshot path, verification result, SHA-256 hash and completion
time. A manual copy resets the next scheduled due time. A process lock prevents service
and CLI instances from making concurrent copies of the same database.

## Consistency and recovery

Info Hub uses SQLite's online backup API, including committed data in the write-ahead
log, without modifying the live database. A copy is built at a temporary path, checked
with `PRAGMA integrity_check`, hashed, and atomically published. The latest-success
record advances only after verification. Temporary `.pending-*` files are not completed
backups. A backup checks for shutdown and has a 15-minute time limit.

To restore a chosen completed `.sqlite3` copy:

1. Quit the desktop app and stop any separately running Info Hub service.
2. Preserve the current database and any adjacent `-wal` / `-shm` files in a separate
   recovery folder, then move that set out of the configured database path.
3. Copy the chosen completed snapshot to the path configured by `hub.database`
   (normally `data/hub.db`). Keep the old WAL/SHM files out of this restored path.
4. Restart Info Hub. Keep the preserved original until you have checked the restored data.

SQLite's integrity check validates the copy's internal consistency. The stored hash can
also be used to check whether the latest snapshot has changed since it was created.
