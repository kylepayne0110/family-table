# sqlite3 is Python's built-in SQLite driver.
import sqlite3

# Iterator types the request-scoped connection generator.
from collections.abc import Iterator

# contextmanager builds the write_transaction helper.
from contextlib import contextmanager

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

-- One row per planned week (keyed by its Monday). Version numbers let phones
-- ask "anything new?" cheaply; list_signature records which meals the
-- shopping list was built from (NULL = no list generated yet).
CREATE TABLE IF NOT EXISTS week_plans (
    week_start     TEXT    PRIMARY KEY CHECK (week_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    plan_version   INTEGER NOT NULL DEFAULT 0,
    list_version   INTEGER NOT NULL DEFAULT 0,
    list_signature TEXT,
    list_built_at  TEXT,
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Dinner for one day of a week (day 0 = Monday). Empty days simply have no row.
CREATE TABLE IF NOT EXISTS planned_meals (
    week_start TEXT    NOT NULL REFERENCES week_plans(week_start) ON DELETE CASCADE,
    day        INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    PRIMARY KEY (week_start, day)
);

-- One line on a week's shopping list. item_key is the normalized name, so the
-- same ingredient can never appear twice in a week. rev increases on every
-- change so a stale phone can't overwrite a newer edit.
CREATE TABLE IF NOT EXISTS shopping_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start  TEXT    NOT NULL REFERENCES week_plans(week_start) ON DELETE CASCADE,
    item_key    TEXT    NOT NULL CHECK (length(item_key) > 0),
    name        TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    quantity    TEXT    NOT NULL DEFAULT '',   -- combined from recipes, e.g. "3 lbs"
    totals_json TEXT    NOT NULL DEFAULT '{}', -- machine-readable totals for change detection
    manual      INTEGER NOT NULL DEFAULT 0,    -- 1 = someone added it by hand
    manual_qty  TEXT    NOT NULL DEFAULT '',   -- quantity typed with a manual item
    status      TEXT    NOT NULL DEFAULT 'need'
                CHECK (status IN ('need', 'bought', 'have', 'staple')),
    stale       INTEGER NOT NULL DEFAULT 0,    -- legacy: 1 = flagged by an older version; removed on the next Update
    change_note TEXT    NOT NULL DEFAULT '',   -- e.g. "Was 1 lb, now 3 lbs"
    rev         INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    cleared     INTEGER NOT NULL DEFAULT 0,    -- 1 = purchased, then hidden with Clear Purchased
    bought_totals TEXT  NOT NULL DEFAULT '',   -- totals_json when it was purchased ('' = not recorded)
    UNIQUE (week_start, item_key)
);

-- Which recipes need each item. The recipe name is copied so the line still
-- reads correctly if the recipe is later deleted.
CREATE TABLE IF NOT EXISTS shopping_item_sources (
    item_id     INTEGER NOT NULL REFERENCES shopping_items(id) ON DELETE CASCADE,
    recipe_id   INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    recipe_name TEXT    NOT NULL,
    amount      TEXT    NOT NULL DEFAULT '',
    times       INTEGER NOT NULL DEFAULT 1 CHECK (times > 0)
);

-- Ingredients the family normally keeps at home (shared across all weeks).
CREATE TABLE IF NOT EXISTS staples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    name_key   TEXT    NOT NULL UNIQUE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Small key/value notes the app keeps about itself (e.g. "staples seeded").
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingredients_recipe ON ingredients(recipe_id, position);
CREATE INDEX IF NOT EXISTS idx_planned_recipe ON planned_meals(recipe_id);
CREATE INDEX IF NOT EXISTS idx_item_sources_item ON shopping_item_sources(item_id);
CREATE INDEX IF NOT EXISTS idx_item_sources_recipe ON shopping_item_sources(recipe_id);
CREATE INDEX IF NOT EXISTS idx_steps_recipe ON steps(recipe_id, position);
CREATE INDEX IF NOT EXISTS idx_notes_recipe ON notes(recipe_id, created_at);
"""


# Columns added after the first release: (table, column, definition).
# Older databases get them via ALTER TABLE; existing rows are kept.
ADDED_COLUMNS = [
    ("recipes", "source_url", "TEXT NOT NULL DEFAULT ''"),
    ("shopping_items", "cleared", "INTEGER NOT NULL DEFAULT 0"),
    ("shopping_items", "bought_totals", "TEXT NOT NULL DEFAULT ''"),
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


# Starting Kitchen Staples. Added once; anything the family deletes stays deleted.
DEFAULT_STAPLES = [
    "Salt", "Black pepper", "Garlic powder", "Onion powder", "Paprika",
    "Chili powder", "Olive oil", "Butter", "Milk",
]


def migrate(conn: sqlite3.Connection) -> None:
    """Add any missing columns and first-run data. Safe to run on every startup."""
    for table, column, definition in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            # Names come from the constant list above, never from user input.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # Seed default staples only the first time; the flag stops deleted ones coming back.
    if conn.execute("SELECT 1 FROM app_meta WHERE key = 'staples_seeded'").fetchone() is None:
        from app.ingredients import name_key  # imported here to keep database.py light
        conn.executemany(
            "INSERT OR IGNORE INTO staples (name, name_key) VALUES (?, ?)",
            [(name, name_key(name)) for name in DEFAULT_STAPLES],
        )
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('staples_seeded', datetime('now'))")


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Take SQLite's write lock up front, so check-then-write steps from two phones can't interleave."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()
