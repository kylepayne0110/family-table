"""Account tools for the person who runs this Family Table.

Family Table doesn't send email, so password recovery goes through whoever runs the server:
they make a one-time reset link here and send it to the person (text message, in person).
The link works once, expires after an hour, and signs the account out everywhere when used.

    python -m app.accounts reset-link USERNAME [--base https://your-address]
    python -m app.accounts list
"""

# argparse reads the command line.
import argparse

# sqlite3 types the connection.
import sqlite3

# sys sets the exit status.
import sys

# timedelta sets the link lifetime.
from datetime import timedelta

# Project modules.
from app import auth
from app.config import load_settings
from app.database import connect, init_db, write_transaction


RESET_LIFETIME = timedelta(hours=1)


def create_reset_link(conn: sqlite3.Connection, username: str) -> str | None:
    """A new one-time reset token for an account (older unused links for it stop working)."""
    user = conn.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if user is None:
        return None
    token, token_hash = auth.new_token()
    with write_transaction(conn):
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user["id"],))
        conn.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (token_hash, user["id"], auth.utc_in(RESET_LIFETIME)))
    return token


def reset_token_user(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token or len(token) > 100:
        return None
    return conn.execute(
        """SELECT u.id, u.username, u.display_name FROM password_resets r JOIN users u ON u.id = r.user_id
           WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > datetime('now')""",
        (auth.hash_token(token),)).fetchone()


def use_reset_token(conn: sqlite3.Connection, token: str, password_hash: str) -> bool:
    """Set the new password and sign the account out everywhere. False if the link is no longer valid."""
    with write_transaction(conn):
        row = conn.execute(
            """SELECT user_id FROM password_resets WHERE token_hash = ? AND used_at IS NULL
               AND expires_at > datetime('now')""", (auth.hash_token(token),)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE password_resets SET used_at = datetime('now') WHERE token_hash = ?",
                     (auth.hash_token(token),))
        conn.execute("UPDATE users SET password_hash = ?, password_changed_at = datetime('now') WHERE id = ?",
                     (password_hash, row["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    return True


def change_password(conn: sqlite3.Connection, user_id: int, password_hash: str, keep_token_hash: str) -> None:
    """New password from the Account page: other devices are signed out, this one stays."""
    with write_transaction(conn):
        conn.execute("UPDATE users SET password_hash = ?, password_changed_at = datetime('now') WHERE id = ?",
                     (password_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep_token_hash))
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Family Table account tools")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-link", help="make a one-time password reset link")
    reset.add_argument("username")
    reset.add_argument("--base", default="", help="your Family Table address, e.g. https://name.ts.net")
    sub.add_parser("list", help="list accounts and their Tables")
    args = parser.parse_args(argv)

    settings = load_settings()
    init_db(settings.database_path)
    conn = connect(settings.database_path)
    try:
        if args.command == "list":
            for row in conn.execute(
                """SELECT u.username, u.display_name, group_concat(t.name, ', ') AS tables FROM users u
                   LEFT JOIN memberships m ON m.user_id = u.id LEFT JOIN family_tables t ON t.id = m.table_id
                   GROUP BY u.id ORDER BY u.id"""):
                print(f"{row['username']:<24} {row['display_name']:<20} {row['tables'] or '(no Table)'}")
            return 0
        token = create_reset_link(conn, args.username)
        if token is None:
            print(f"No account named {args.username!r}. See: python -m app.accounts list", file=sys.stderr)
            return 1
        base = args.base.rstrip("/") or "https://<your Family Table address>"
        print(f"One-time reset link for {args.username} (works once, for 1 hour):\n\n  {base}/reset/{token}\n")
        print("Send it to them privately. Using it signs that account out on every device.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
