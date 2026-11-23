# sqlite3 is Python's built-in SQLite driver.
import sqlite3

# Iterator types the request-scoped connection generator.
from collections.abc import Iterator

# contextmanager builds the write_transaction helper.
from contextlib import contextmanager

# fcntl locks the data folder so only one server uses it at a time (Linux/WSL and Docker).
import fcntl

# Path provides filesystem utilities.
from pathlib import Path

# Request gives dependencies access to app settings.
from fastapi import Request


# The current database layout. PRAGMA user_version records which layout a file has:
#   0 = a single-family database from before accounts (or a brand-new file)
#   2 = individual accounts and shared Tables (this release)
SCHEMA_VERSION = 2

# Every table is created only if missing, so existing data is never touched.
# Each family's data belongs to a Table (family_tables); people reach it through memberships.
SCHEMA = """
-- ---- Accounts and Tables ----

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT    NOT NULL UNIQUE COLLATE NOCASE CHECK (length(username) BETWEEN 3 AND 40),
    display_name        TEXT    NOT NULL CHECK (length(display_name) BETWEEN 1 AND 60),
    password_hash       TEXT    NOT NULL,
    last_table_id       INTEGER REFERENCES family_tables(id) ON DELETE SET NULL,  -- remembered Table
    password_changed_at TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- A Table: the shared space where a family keeps recipes, dinners, and groceries.
CREATE TABLE IF NOT EXISTS family_tables (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Every member of a Table has the same rights (rename, invite, remove).
CREATE TABLE IF NOT EXISTS memberships (
    table_id   INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    joined_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (table_id, user_id)
);

-- One invite link per person. Only a hash of the link's token is stored.
CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    token_hash  TEXT    NOT NULL UNIQUE,
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    inviter_name TEXT   NOT NULL,                 -- shown as "Kyle invited you"
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT    NOT NULL,
    revoked_at  TEXT,
    accepted_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    accepted_at TEXT
);

-- One-time password reset links made by the server owner (python -m app.accounts reset-link).
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT    NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT    PRIMARY KEY,  -- SHA-256 of the cookie value
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token      TEXT    NOT NULL,
    active_table_id INTEGER REFERENCES family_tables(id) ON DELETE SET NULL,
    device          TEXT    NOT NULL DEFAULT '',  -- e.g. "iPhone · Safari", for the sessions list
    expires_at      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Photos waiting to be saved with a recipe (from Import or a draft). Only the person who
-- added one can see or use it.
CREATE TABLE IF NOT EXISTS temp_photos (
    filename   TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    table_id   INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---- A Table's recipe book ----

CREATE TABLE IF NOT EXISTS recipes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,  -- AUTOINCREMENT never reuses ids
    table_id       INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    description    TEXT    NOT NULL DEFAULT '',
    category       TEXT    NOT NULL,
    cuisine        TEXT    NOT NULL DEFAULT '',
    prep_minutes   INTEGER CHECK (prep_minutes IS NULL OR prep_minutes >= 0),
    cook_minutes   INTEGER CHECK (cook_minutes IS NULL OR cook_minutes >= 0),
    servings       INTEGER CHECK (servings IS NULL OR servings > 0),
    photo_filename TEXT,
    card_tag       TEXT    NOT NULL DEFAULT '',
    last_made      TEXT,   -- ISO date, e.g. 2026-11-14 (set by "Mark as made")
    source_url     TEXT    NOT NULL DEFAULT '',  -- where an imported recipe came from
    creator_credit TEXT    NOT NULL DEFAULT '',  -- who created the recipe, e.g. "Grandma"
    added_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- who entered it
    community_listing_id INTEGER REFERENCES community_listings(id) ON DELETE SET NULL,  -- saved from Community
    community_version    INTEGER,                    -- which version of that listing was saved
    community_credit     TEXT NOT NULL DEFAULT '',   -- "Shared by Kyle", kept even if the listing goes away
    version        INTEGER NOT NULL DEFAULT 1,       -- bumped on every content edit (stale-edit checks)
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

-- Signed family notes on a recipe card. Private: never published to the community.
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    author     TEXT    NOT NULL CHECK (length(author) > 0),
    body       TEXT    NOT NULL CHECK (length(body) > 0),
    added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---- A Table's weekly plans and grocery lists ----

-- One row per planned week (keyed by its Monday). Version numbers let phones
-- ask "anything new?" cheaply. list_signature and list_built_at are left from the old
-- whole-week "Generate Shopping List" and are no longer used.
CREATE TABLE IF NOT EXISTS week_plans (
    table_id       INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    week_start     TEXT    NOT NULL CHECK (week_start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    plan_version   INTEGER NOT NULL DEFAULT 0,
    list_version   INTEGER NOT NULL DEFAULT 0,
    list_signature TEXT,
    list_built_at  TEXT,
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (table_id, week_start)
);

-- Dinner for one day of a week (day 0 = Monday). Empty days simply have no row.
-- id stays with the dinner when it moves to another day, so its serving count,
-- shopping additions, and assistant chat move with it. At most one dinner per day:
-- the planner checks that inside a write transaction (a UNIQUE index would block swaps).
-- Rows for past days are kept as the family's history.
-- recipe_id is NULL for a scanned packaged meal (its details live in packaged_meals).
CREATE TABLE IF NOT EXISTS planned_meals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id   INTEGER NOT NULL,
    week_start TEXT    NOT NULL,
    day        INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),
    recipe_id  INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
    servings   INTEGER CHECK (servings IS NULL OR servings BETWEEN 1 AND 100),  -- NULL = the recipe's own yield
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (table_id, week_start) REFERENCES week_plans(table_id, week_start) ON DELETE CASCADE
);

-- A store-bought meal planned for one dinner (scanned or typed in). It belongs to that
-- planned dinner only and never becomes a recipe in the recipe box.
CREATE TABLE IF NOT EXISTS packaged_meals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id              INTEGER NOT NULL UNIQUE REFERENCES planned_meals(id) ON DELETE CASCADE,
    barcode              TEXT    NOT NULL DEFAULT '' CHECK (barcode = '' OR barcode NOT GLOB '*[^0-9]*'),
    name                 TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    brand                TEXT    NOT NULL DEFAULT '',
    package_size         TEXT    NOT NULL DEFAULT '',
    product_source       TEXT    NOT NULL DEFAULT 'manual',  -- 'openfoodfacts' or 'manual'
    oven_directions      TEXT    NOT NULL DEFAULT '',
    oven_source          TEXT    NOT NULL DEFAULT '',        -- 'openfoodfacts', 'typed', or ''
    microwave_directions TEXT    NOT NULL DEFAULT '',
    microwave_source     TEXT    NOT NULL DEFAULT '',
    other_directions     TEXT    NOT NULL DEFAULT '',
    other_source         TEXT    NOT NULL DEFAULT '',
    label_photo          TEXT,                                -- photo of the cooking panel (uploads/)
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- One line on a week's shopping list. item_key is the normalized name, so the
-- same ingredient can never appear twice in a week. rev increases on every
-- change so a stale phone can't overwrite a newer edit.
CREATE TABLE IF NOT EXISTS shopping_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL,
    week_start  TEXT    NOT NULL,
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
    UNIQUE (table_id, week_start, item_key),
    FOREIGN KEY (table_id, week_start) REFERENCES week_plans(table_id, week_start) ON DELETE CASCADE
);

-- Which recipes need each item. The recipe name is copied so the line still
-- reads correctly if the recipe is later deleted.
CREATE TABLE IF NOT EXISTS shopping_item_sources (
    item_id     INTEGER NOT NULL REFERENCES shopping_items(id) ON DELETE CASCADE,
    recipe_id   INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    recipe_name TEXT    NOT NULL,
    amount      TEXT    NOT NULL DEFAULT '',
    times       INTEGER NOT NULL DEFAULT 1 CHECK (times > 0),
    meal_id     INTEGER REFERENCES planned_meals(id) ON DELETE SET NULL,
    ingredient  TEXT    NOT NULL DEFAULT '',
    servings    INTEGER
);

-- "Add ingredients" requests already applied, so a double tap or a retried request
-- never adds the same selection twice.
CREATE TABLE IF NOT EXISTS meal_list_requests (
    request_key TEXT    PRIMARY KEY,
    meal_id     INTEGER NOT NULL REFERENCES planned_meals(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The Cooking Assistant conversation for one planned dinner (typed and spoken turns).
CREATE TABLE IF NOT EXISTS meal_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id     INTEGER NOT NULL REFERENCES planned_meals(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    body        TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'text',  -- text, clarify, servings, proposal, offline, step
    data_json   TEXT    NOT NULL DEFAULT '{}',    -- e.g. a pending substitution awaiting an answer
    request_key TEXT,                             -- the browser's id for a sent message (retries)
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- A permanent recipe change the assistant suggested. Saved only after Confirm.
CREATE TABLE IF NOT EXISTS recipe_edit_proposals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id      INTEGER NOT NULL REFERENCES planned_meals(id) ON DELETE CASCADE,
    recipe_id    INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    message_id   INTEGER REFERENCES meal_messages(id) ON DELETE SET NULL,
    changes_json TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'confirmed', 'cancelled', 'stale')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at   TEXT
);

-- "Cook with me": where the cook is in tonight's recipe. One row per planned dinner, so
-- planning the same recipe twice gives two separate cooking sessions.
CREATE TABLE IF NOT EXISTS cook_sessions (
    meal_id    INTEGER PRIMARY KEY REFERENCES planned_meals(id) ON DELETE CASCADE,
    step       INTEGER NOT NULL DEFAULT 0,      -- 0 = not started; otherwise the current step (1-based)
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Ingredients a Table normally keeps at home (shared across all weeks).
CREATE TABLE IF NOT EXISTS staples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id   INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    name_key   TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (table_id, name_key)
);

-- ---- Recipe drafts (Add a recipe, including the voice helper) ----

-- An unsaved recipe form, kept so a lock screen, lost connection, or reload doesn't lose
-- work. A draft is never a recipe: only the form's Save button adds one to the recipe book.
CREATE TABLE IF NOT EXISTS recipe_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id    INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipe_id   INTEGER REFERENCES recipes(id) ON DELETE CASCADE,  -- set when editing an existing recipe
    fields_json TEXT    NOT NULL DEFAULT '{}',
    voice_json  TEXT    NOT NULL DEFAULT '{}',  -- the voice helper's notes: skipped fields, pending question
    rev         INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS draft_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES recipe_drafts(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    body        TEXT    NOT NULL,
    request_key TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---- Community recipes (shared across the app, opt-in per recipe) ----

-- A published snapshot. Editing the family recipe never changes it; only an explicit
-- "Update community recipe" makes a new version.
CREATE TABLE IF NOT EXISTS community_listings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table_id  INTEGER REFERENCES family_tables(id) ON DELETE SET NULL,
    source_recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    source_recipe_version INTEGER,               -- the family recipe's version when last published
    publisher_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    publisher_name   TEXT    NOT NULL,           -- display name only, never a username
    status           TEXT    NOT NULL DEFAULT 'published'
                     CHECK (status IN ('published', 'withdrawn', 'hidden', 'removed')),
    version          INTEGER NOT NULL DEFAULT 1,
    name             TEXT    NOT NULL,
    description      TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL DEFAULT '',
    cuisine          TEXT    NOT NULL DEFAULT '',
    prep_minutes     INTEGER,
    cook_minutes     INTEGER,
    servings         INTEGER,
    creator_credit   TEXT    NOT NULL DEFAULT '',
    photo_filename   TEXT,
    ingredient_count INTEGER NOT NULL DEFAULT 0,
    report_count     INTEGER NOT NULL DEFAULT 0,
    published_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    withdrawn_at     TEXT
);

-- Every published version, so "Save" copies exactly the version the person was looking at.
CREATE TABLE IF NOT EXISTS community_versions (
    listing_id     INTEGER NOT NULL REFERENCES community_listings(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    snapshot_json  TEXT    NOT NULL,
    photo_filename TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (listing_id, version)
);

-- Saves into a Table: one per listing version per Table, so a repeat tap never makes two copies.
CREATE TABLE IF NOT EXISTS community_saves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES community_listings(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    table_id   INTEGER NOT NULL REFERENCES family_tables(id) ON DELETE CASCADE,
    recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    saved_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (listing_id, version, table_id)
);

CREATE TABLE IF NOT EXISTS community_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL REFERENCES community_listings(id) ON DELETE CASCADE,
    reporter_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason      TEXT    NOT NULL,
    details     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (listing_id, reporter_id)
);

-- Full-text index of published listings (dish name, ingredients, cuisine). rowid = listing id.
CREATE VIRTUAL TABLE IF NOT EXISTS community_fts USING fts5(
    name, ingredients, extra, tokenize = 'porter unicode61 remove_diacritics 2'
);

-- Small key/value notes the app keeps about itself.
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---- Indexes, chosen for how pages read the data ----
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id, table_id);
CREATE INDEX IF NOT EXISTS idx_invites_table ON invites(table_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets(user_id);
CREATE INDEX IF NOT EXISTS idx_temp_photos_user ON temp_photos(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_recipes_table ON recipes(table_id, name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_recipes_table_made ON recipes(table_id, last_made);
CREATE INDEX IF NOT EXISTS idx_recipes_photo ON recipes(photo_filename) WHERE photo_filename IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_recipes_listing ON recipes(community_listing_id) WHERE community_listing_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ingredients_recipe ON ingredients(recipe_id, position);
CREATE INDEX IF NOT EXISTS idx_steps_recipe ON steps(recipe_id, position);
CREATE INDEX IF NOT EXISTS idx_notes_recipe ON notes(recipe_id, created_at);
CREATE INDEX IF NOT EXISTS idx_planned_recipe ON planned_meals(recipe_id);
CREATE INDEX IF NOT EXISTS idx_planned_table_week_day ON planned_meals(table_id, week_start, day);
CREATE INDEX IF NOT EXISTS idx_packaged_photo ON packaged_meals(label_photo) WHERE label_photo IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_meal_messages_meal ON meal_messages(meal_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_meal_messages_request ON meal_messages(meal_id, request_key)
    WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_proposals_meal ON recipe_edit_proposals(meal_id);
CREATE INDEX IF NOT EXISTS idx_item_sources_item ON shopping_item_sources(item_id);
CREATE INDEX IF NOT EXISTS idx_item_sources_recipe ON shopping_item_sources(recipe_id);
CREATE INDEX IF NOT EXISTS idx_item_sources_meal ON shopping_item_sources(meal_id);
CREATE INDEX IF NOT EXISTS idx_drafts_owner ON recipe_drafts(user_id, table_id, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_messages_request ON draft_messages(draft_id, request_key)
    WHERE request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_draft_messages_draft ON draft_messages(draft_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_source ON community_listings(source_recipe_id)
    WHERE source_recipe_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_listings_status ON community_listings(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_versions_photo ON community_versions(photo_filename) WHERE photo_filename IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_saves_table ON community_saves(table_id);
CREATE INDEX IF NOT EXISTS idx_reports_listing ON community_reports(listing_id);
"""

