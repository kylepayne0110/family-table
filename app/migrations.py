# Database upgrades. Each runs once, inside one transaction, after a backup copy is made.
#
# Before this release a database held one family's data with one shared login from .env.
# Upgrading to accounts and Tables keeps every row and id: the family's recipes, photos,
# plans, history, packaged meals, servings, groceries, staples, and conversations all move
# into one Table, and the shared login becomes the first account and that Table's member.

# logging records what an upgrade did (never passwords or family content).
import logging

# sqlite3 types the connection and makes the backup copy.
import sqlite3

# datetime names the backup file.
from datetime import UTC, datetime

# Path locates the backup folder.
from pathlib import Path

# Project modules.
from app.database import DEFAULT_STAPLES, SCHEMA, SCHEMA_VERSION, statements


log = logging.getLogger("family_table.migrations")


class MigrationError(RuntimeError):
    """An upgrade stopped before changing anything (the original database is untouched)."""


# ---- The layout before accounts (kept so very old databases can be brought up to date) ----

LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, cuisine TEXT NOT NULL DEFAULT '',
    prep_minutes INTEGER, cook_minutes INTEGER, servings INTEGER, photo_filename TEXT,
    card_tag TEXT NOT NULL DEFAULT '', last_made TEXT, source_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS ingredients (id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE, position INTEGER NOT NULL,
    amount TEXT NOT NULL DEFAULT '', name TEXT NOT NULL CHECK (length(name) > 0));
