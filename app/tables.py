# Tables: the shared spaces where a family keeps recipes, dinners, and groceries.
#
# Access rule used everywhere: a person can read or change a Table's data only while they
# are a member of it. The browser may *ask* for a Table (a hidden table_id field or a
# ?table= link), but that request is checked against memberships on every call.

# logging records access problems for the maintainer (ids only, never content).
import logging

# sqlite3 types the connection.
import sqlite3

# dataclass describes the Table a request works in.
from dataclasses import dataclass

# timedelta sets invite lifetimes.
from datetime import timedelta

# Request carries the session and the asked-for Table.
from fastapi import HTTPException, Request

# Project modules.
from app import auth
from app.database import write_transaction
from app.migrations import create_table_in_transaction


log = logging.getLogger("family_table.access")

INVITE_DAYS = 7
MAX_TABLE_NAME = 60
MAX_PENDING_INVITES = 25


class TableError(Exception):
    """A Table action that can't be done (the message is safe to show)."""


class NoTable(Exception):
    """The signed-in person isn't in any Table yet (send them to Welcome)."""


class NotMember(HTTPException):
    """The asked-for Table isn't one of yours (or you were removed from it)."""

    def __init__(self):
        super().__init__(status_code=404, detail="That Table isn't available to you. You may have been removed "
                                                 "from it, or the link is wrong.")


@dataclass
class Scope:
    """The Table a request reads or changes, after the membership check."""

    table_id: int
    name: str
    user_id: int


# ---- Reading ----

def my_tables(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT t.id, t.name, (SELECT COUNT(*) FROM memberships m2 WHERE m2.table_id = t.id) AS member_count
           FROM memberships m JOIN family_tables t ON t.id = m.table_id
           WHERE m.user_id = ? ORDER BY t.name COLLATE NOCASE, t.id""", (user_id,))]


def is_member(conn: sqlite3.Connection, table_id: int | None, user_id: int) -> bool:
    if not table_id:
        return False
    return conn.execute("SELECT 1 FROM memberships WHERE table_id = ? AND user_id = ?",
                        (table_id, user_id)).fetchone() is not None


def get_table(conn: sqlite3.Connection, table_id: int) -> dict | None:
    row = conn.execute("SELECT id, name, created_at FROM family_tables WHERE id = ?", (table_id,)).fetchone()
    return dict(row) if row else None


def members(conn: sqlite3.Connection, table_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT u.id, u.display_name, u.username, m.joined_at, inviter.display_name AS invited_by_name
           FROM memberships m JOIN users u ON u.id = m.user_id
           LEFT JOIN users inviter ON inviter.id = m.invited_by
           WHERE m.table_id = ? ORDER BY m.joined_at, u.id""", (table_id,))]


def _parse_id(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) < 12 else None


def resolve(conn: sqlite3.Connection, session: auth.Session, requested=None, strict: bool = False) -> Scope:
    """The Table to use: the one asked for (if you're a member), else your current one.

    With `strict`, an asked-for Table you can't access is refused instead of falling back,
    so a page drawn for one Table can never send a change to another.
    """
    wanted = _parse_id(requested)
    if wanted is not None:
        if is_member(conn, wanted, session.user_id):
            return _scope(conn, wanted, session.user_id)
        log.info("access denied: user %s asked for table %s", session.user_id, wanted)
        if strict:
            raise NotMember()
    for candidate in (session.active_table_id, _last_table(conn, session.user_id)):
        if candidate and is_member(conn, candidate, session.user_id):
            return _scope(conn, candidate, session.user_id)
    tables = my_tables(conn, session.user_id)
    if not tables:
        raise NoTable()
    return _scope(conn, tables[0]["id"], session.user_id)


