# sqlite3 is Python's built-in SQLite driver.
import sqlite3

# Iterator types the request-scoped connection generator.
from collections.abc import Iterator

# Path provides filesystem utilities.
from pathlib import Path

# Request gives dependencies access to app settings.
from fastapi import Request


# Tables are created only if missing, so existing data is never touched.
SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- AUTOINCREMENT never reuses ids
    name           TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    description    TEXT    NOT NULL DEFAULT '',
    category       TEXT    NOT NULL,
    cuisine        TEXT    NOT NULL DEFAULT '',
    prep_minutes   INTEGER CHECK (prep_minutes IS NULL OR prep_minutes >= 0),
    cook_minutes   INTEGER CHECK (cook_minutes IS NULL OR cook_minutes >= 0),
    servings       INTEGER CHECK (servings IS NULL OR servings > 0),
    photo_filename TEXT,
    card_tag       TEXT    NOT NULL DEFAULT '',
    last_made      TEXT,   -- ISO date, e.g. 2026-11-14
    source_url     TEXT    NOT NULL DEFAULT '',  -- where an imported recipe came from
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingredients (
    id        INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    amount    TEXT    NOT NULL DEFAULT '',
    name      TEXT    NOT NULL CHECK (length(name) > 0)
);

CREATE TABLE IF NOT EXISTS steps (
    id        INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    title     TEXT    NOT NULL DEFAULT '',
    body      TEXT    NOT NULL CHECK (length(body) > 0)
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    author     TEXT    NOT NULL CHECK (length(author) > 0),
    body       TEXT    NOT NULL CHECK (length(body) > 0),
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,  -- SHA-256 of the cookie value
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ingredients_recipe ON ingredients(recipe_id, position);
CREATE INDEX IF NOT EXISTS idx_steps_recipe ON steps(recipe_id, position);
CREATE INDEX IF NOT EXISTS idx_notes_recipe ON notes(recipe_id, created_at);
"""


# Columns added after the first release: (table, column, definition).
# Older databases get them via ALTER TABLE; existing rows are kept.
ADDED_COLUMNS = [
    ("recipes", "source_url", "TEXT NOT NULL DEFAULT ''"),
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on and dict-like rows."""
    # check_same_thread=False: FastAPI may hand a request between threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    """Create data folders and any missing tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        # WAL mode lets reads continue while a write is in progress.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    """Add any missing columns. Safe to run on every startup."""
    for table, column, definition in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            # Names come from the constant list above, never from user input.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()
