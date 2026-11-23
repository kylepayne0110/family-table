"""Create an account or set a password, from the server itself.

Run from the project folder:  python -m app.setup_login

- An existing username: choose a new password (that account is signed out everywhere).
- A new username: creates the account. If an upgraded family Table has no members yet,
  the account joins it; otherwise the account gets its own new Table.

Most people should simply use "Create an account" in the app; this is for the person who
runs Family Table (first setup, or a forgotten password without a reset link).
"""

# getpass reads a password without echoing it to the screen.
from getpass import getpass

# Project helpers.
from app import auth
from app.config import load_settings
from app.database import connect, init_db, write_transaction
from app.migrations import create_table_in_transaction, default_table_name


def _ask_password(username: str, display_name: str) -> str:
    while True:
        password = getpass(f"Password (at least {auth.MIN_PASSWORD} characters): ")
        problem = auth.password_problem(password, username, display_name)
        if problem:
            print(problem)
            continue
        if getpass("Type it again: ") != password:
            print("Those didn't match. Try again.")
            continue
        return password


def main() -> None:
    settings = load_settings()
    legacy = (settings.username, settings.password_hash) if settings.login_configured else None
    init_db(settings.database_path, legacy, settings.display_name, settings.table_name)
    conn = connect(settings.database_path)
    try:
        print("Family Table: create an account or set a password.\n")
        username = ""
        while auth.username_problem(username):
            username = input("Username: ").strip()
            if auth.username_problem(username):
                print(auth.username_problem(username))
        row = conn.execute("SELECT id, display_name FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            password_hash = auth.hash_password(_ask_password(username, row["display_name"]))
            with write_transaction(conn):
                conn.execute("UPDATE users SET password_hash = ?, password_changed_at = datetime('now') WHERE id = ?",
                             (password_hash, row["id"]))
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            print(f"\nNew password saved for {username}. That account was signed out on every device.")
            return
        display_name = ""
        while not display_name:
            display_name = input("Your name (what your family sees): ").strip()[:60]
        password_hash = auth.hash_password(_ask_password(username, display_name))
        with write_transaction(conn):
            user_id = conn.execute("INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                                   (username, display_name, password_hash)).lastrowid
            orphan = conn.execute("""SELECT id, name FROM family_tables t WHERE NOT EXISTS
                                     (SELECT 1 FROM memberships m WHERE m.table_id = t.id) ORDER BY id LIMIT 1""").fetchone()
            if orphan:
                conn.execute("INSERT INTO memberships (table_id, user_id) VALUES (?, ?)", (orphan["id"], user_id))
                table_id, table_name = orphan["id"], orphan["name"]
            else:
                table_name = default_table_name(display_name)
                table_id = create_table_in_transaction(conn, table_name, user_id)
            conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (table_id, user_id))
        print(f"\nCreated {username}, a member of {table_name}. Sign in at your Family Table address.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
