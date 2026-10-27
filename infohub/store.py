"""SQLite store for all three layers. Message text lives only here (data/ is git-ignored).

Layer 1 (sync):     messages, channels, segments, events, cursors, polls, overrides
Layer 2 (organize): units, unit_tags, unit_links (graph: a unit can have many parents/children),
                    unit_messages (evidence), jobs (AI/agent queue), llm_usage
Layer 3 (apps):     reads the above; units.reminded_at for reminders
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- ---------- Layer 1 ----------
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, account TEXT NOT NULL DEFAULT '', item_key TEXT NOT NULL,
  channel TEXT, channel_name TEXT, is_group INTEGER DEFAULT 0,
  sender TEXT, sender_name TEXT, sent_at INTEGER, kind TEXT,
  text TEXT, link TEXT, mentions_me INTEGER DEFAULT 0, from_me INTEGER DEFAULT 0,
  flags TEXT, policy TEXT, priority TEXT, labels TEXT, rule TEXT,
  segment_id INTEGER, done INTEGER DEFAULT 0, collected_at INTEGER,
  UNIQUE(source, account, item_key));
CREATE INDEX IF NOT EXISTS m_time ON messages(sent_at);
CREATE INDEX IF NOT EXISTS m_chan ON messages(source, account, channel, sent_at);
CREATE INDEX IF NOT EXISTS m_pri ON messages(priority, done, sent_at);
CREATE INDEX IF NOT EXISTS m_seg ON messages(segment_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, channel_name, sender_name, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text, channel_name, sender_name)
  VALUES (new.id, new.text, new.channel_name, new.sender_name); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text, channel_name, sender_name)
  VALUES ('delete', old.id, old.text, old.channel_name, old.sender_name); END;

CREATE TABLE IF NOT EXISTS channels(
  source TEXT, account TEXT DEFAULT '', channel TEXT, channel_name TEXT, is_group INTEGER,
  policy TEXT, unread INTEGER, last_at INTEGER, muted INTEGER, chat_mode TEXT,
  PRIMARY KEY(source, account, channel));
CREATE TABLE IF NOT EXISTS segments(
  id INTEGER PRIMARY KEY, source TEXT, account TEXT, channel TEXT, channel_name TEXT, policy TEXT,
  start_at INTEGER, end_at INTEGER, n INTEGER DEFAULT 0, closed INTEGER DEFAULT 0,
  rule_done INTEGER DEFAULT 0, ai_done INTEGER DEFAULT 0, updated_at INTEGER);
CREATE INDEX IF NOT EXISTS s_chan ON segments(source, account, channel, end_at);
CREATE TABLE IF NOT EXISTS events(
  source TEXT, account TEXT DEFAULT '', item_key TEXT, title TEXT, start_at INTEGER, end_at INTEGER,
  location TEXT, link TEXT, PRIMARY KEY(source, account, item_key));
CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY, value INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS overrides(
  kind TEXT, source TEXT, value TEXT, label TEXT, created_at INTEGER, PRIMARY KEY(kind, source, value));
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user TEXT, created_at INTEGER, expires_at INTEGER, last_seen INTEGER, client TEXT);
CREATE TABLE IF NOT EXISTS polls(
  id INTEGER PRIMARY KEY, layer TEXT, source TEXT, started_at INTEGER, finished_at INTEGER,
  status TEXT, stored INTEGER, detail TEXT, error TEXT);

-- ---------- Layer 2 ----------
CREATE TABLE IF NOT EXISTS units(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE,
  kind TEXT, title TEXT, summary TEXT, status TEXT DEFAULT 'open', priority TEXT,
  due_at INTEGER, start_at INTEGER, end_at INTEGER,
  origin TEXT, confidence REAL, created_at INTEGER, updated_at INTEGER, last_activity_at INTEGER,
  reminded_at INTEGER, for_me INTEGER DEFAULT 0, polished INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS u_kind ON units(kind, status, due_at);
CREATE INDEX IF NOT EXISTS u_act ON units(last_activity_at);
CREATE TABLE IF NOT EXISTS unit_tags(unit_id INTEGER, tag TEXT, PRIMARY KEY(unit_id, tag));
CREATE INDEX IF NOT EXISTS ut_tag ON unit_tags(tag);
CREATE TABLE IF NOT EXISTS unit_links(parent_id INTEGER, child_id INTEGER, relation TEXT DEFAULT 'part_of',
  PRIMARY KEY(parent_id, child_id, relation));
CREATE INDEX IF NOT EXISTS ul_child ON unit_links(child_id);
CREATE TABLE IF NOT EXISTS unit_messages(unit_id INTEGER, message_id INTEGER, PRIMARY KEY(unit_id, message_id));
CREATE INDEX IF NOT EXISTS um_msg ON unit_messages(message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
  title, summary, content='units', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS units_ai AFTER INSERT ON units BEGIN
  INSERT INTO units_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary); END;
CREATE TRIGGER IF NOT EXISTS units_au AFTER UPDATE OF title, summary ON units BEGIN
  INSERT INTO units_fts(units_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
  INSERT INTO units_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary); END;
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, ref_id INTEGER, status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, not_before INTEGER DEFAULT 0, last_error TEXT,
  created_at INTEGER, updated_at INTEGER, UNIQUE(kind, ref_id));
CREATE TABLE IF NOT EXISTS llm_usage(day TEXT, model TEXT, requests INTEGER, input_tokens INTEGER,
  output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL,
  PRIMARY KEY(day, model));

CREATE TABLE IF NOT EXISTS task_schedules(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER DEFAULT 1,
  mode TEXT NOT NULL, daily_time TEXT, interval_minutes INTEGER,
  prompt TEXT NOT NULL, next_run INTEGER, last_run INTEGER,
  last_status TEXT, last_error TEXT, created_at INTEGER, updated_at INTEGER);
CREATE INDEX IF NOT EXISTS ts_due ON task_schedules(enabled, next_run);
"""

