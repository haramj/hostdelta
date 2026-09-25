"""Private local SQLite storage. Session starts and read cursors are separate."""

import json
import os
import re
import sqlite3
from pathlib import Path

DEFAULT_WATCH = ["/etc/systemd/system", "/etc/netplan", "/etc/ssh/sshd_config"]


def default_dir():
    return Path(os.environ.get("HOSTDELTA_STATE_DIR", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "hostdelta")))


class Store:
    def __init__(self, directory, create=False):
        self.directory = Path(directory).expanduser().absolute()
        if not create and not (self.directory / "state.sqlite3").is_file():
            raise ValueError("HostDelta is not initialized. Run: hostdelta init")
        if self.directory.is_symlink():
            raise ValueError("State directory must not be a symlink.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("State directory must be owned by you with permissions 0700.")
        path = self.directory / "state.sqlite3"
        if path.is_symlink():
            raise ValueError("Database must not be a symlink.")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=3)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.close()
            raise ValueError(f"Unsupported database schema {version}; upgrade HostDelta.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, label TEXT UNIQUE, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS snapshot_time ON snapshots(at);
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, token TEXT UNIQUE, since TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_time ON events(at);
            CREATE TABLE IF NOT EXISTS archive_events (
                event_id TEXT PRIMARY KEY, at TEXT NOT NULL, observed_at TEXT NOT NULL,
                source TEXT NOT NULL, service TEXT, severity TEXT, trace_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS archive_time ON archive_events(at);
            CREATE INDEX IF NOT EXISTS archive_observed ON archive_events(observed_at);
            CREATE INDEX IF NOT EXISTS archive_trace ON archive_events(trace_id,at);
            CREATE TABLE IF NOT EXISTS archive_http (
                event_id TEXT PRIMARY KEY REFERENCES archive_events(event_id) ON DELETE CASCADE,
                at TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, ip TEXT NOT NULL,
                status INTEGER NOT NULL, slow INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS archive_http_time ON archive_http(at);
            CREATE TABLE IF NOT EXISTS checkpoints (source TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_runs (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS source_run_time ON source_runs(at);
            CREATE TABLE IF NOT EXISTS monitors (entity TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, entity TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS incident_window ON incidents(opened_at,closed_at);
            PRAGMA user_version=2;
        """)

    def close(self):
        self.db.close()

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def save(self, snapshot, label=None):
        if label and (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", label) or label == "HEAD"):
            raise ValueError("Label must begin with a letter, use letters/digits/._-, and not be HEAD.")
        with self.db:
            try:
                cursor = self.db.execute("INSERT INTO snapshots(at,label,payload) VALUES (?,?,?)", (snapshot["at"], label, json.dumps(snapshot)))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Checkpoint label already exists: {label}") from exc
        return cursor.lastrowid

    def resolve(self, reference="HEAD"):
        match = re.fullmatch(r"HEAD(?:~(\d+))?", reference)
        if match:
            row = self.db.execute("SELECT * FROM snapshots ORDER BY at DESC,id DESC LIMIT 1 OFFSET ?", (int(match[1] or 0),)).fetchone()
        elif reference.isdigit():
            row = self.db.execute("SELECT * FROM snapshots WHERE id=?", (int(reference),)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM snapshots WHERE label=?", (reference,)).fetchone()
        if not row:
            raise ValueError(f"Snapshot not found: {reference}")
        return json.loads(row["payload"])

    def baseline(self, at):
        row = self.db.execute("SELECT payload FROM snapshots WHERE at<=? ORDER BY at DESC,id DESC LIMIT 1", (at,)).fetchone()
        return json.loads(row[0]) if row else None

    def last_since(self):
        row = self.db.execute("SELECT since FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def last_session(self):
        row = self.db.execute("SELECT at FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def begin_session(self, at, fallback, token=None):
        """Atomically claim a session; overlapping shells must not share a cursor."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if token and self.db.execute("SELECT 1 FROM sessions WHERE token=?", (token,)).fetchone():
                return None
            row = self.db.execute("SELECT at FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
            since = row[0] if row else fallback
            self.db.execute("INSERT INTO sessions(at,token,since) VALUES (?,?,?)", (at, token, since))
        return since

    def snapshots(self):
        return [dict(row) for row in self.db.execute("SELECT id,at,label FROM snapshots ORDER BY at DESC,id DESC LIMIT 100")]

    def ack(self, consumer, until):
        key = "consumer:" + consumer
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.setting(key)
            if previous and until < previous:
                raise ValueError("Consumer cursor cannot move backwards.")
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(until)))

    def record(self, event):
        with self.db:
            cursor = self.db.execute("INSERT INTO events(at,payload) VALUES (?,?)", (event["at"], json.dumps(event)))
        return cursor.lastrowid

    def events(self, since, until):
        rows = self.db.execute("SELECT id,payload FROM events WHERE at>? AND at<=? ORDER BY at,id LIMIT 10001", (since, until)).fetchall()
        result = []
        for row in rows[:10000]:
            item = json.loads(row["payload"])
            item["ref"] = f"local-event:{row['id']}"
            result.append(item)
        return result, len(rows) > 10000