def _last_table(conn: sqlite3.Connection, user_id: int) -> int | None:
    row = conn.execute("SELECT last_table_id FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["last_table_id"] if row else None


def _scope(conn: sqlite3.Connection, table_id: int, user_id: int) -> Scope:
    row = conn.execute("SELECT name FROM family_tables WHERE id = ?", (table_id,)).fetchone()
    return Scope(table_id, row["name"], user_id)


def scope_for_get(request: Request, conn: sqlite3.Connection, session: auth.Session) -> Scope:
    """The Table for a page or poll: ?table= if given (checked), else the current one.

    A page that asks for a specific Table by link becomes the current Table, so the
    header always names the Table being shown.
    """
    requested = request.query_params.get("table")
    scope = resolve(conn, session, requested, strict=requested is not None)
    if scope.table_id != session.active_table_id:
        remember(conn, session, scope.table_id)
    request.state.scope = scope
    return scope


def scope_for_post(request: Request, conn: sqlite3.Connection, session: auth.Session, form) -> Scope:
    """The Table for a change. A page always sends the table_id it was drawn for, and that one is used."""
    requested = form.get("table_id") if form is not None else None
    if requested is None:
        requested = request.query_params.get("table")
    scope = resolve(conn, session, requested, strict=requested is not None)
    request.state.scope = scope
    return scope


def remember(conn: sqlite3.Connection, session: auth.Session, table_id: int) -> None:
    """Make a Table current for this device and remember it for next sign-in."""
    try:
        with conn:
            conn.execute("UPDATE sessions SET active_table_id = ? WHERE token_hash = ?", (table_id, session.token_hash))
            conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (table_id, session.user_id))
    except sqlite3.OperationalError:
        return   # busy: the choice is remembered next time
    session.active_table_id = table_id


# ---- Changing Tables ----

def clean_name(name: str) -> str:
    return " ".join(str(name or "").split())[:MAX_TABLE_NAME]


def create_table(conn: sqlite3.Connection, user_id: int, name: str) -> int:
    name = clean_name(name)
    if not name:
        raise TableError("Give your Table a name.")
    with write_transaction(conn):
        count = conn.execute("SELECT COUNT(*) FROM memberships WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= 20:
            raise TableError("You're already in 20 Tables, which is the most one account can join.")
        table_id = create_table_in_transaction(conn, name, user_id)
        conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (table_id, user_id))
    return table_id


def rename_table(conn: sqlite3.Connection, scope: Scope, name: str) -> None:
    name = clean_name(name)
    if not name:
        raise TableError("A Table needs a name.")
    with write_transaction(conn):
        _check_member(conn, scope)
        conn.execute("UPDATE family_tables SET name = ?, updated_at = datetime('now') WHERE id = ?",
                     (name, scope.table_id))


def _check_member(conn: sqlite3.Connection, scope: Scope) -> None:
    """Checked again inside the write transaction: membership may have changed since the page loaded."""
    if not is_member(conn, scope.table_id, scope.user_id):
        raise NotMember()


def remove_member(conn: sqlite3.Connection, scope: Scope, member_id: int) -> str:
    """Remove someone from a Table. Their account, other Tables, and the recipes they added stay.

    Returns the removed person's name. You can't remove yourself here (that's Leave), and a
    Table always keeps at least one member, so nothing is ever stranded.
    """
    with write_transaction(conn):
        _check_member(conn, scope)
        if member_id == scope.user_id:
            raise TableError("To take yourself off this Table, use Leave this Table.")
        row = conn.execute("""SELECT u.display_name FROM memberships m JOIN users u ON u.id = m.user_id
                              WHERE m.table_id = ? AND m.user_id = ?""", (scope.table_id, member_id)).fetchone()
        if row is None:
            raise TableError("That person isn't a member anymore.")
        conn.execute("DELETE FROM memberships WHERE table_id = ? AND user_id = ?", (scope.table_id, member_id))
        _end_access(conn, scope.table_id, member_id)
    log.info("member removed: table %s user %s by %s", scope.table_id, member_id, scope.user_id)
    return row["display_name"]


def leave_table(conn: sqlite3.Connection, scope: Scope) -> None:
    """Leave a Table. The last member can't leave, so a routine tap never strands the family's data."""
    with write_transaction(conn):
        _check_member(conn, scope)
        count = conn.execute("SELECT COUNT(*) FROM memberships WHERE table_id = ?", (scope.table_id,)).fetchone()[0]
        if count <= 1:
            raise TableError("You're the only member, so leaving would leave this Table's recipes, plans, and lists "
                             "with nobody to look after them. Invite someone first, then you can leave.")
        conn.execute("DELETE FROM memberships WHERE table_id = ? AND user_id = ?", (scope.table_id, scope.user_id))
        _end_access(conn, scope.table_id, scope.user_id)


def _end_access(conn: sqlite3.Connection, table_id: int, user_id: int) -> None:
    """Clear the person's place in this Table: current-Table pointers, drafts, and unsaved photos."""
    conn.execute("UPDATE sessions SET active_table_id = NULL WHERE user_id = ? AND active_table_id = ?",
                 (user_id, table_id))
    conn.execute("UPDATE users SET last_table_id = NULL WHERE id = ? AND last_table_id = ?", (user_id, table_id))
    conn.execute("DELETE FROM recipe_drafts WHERE user_id = ? AND table_id = ?", (user_id, table_id))
    conn.execute("DELETE FROM temp_photos WHERE user_id = ? AND table_id = ?", (user_id, table_id))