# Starting Kitchen Staples for a new Table. Anything the family deletes stays deleted.
DEFAULT_STAPLES = [
    "Salt", "Black pepper", "Garlic powder", "Onion powder", "Paprika",
    "Chili powder", "Olive oil", "Butter", "Milk",
]


def statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (so they can run inside one transaction).

    executescript() would commit first, which a migration must never do halfway through.
    """
    out, buffer = [], ""
    for line in script.splitlines(keepends=True):
        if line.strip().startswith("--") and not buffer.strip():
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                out.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        out.append(buffer.strip())
    return out


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with foreign keys on and dict-like rows."""
    # check_same_thread=False: FastAPI may hand a request between threads.
    # timeout: wait up to 5 s for another writer instead of failing at once.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(db_path: Path, legacy_login: tuple[str, str] | None = None, display_name: str = "",
            table_name: str = "", backup_dir: Path | None = None) -> dict | None:
    """Create data folders and tables, upgrading an older database first.

    `legacy_login` is the shared (username, password hash) from .env: on the first start
    after upgrading, it becomes the first account and joins the family's Table.
    Returns a migration report when an upgrade ran, otherwise None.
    """
    from app import migrations   # imported here: migrations imports this module

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        # WAL mode lets reads continue while a write is in progress.
        conn.execute("PRAGMA journal_mode = WAL")
        report = migrations.upgrade(conn, db_path, legacy_login, display_name, table_name,
                                    backup_dir or db_path.parent / "backups")
        return report
    finally:
        conn.close()


class DataInUse(RuntimeError):
    """Another Family Table server is already using this data folder."""


@contextmanager
def data_folder_lock(data_dir: Path):
    """Hold a lock on the data folder while the server runs.

    Stops a second server (for example the Docker container and scripts/serve.sh, or a
    copy on another port) from writing to the same family database at the same time.
    The lock disappears automatically when the process stops, even after a crash.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / ".family-table.lock", "a")  # noqa: SIM115 - held for the server's lifetime
    try:
        try:
            # POSIX record lock: works across processes and containers sharing the folder.
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DataInUse(
                f"Family Table is already running with the data folder {data_dir}. "
                "Stop the other server first (Ctrl+C in its terminal, or: docker compose down)."
            ) from None
        yield
    finally:
        handle.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Take SQLite's write lock up front, so check-then-write steps from two phones can't interleave.

    Keep these short: never wait for a model, an upload, or a website inside one.
    """
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
    request.state.db = conn   # the page header reads the person's Tables with the same connection
    try:
        yield conn
    finally:
        request.state.db = None
        conn.close()