CREATE TABLE IF NOT EXISTS steps (id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE, position INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL CHECK (length(body) > 0));
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE, author TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS week_plans (week_start TEXT PRIMARY KEY, plan_version INTEGER NOT NULL DEFAULT 0,
    list_version INTEGER NOT NULL DEFAULT 0, list_signature TEXT, list_built_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS planned_meals (id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL REFERENCES week_plans(week_start) ON DELETE CASCADE,
    day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6), recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
    servings INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS packaged_meals (id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL UNIQUE REFERENCES planned_meals(id) ON DELETE CASCADE,
    barcode TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, brand TEXT NOT NULL DEFAULT '',
    package_size TEXT NOT NULL DEFAULT '', product_source TEXT NOT NULL DEFAULT 'manual',
    oven_directions TEXT NOT NULL DEFAULT '', oven_source TEXT NOT NULL DEFAULT '',
    microwave_directions TEXT NOT NULL DEFAULT '', microwave_source TEXT NOT NULL DEFAULT '',
    other_directions TEXT NOT NULL DEFAULT '', other_source TEXT NOT NULL DEFAULT '', label_photo TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS shopping_items (id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL REFERENCES week_plans(week_start) ON DELETE CASCADE, item_key TEXT NOT NULL,
    name TEXT NOT NULL, quantity TEXT NOT NULL DEFAULT '', totals_json TEXT NOT NULL DEFAULT '{}',
    manual INTEGER NOT NULL DEFAULT 0, manual_qty TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'need',
    stale INTEGER NOT NULL DEFAULT 0, change_note TEXT NOT NULL DEFAULT '', rev INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (week_start, item_key));
CREATE TABLE IF NOT EXISTS shopping_item_sources (
    item_id INTEGER NOT NULL REFERENCES shopping_items(id) ON DELETE CASCADE,
    recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL, recipe_name TEXT NOT NULL,
    amount TEXT NOT NULL DEFAULT '', times INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS meal_list_requests (request_key TEXT PRIMARY KEY,
    meal_id INTEGER NOT NULL REFERENCES planned_meals(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS meal_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL REFERENCES planned_meals(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')), body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text', data_json TEXT NOT NULL DEFAULT '{}', request_key TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS recipe_edit_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL REFERENCES planned_meals(id) ON DELETE CASCADE,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES meal_messages(id) ON DELETE SET NULL, changes_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT (datetime('now')), decided_at TEXT);
CREATE TABLE IF NOT EXISTS staples (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Columns added during the single-family era: (table, column, definition).
LEGACY_ADDED_COLUMNS = [
    ("recipes", "source_url", "TEXT NOT NULL DEFAULT ''"),
    ("shopping_items", "cleared", "INTEGER NOT NULL DEFAULT 0"),
    ("shopping_items", "bought_totals", "TEXT NOT NULL DEFAULT ''"),
    ("shopping_item_sources", "meal_id", "INTEGER REFERENCES planned_meals(id) ON DELETE SET NULL"),
    ("shopping_item_sources", "ingredient", "TEXT NOT NULL DEFAULT ''"),
    ("shopping_item_sources", "servings", "INTEGER"),
]

# Rows that must survive the move into a Table, counted before and after.
FAMILY_TABLES = [
    "recipes", "ingredients", "steps", "notes", "week_plans", "planned_meals", "packaged_meals",
    "shopping_items", "shopping_item_sources", "meal_list_requests", "meal_messages",
    "recipe_edit_proposals", "staples",
]


def _columns(conn: sqlite3.Connection, table: str) -> dict:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _run(conn: sqlite3.Connection, script: str) -> None:
    for statement in statements(script):
        conn.execute(statement)


def backup(conn: sqlite3.Connection, db_path: Path, backup_dir: Path, label: str) -> Path:
    """Copy the whole database (consistently, even mid-WAL) before changing it."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db_path.stem}-before-{label}-{stamp}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


# ---- Entry point ----

def upgrade(conn: sqlite3.Connection, db_path: Path, legacy_login: tuple[str, str] | None,
            display_name: str, table_name: str, backup_dir: Path) -> dict | None:
    """Bring any database to the current layout. Safe to run on every start."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise MigrationError(
            f"This database was made by a newer Family Table (layout {version}); this copy understands "
            f"up to {SCHEMA_VERSION}. Update the code, or restore a backup from {backup_dir}.")
    report = None
    if version < 2:
        legacy_tables = {"recipes", "week_plans", "planned_meals", "shopping_items", "staples", "sessions", "notes"}
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if existing & legacy_tables:
            report = migrate_single_family(conn, db_path, legacy_login, display_name, table_name, backup_dir)
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _run(conn, SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    else:
        # Same layout: create anything missing (indexes added in a later point release, say).
        conn.execute("BEGIN IMMEDIATE")
        try:
            _run(conn, SCHEMA)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    if legacy_login:
        adopt_legacy_login(conn, legacy_login, display_name, table_name)
    return report


# ---- Single-family database -> accounts and Tables ----

def _bring_legacy_up_to_date(conn: sqlite3.Connection) -> None:
    """Older single-family databases: add any tables and columns they're missing (inside the transaction)."""
    columns = _columns(conn, "planned_meals")
    if columns and "id" not in columns:
        # The very first planner keyed dinners by (week, day) with no id.
        conn.execute("""CREATE TABLE planned_meals_upgrade (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        week_start TEXT NOT NULL, day INTEGER NOT NULL, recipe_id INTEGER, servings INTEGER,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        conn.execute("""INSERT INTO planned_meals_upgrade (week_start, day, recipe_id)
                        SELECT week_start, day, recipe_id FROM planned_meals ORDER BY week_start, day""")
        conn.execute("DROP TABLE planned_meals")
        conn.execute("ALTER TABLE planned_meals_upgrade RENAME TO planned_meals")
    _run(conn, LEGACY_SCHEMA)
    for table, column, definition in LEGACY_ADDED_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    if "servings" not in _columns(conn, "planned_meals"):
        conn.execute("ALTER TABLE planned_meals ADD COLUMN servings INTEGER")
    if "created_at" not in _columns(conn, "planned_meals"):
        conn.execute("ALTER TABLE planned_meals ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in FAMILY_TABLES}


def default_table_name(username: str | None) -> str:
    """'payne' -> "Payne's Table"; without a login, "Our Table"."""
    name = (username or "").strip()
    if not name:
        return "Our Table"
    name = name[:1].upper() + name[1:]
    return f"{name}'" + ("" if name.endswith("s") else "s") + " Table"


def migrate_single_family(conn: sqlite3.Connection, db_path: Path, legacy_login: tuple[str, str] | None,
                          display_name: str, table_name: str, backup_dir: Path) -> dict:
    """Move a single-family database into one Table. All or nothing, after a backup."""
    backup_path = backup(conn, db_path, backup_dir, "accounts")
    log.info("Backed up the database before upgrading to accounts: %s", backup_path)

    conn.execute("PRAGMA foreign_keys = OFF")   # tables are rebuilt; references are checked at the end
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _bring_legacy_up_to_date(conn)
            before = _counts(conn)
            name = (table_name or default_table_name(legacy_login[0] if legacy_login else "")).strip()[:60]

            # Old tables that are rebuilt with an owner are renamed out of the way first.
            for table in ("recipes", "week_plans", "planned_meals", "shopping_items", "staples", "sessions", "notes"):
                conn.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
            # Renaming rewrote references in other tables to point at legacy_*: SQLite does that
            # unless legacy_alter_table is on. Recreate references by rebuilding those tables too
            # is unnecessary because the new tables take the original names below and every
            # child table is rebuilt from SCHEMA wording (see _rebuild_children).
            _run(conn, SCHEMA)

            table_id = conn.execute("INSERT INTO family_tables (name) VALUES (?)", (name,)).lastrowid
            owner_id = None
            if legacy_login:
                owner_id = _create_legacy_user(conn, legacy_login, display_name)
                conn.execute("INSERT INTO memberships (table_id, user_id) VALUES (?, ?)", (table_id, owner_id))
                conn.execute("UPDATE family_tables SET created_by = ? WHERE id = ?", (owner_id, table_id))
                conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (table_id, owner_id))

            conn.execute(
                """INSERT INTO recipes (id, table_id, name, description, category, cuisine, prep_minutes, cook_minutes,
                       servings, photo_filename, card_tag, last_made, source_url, added_by, created_at, updated_at)
                   SELECT id, ?, name, description, category, cuisine, prep_minutes, cook_minutes, servings,
                       photo_filename, card_tag, last_made, source_url, ?, created_at, updated_at
                   FROM legacy_recipes""", (table_id, owner_id))
            conn.execute("""INSERT INTO notes (id, recipe_id, author, body, created_at)
                            SELECT id, recipe_id, author, body, created_at FROM legacy_notes""")
            conn.execute(
                """INSERT INTO week_plans (table_id, week_start, plan_version, list_version, list_signature,
                       list_built_at, updated_at)
                   SELECT ?, week_start, plan_version, list_version, list_signature, list_built_at, updated_at
                   FROM legacy_week_plans""", (table_id,))
            conn.execute(
                """INSERT INTO planned_meals (id, table_id, week_start, day, recipe_id, servings, created_at)
                   SELECT id, ?, week_start, day, recipe_id, servings,
                          CASE WHEN created_at = '' THEN datetime('now') ELSE created_at END
                   FROM legacy_planned_meals""", (table_id,))
            conn.execute(
                """INSERT INTO shopping_items (id, table_id, week_start, item_key, name, quantity, totals_json, manual,
                       manual_qty, status, stale, change_note, rev, created_at, updated_at, cleared, bought_totals)
                   SELECT id, ?, week_start, item_key, name, quantity, totals_json, manual, manual_qty, status, stale,
                       change_note, rev, created_at, updated_at, cleared, bought_totals
                   FROM legacy_shopping_items""", (table_id,))
            conn.execute("""INSERT INTO staples (id, table_id, name, name_key, created_at)
                            SELECT id, ?, name, name_key, created_at FROM legacy_staples""", (table_id,))
            # Sessions from the shared login are signed out: everyone signs in once with an account.
            for table in ("recipes", "week_plans", "planned_meals", "shopping_items", "staples", "sessions", "notes"):
                conn.execute(f"DROP TABLE legacy_{table}")
            _rebuild_children(conn)
            _run(conn, SCHEMA)   # indexes on the rebuilt tables

            after = _counts(conn)
            if after != before:
                lost = {t: (before[t], after[t]) for t in FAMILY_TABLES if before[t] != after[t]}
                raise MigrationError(f"Row counts changed during the upgrade: {lost}")
            # A database from before Kitchen Staples existed gets the starting staples, as it would have.
            if conn.execute("SELECT 1 FROM app_meta WHERE key = 'staples_seeded'").fetchone() is None:
                from app.ingredients import name_key
                conn.executemany("INSERT OR IGNORE INTO staples (table_id, name, name_key) VALUES (?, ?, ?)",
                                 [(table_id, n, name_key(n)) for n in DEFAULT_STAPLES])
                conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('staples_seeded', datetime('now'))")
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise MigrationError(f"The upgrade found {len(problems)} broken references; nothing was changed.")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    report = {"backup": str(backup_path), "table_id": table_id, "table_name": name, "owner_id": owner_id,
              "counts": after}
    log.info("Upgraded to accounts and Tables: %s now holds %s",
             name, ", ".join(f"{v} {k}" for k, v in after.items() if v))
    return report


def _rebuild_children(conn: sqlite3.Connection) -> None:
    """Point child tables' foreign keys back at the rebuilt parents.

    Renaming a parent (recipes -> legacy_recipes) makes SQLite rewrite the child tables'
    REFERENCES clauses to the new name. Each child is rebuilt with SCHEMA's wording so its
    references name the real tables again. Rows and ids are copied exactly.
    """
    children = ["ingredients", "steps", "packaged_meals", "shopping_item_sources", "meal_list_requests",
                "meal_messages", "recipe_edit_proposals"]
    schema_sql = {}
    for statement in statements(SCHEMA):
        head = statement.split("(", 1)[0].split()
        if len(head) >= 6 and head[0] == "CREATE" and head[1] == "TABLE":
            schema_sql[head[-1]] = statement
    for table in children:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        if "legacy_" not in sql:
            continue
        cols = ", ".join(_columns(conn, table))
        conn.execute(f"ALTER TABLE {table} RENAME TO rebuild_{table}")
        conn.execute(schema_sql[table].replace("IF NOT EXISTS ", ""))
        conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM rebuild_{table}")
        conn.execute(f"DROP TABLE rebuild_{table}")


def _create_legacy_user(conn: sqlite3.Connection, legacy_login: tuple[str, str], display_name: str) -> int:
    username, password_hash = legacy_login
    username = username.strip()[:40]
    if len(username) < 3:
        username = (username + "-family")[:40]
    shown = (display_name or username[:1].upper() + username[1:]).strip()[:60]
    return conn.execute("INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                        (username, shown, password_hash)).lastrowid


def adopt_legacy_login(conn: sqlite3.Connection, legacy_login: tuple[str, str], display_name: str,
                       table_name: str) -> None:
    """The .env login on an upgraded or brand-new database.

    - No accounts yet (a new install configured the old way): create the account and its Table.
    - A Table with no members (upgraded before a login existed): the account joins it.
    Otherwise nothing changes: the account already exists and .env is no longer consulted.
    """
    username = legacy_login[0].strip()
    if not username:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        any_user = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        orphan = conn.execute("""SELECT id FROM family_tables t WHERE NOT EXISTS
                                 (SELECT 1 FROM memberships m WHERE m.table_id = t.id) ORDER BY id LIMIT 1""").fetchone()
        if user is None and (any_user is None or orphan is not None):
            user_id = _create_legacy_user(conn, legacy_login, display_name)
            if orphan is not None:
                table_id = orphan["id"]
            else:
                table_id = create_table_in_transaction(conn, table_name or default_table_name(username), user_id)
            conn.execute("INSERT OR IGNORE INTO memberships (table_id, user_id) VALUES (?, ?)", (table_id, user_id))
            conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (table_id, user_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def create_table_in_transaction(conn: sqlite3.Connection, name: str, user_id: int) -> int:
    """A new Table with its first member and the starting Kitchen Staples (caller holds the transaction)."""
    from app.ingredients import name_key   # imported here to keep this module light
    table_id = conn.execute("INSERT INTO family_tables (name, created_by) VALUES (?, ?)", (name, user_id)).lastrowid
    conn.execute("INSERT INTO memberships (table_id, user_id) VALUES (?, ?)", (table_id, user_id))
    conn.executemany("INSERT OR IGNORE INTO staples (table_id, name, name_key) VALUES (?, ?, ?)",
                     [(table_id, s, name_key(s)) for s in DEFAULT_STAPLES])
    return table_id