MSG_FIELDS = ["source", "account", "item_key", "channel", "channel_name", "is_group", "sender", "sender_name",
              "sent_at", "kind", "text", "link", "mentions_me", "from_me", "flags", "policy", "priority", "labels", "rule"]


def _migrate_v1(path: Path):
    """v1 (single 'items' table) -> v2: keep the old file as a backup, carry over dashboard overrides."""
    con = sqlite3.connect(str(path))
    has_items = con.execute("SELECT 1 FROM sqlite_master WHERE name='items'").fetchone()
    overrides = con.execute("SELECT * FROM overrides").fetchall() if has_items else []
    con.close()
    if not has_items:
        return []
    backup = path.with_name(path.stem + ".v1.bak.db")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            shutil.move(str(src), str(backup) + suffix)
    return overrides


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        carried = _migrate_v1(path) if path.exists() else []
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(units)")}
        for col in ("for_me", "polished"):          # v2 -> v3
            if col not in cols:
                self.db.execute(f"ALTER TABLE units ADD COLUMN {col} INTEGER DEFAULT 0")
        ccols = {r[1] for r in self.db.execute("PRAGMA table_info(channels)")}
        for col, kind in (("muted", "INTEGER"), ("chat_mode", "TEXT")):   # v3 -> v4
            if col not in ccols:
                self.db.execute(f"ALTER TABLE channels ADD COLUMN {col} {kind}")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        if carried:
            self.db.executemany("INSERT OR IGNORE INTO overrides VALUES(?,?,?,?,?)", carried)
        self.db.commit()

    def q(self, sql, args=()):
        return self.db.execute(sql, args)

    # ---- cursors
    def cursor(self, key: str) -> int | None:
        row = self.q("SELECT value FROM cursors WHERE source=?", (key,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, key: str, value: int):
        self.q("INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               (key, int(value), int(time.time())))
        self.db.commit()

    # ---- messages
    def add_messages(self, items: list[dict]) -> list[int]:
        """Insert new messages; returns ids of rows actually inserted (duplicates are skipped)."""
        now, new_ids = int(time.time()), []
        for it in items:
            it.setdefault("account", "")
            vals = [json.dumps(it.get(f), ensure_ascii=False) if f in ("labels", "flags") else it.get(f) for f in MSG_FIELDS]
            cur = self.q(f"INSERT OR IGNORE INTO messages({','.join(MSG_FIELDS)},collected_at) "
                         f"VALUES({','.join('?' * len(MSG_FIELDS))},?)", vals + [now])
            if cur.rowcount:
                new_ids.append(cur.lastrowid)
        self.db.commit()
        return new_ids

    def upsert_channel(self, source, account, channel, name, is_group, policy, unread=None, last_at=None,
                       muted=None, chat_mode=None):
        self.q("""INSERT INTO channels VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,account,channel) DO UPDATE SET
            channel_name=excluded.channel_name, is_group=excluded.is_group, policy=excluded.policy,
            unread=coalesce(excluded.unread, channels.unread), last_at=max(coalesce(excluded.last_at,0), coalesce(channels.last_at,0)),
            muted=coalesce(excluded.muted, channels.muted), chat_mode=coalesce(excluded.chat_mode, channels.chat_mode)""",
               (source, account or "", channel, name, int(bool(is_group)), policy, unread, last_at,
                None if muted is None else int(bool(muted)), chat_mode))

    def replace_events(self, source, account, events: list[dict]):
        self.q("DELETE FROM events WHERE source=? AND account=?", (source, account or ""))
        self.db.executemany("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?)",
                            [(source, account or "", e["item_key"], e["title"], e["start_at"], e.get("end_at"),
                              e.get("location"), e.get("link")) for e in events])
        self.db.commit()

    # ---- overrides (dashboard "teach" buttons)
    def overrides(self) -> dict:
        out = {}
        for r in self.q("SELECT kind, source, value FROM overrides"):
            out.setdefault(r["kind"], set()).add((r["source"], r["value"]))
        return out

    def add_override(self, kind, source, value, label=""):
        self.q("INSERT OR REPLACE INTO overrides VALUES(?,?,?,?,?)", (kind, source, value, label, int(time.time())))
        self.db.commit()

    def remove_override(self, kind, source, value):
        self.q("DELETE FROM overrides WHERE kind=? AND source=? AND value=?", (kind, source, value))
        self.db.commit()

    def mark_done(self, message_id: int, done: bool = True):
        self.q("UPDATE messages SET done=? WHERE id=?", (int(done), message_id))
        self.db.commit()

    def update_message_content(self, message_id: int, *, text: str, kind: str | None = None,
                               sender: str | None = None, sender_name: str | None = None,
                               link: str | None = None, mentions_me: bool | None = None,
                               flags: dict | None = None) -> bool:
        """Repair source text without changing user triage fields such as priority or done."""
        old = self.q("SELECT text FROM messages WHERE id=?", (int(message_id),)).fetchone()
        if not old:
            return False
        fields = ["text=?"]
        args = [text]
        for key, value in (("kind", kind), ("sender", sender), ("sender_name", sender_name),
                           ("link", link), ("mentions_me", None if mentions_me is None else int(mentions_me)),
                           ("flags", None if flags is None else json.dumps(flags, ensure_ascii=False))):
            if value is not None:
                fields.append(f"{key}=?")
                args.append(value)
        args.append(int(message_id))
        self.q(f"UPDATE messages SET {','.join(fields)} WHERE id=?", args)
        # messages_fts has external content and only insert/delete triggers; rebuild after repair.
        self.q("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        self.db.commit()
        return True

    def log(self, layer, source, started, status, stored=0, detail=None, error=None):
        self.q("INSERT INTO polls(layer,source,started_at,finished_at,status,stored,detail,error) VALUES(?,?,?,?,?,?,?,?)",
               (layer, source, started, int(time.time()), status, stored,
                json.dumps(detail, ensure_ascii=False) if detail else None, error))
        self.db.commit()

    # ---- user-defined assistant schedules
    def schedules(self):
        return [dict(r) for r in self.q("SELECT * FROM task_schedules ORDER BY enabled DESC, name, id")]

    def schedule(self, sid: int):
        row = self.q("SELECT * FROM task_schedules WHERE id=?", (int(sid),)).fetchone()
        return dict(row) if row else None

    def save_schedule(self, data: dict, next_run: int, sid: int | None = None) -> dict:
        now = int(time.time())
        if sid:
            self.q("""UPDATE task_schedules SET name=?, enabled=?, mode=?, daily_time=?, interval_minutes=?,
                       prompt=?, next_run=?, updated_at=? WHERE id=?""",
                   (data["name"], int(data["enabled"]), data["mode"], data.get("daily_time"),
                    data.get("interval_minutes"), data["prompt"], int(next_run), now, int(sid)))
            out = self.schedule(sid)
        else:
            cur = self.q("""INSERT INTO task_schedules(name,enabled,mode,daily_time,interval_minutes,prompt,
                       next_run,last_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         (data["name"], int(data["enabled"]), data["mode"], data.get("daily_time"),
                          data.get("interval_minutes"), data["prompt"], int(next_run), "never", now, now))
            out = self.schedule(cur.lastrowid)
        self.db.commit()
        return out

    def delete_schedule(self, sid: int) -> bool:
        cur = self.q("DELETE FROM task_schedules WHERE id=?", (int(sid),))
        self.db.commit()
        return bool(cur.rowcount)

    def due_schedules(self, now: int | None = None) -> list[dict]:
        return [dict(r) for r in self.q("""SELECT * FROM task_schedules
                                         WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?
                                           AND coalesce(last_status,'')!='running'
                                         ORDER BY next_run, id LIMIT 8""", (int(now or time.time()),))]

    def mark_schedule_running(self, sid: int, now: int | None = None) -> bool:
        cur = self.q("UPDATE task_schedules SET last_status='running', last_error=NULL, updated_at=? "
                     "WHERE id=? AND enabled=1 AND coalesce(last_status,'')!='running'",
                     (int(now or time.time()), int(sid)))
        self.db.commit()
        return bool(cur.rowcount)

    def finish_schedule(self, sid: int, *, next_run: int, status: str, error: str | None = None,
                        now: int | None = None):
        t = int(now or time.time())
        self.q("""UPDATE task_schedules SET last_run=?, next_run=?, last_status=?, last_error=?, updated_at=?
                  WHERE id=?""", (t, int(next_run), status, error, t, int(sid)))
        self.db.commit()

    def prune(self, retention_days: int):
        cutoff = int(time.time()) - retention_days * 86400
        self.q("""DELETE FROM messages WHERE sent_at < ? AND (done=1 OR priority='P3')
                  AND id NOT IN (SELECT message_id FROM unit_messages)""", (cutoff,))
        self.q("DELETE FROM polls WHERE finished_at < ?", (cutoff,))
        self.db.commit()
