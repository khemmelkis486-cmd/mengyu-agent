"""SQLite storage for stable friends, invitations, and meetups."""

import sqlite3
from contextlib import closing, contextmanager

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE friends (
    friend_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    distance_band TEXT NOT NULL,
    familiarity INTEGER NOT NULL CHECK(familiarity BETWEEN 0 AND 100),
    status TEXT NOT NULL CHECK(status IN ('active','distant','paused')),
    interests_json TEXT NOT NULL,
    activities_json TEXT NOT NULL,
    contact_min_days INTEGER NOT NULL,
    contact_max_days INTEGER NOT NULL,
    last_contact_at REAL,
    last_meetup_at REAL,
    next_invite_after REAL NOT NULL,
    current_note TEXT NOT NULL,
    appearance TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE invitations (
    invite_id TEXT PRIMARY KEY,
    friend_id TEXT NOT NULL REFERENCES friends(friend_id),
    seq INTEGER NOT NULL,
    initiator TEXT NOT NULL CHECK(initiator IN ('friend','mengyu')),
    activity_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','invited','accepted','deferred','declined','scheduled','cancelled')),
    window TEXT NOT NULL,
    preferred_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    decision_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(friend_id, preferred_date, seq)
);
CREATE TABLE meetups (
    meetup_id TEXT PRIMARY KEY,
    invite_id TEXT NOT NULL UNIQUE REFERENCES invitations(invite_id),
    friend_ids_json TEXT NOT NULL,
    title TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scheduled','in_progress','completed','cancelled','no_show')),
    scheduled_start_at REAL,
    scheduled_end_at REAL,
    mode TEXT NOT NULL,
    place TEXT NOT NULL,
    life_slot_id TEXT UNIQUE,
    outcome TEXT NOT NULL DEFAULT '',
    mood TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    follow_up TEXT NOT NULL DEFAULT '',
    completed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Store:
    """Keep each operation in a short read or write transaction."""

    def __init__(self, path):
        self.path = path

    @contextmanager
    def use(self, *, write=False):
        """Open a private SQLite connection for an operation.

        Args:
            write: Reserve the database for a conditional state update.

        Yields:
            The open connection.
        """
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        """Create the three versioned tables on the first start."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self.use(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version == 1:
                db.execute(
                    "ALTER TABLE friends ADD COLUMN appearance TEXT NOT NULL DEFAULT ''"
                )
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                return
            if (
                version
                or db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("unsupported_friends_schema")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