# ---- Invites ----

def create_invite(conn: sqlite3.Connection, scope: Scope, inviter_name: str) -> str:
    """A new invite link token (valid for INVITE_DAYS days, one person)."""
    token, token_hash = auth.new_token()
    with write_transaction(conn):
        _check_member(conn, scope)
        pending = conn.execute(
            """SELECT COUNT(*) FROM invites WHERE table_id = ? AND revoked_at IS NULL AND accepted_at IS NULL
               AND expires_at > datetime('now')""", (scope.table_id,)).fetchone()[0]
        if pending >= MAX_PENDING_INVITES:
            raise TableError("There are already 25 unused invite links. Cancel a few before making more.")
        conn.execute("""INSERT INTO invites (table_id, token_hash, created_by, inviter_name, expires_at)
                        VALUES (?, ?, ?, ?, ?)""",
                     (scope.table_id, token_hash, scope.user_id, inviter_name[:60],
                      auth.utc_in(timedelta(days=INVITE_DAYS))))
    return token


def pending_invites(conn: sqlite3.Connection, table_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT id, inviter_name, created_at, expires_at FROM invites
           WHERE table_id = ? AND revoked_at IS NULL AND accepted_at IS NULL AND expires_at > datetime('now')
           ORDER BY created_at DESC""", (table_id,))]


def revoke_invite(conn: sqlite3.Connection, scope: Scope, invite_id: int) -> None:
    with write_transaction(conn):
        _check_member(conn, scope)
        conn.execute("UPDATE invites SET revoked_at = datetime('now') WHERE id = ? AND table_id = ? "
                     "AND revoked_at IS NULL AND accepted_at IS NULL", (invite_id, scope.table_id))


@dataclass
class InviteView:
    """What an invite link shows. `problem` explains a link that can't be used."""

    status: str              # ok, member, used, expired, revoked, missing
    table_id: int | None = None
    table_name: str = ""
    inviter_name: str = ""

    @property
    def message(self) -> str:
        return {
            "ok": f"{self.inviter_name} invited you to {self.table_name}.",
            "joined": f"Welcome to {self.table_name}!",
            "member": f"You're already a member of {self.table_name}.",
            "used": "This invite link has already been used. Ask for a new one: each link is for one person.",
            "expired": "This invite link has expired. Ask whoever sent it for a new one.",
            "revoked": "This invite link was cancelled. Ask for a new one if you still want to join.",
            "missing": "We couldn't find that invite. Check the link, or ask for a new one.",
        }[self.status]


def view_invite(conn: sqlite3.Connection, token: str, user_id: int | None = None) -> InviteView:
    if not token or len(token) > 100:
        return InviteView("missing")
    row = conn.execute(
        """SELECT i.*, t.name AS table_name, (i.expires_at <= datetime('now')) AS is_expired
           FROM invites i JOIN family_tables t ON t.id = i.table_id WHERE i.token_hash = ?""",
        (auth.hash_token(token),)).fetchone()
    if row is None:
        return InviteView("missing")
    view = InviteView("ok", row["table_id"], row["table_name"], row["inviter_name"])
    if user_id and is_member(conn, row["table_id"], user_id):
        view.status = "member"
    elif row["accepted_at"] is not None:
        view.status = "used"
    elif row["revoked_at"] is not None:
        view.status = "revoked"
    elif row["is_expired"]:
        view.status = "expired"
    return view


def accept_invite(conn: sqlite3.Connection, token: str, user_id: int) -> InviteView:
    """Join the Table. Accepting twice (a double tap, a retry) is harmless: you're simply already a member."""
    with write_transaction(conn):
        view = view_invite(conn, token, user_id)
        if view.status == "member":
            return view
        if view.status != "ok":
            return view
        row = conn.execute("SELECT id, created_by FROM invites WHERE token_hash = ?", (auth.hash_token(token),)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM memberships WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= 20:
            raise TableError("You're already in 20 Tables, which is the most one account can join.")
        conn.execute("INSERT OR IGNORE INTO memberships (table_id, user_id, invited_by) VALUES (?, ?, ?)",
                     (view.table_id, user_id, row["created_by"]))
        conn.execute("UPDATE invites SET accepted_by = ?, accepted_at = datetime('now') WHERE id = ?",
                     (user_id, row["id"]))
        conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (view.table_id, user_id))
    view.status = "joined"
    return view
