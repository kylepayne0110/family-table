# Weekly shopping lists and the family's Kitchen Staples.

# json stores machine-readable quantity totals.
import json

# sqlite3 types the database connection.
import sqlite3

# date identifies a week by its Monday.
from datetime import date

# write_transaction locks the database for each change.
from app.database import write_transaction

# Ingredient combining and name matching.
from app.ingredients import combine, matches_staple, name_key, needs_more

# Plan helpers: which meals are planned, and their fingerprint.
from app.planner import ensure_week, plan_signature, planned_ingredient_lines


STATUSES = ("need", "bought", "have", "staple")
MAX_NAME = 120
MAX_QTY = 40
MAX_STAPLE = 80


class ListError(Exception):
    """A change that can't be made; the message is safe to show."""


class Conflict(ListError):
    """The item changed on another device since this page loaded."""


class Missing(ListError):
    """The item no longer exists (e.g. removed on another device)."""


# ---- Reading ----

def list_state(conn: sqlite3.Connection, week: date) -> dict:
    """Everything the shopping page shows for a week."""
    w = week.isoformat()
    meta = conn.execute("SELECT * FROM week_plans WHERE week_start = ?", (w,)).fetchone()
    items = [dict(row) for row in conn.execute(
        "SELECT * FROM shopping_items WHERE week_start = ? ORDER BY name COLLATE NOCASE, id", (w,)
    )]

    # One query for every item's sources, grouped in Python.
    sources: dict[int, list] = {}
    for row in conn.execute(
        """SELECT s.item_id, s.recipe_id, s.recipe_name, s.amount, s.times
           FROM shopping_item_sources s JOIN shopping_items i ON i.id = s.item_id
           WHERE i.week_start = ? ORDER BY s.rowid""",
        (w,),
    ):
        sources.setdefault(row["item_id"], []).append(dict(row))

    sections: dict[str, list] = {status: [] for status in STATUSES}
    for item in items:
        if item["cleared"]:
            continue  # cleared purchases are remembered but never shown
        item["sources"] = sources.get(item["id"], [])
        item["display_qty"] = " + ".join(q for q in (item["quantity"], item["manual_qty"]) if q)
        sections[item["status"]].append(item)

    has_list = meta is not None and meta["list_signature"] is not None
    # Items flagged by an older version of the app are cleaned up by the next Update.
    legacy_stale = any(i["stale"] for i in items)
    return {
        "sections": sections,
        "need_count": len(sections["need"]),
        "bought_count": len(sections["bought"]),
        "shopping_total": len(sections["need"]) + len(sections["bought"]),
        "total": len(items),   # includes cleared items, so a fully shopped list isn't "empty"
        "has_list": has_list,
        "version": meta["list_version"] if meta else 0,
        "out_of_date": has_list and (legacy_stale or meta["list_signature"] != plan_signature(conn, week)),
    }


def list_status(conn: sqlite3.Connection, week: date) -> str:
    """'none', 'current', or 'outdated' (the plan changed since the list was built)."""
    row = conn.execute("SELECT list_signature FROM week_plans WHERE week_start = ?", (week.isoformat(),)).fetchone()
    if row is None or row["list_signature"] is None:
        return "none"
    return "current" if row["list_signature"] == plan_signature(conn, week) else "outdated"


# ---- Generating and updating ----

def _bump_list(conn: sqlite3.Connection, week: date) -> None:
    conn.execute(
        "UPDATE week_plans SET list_version = list_version + 1, updated_at = datetime('now') WHERE week_start = ?",
        (week.isoformat(),),
    )


def _replace_sources(conn: sqlite3.Connection, item_id: int, sources) -> None:
    conn.execute("DELETE FROM shopping_item_sources WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO shopping_item_sources (item_id, recipe_id, recipe_name, amount, times) VALUES (?, ?, ?, ?, ?)",
        [(item_id, s.recipe_id, s.recipe_name, s.amount, s.times) for s in sources],
    )


def _source_rows(conn: sqlite3.Connection, item_id: int) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT recipe_id, recipe_name, amount, times FROM shopping_item_sources WHERE item_id = ? ORDER BY rowid",
        (item_id,),
    )]


def generate_list(conn: sqlite3.Connection, week: date) -> dict:
    """Build or update a week's list so it matches the meal plan, without losing anyone's progress.

    - New ingredients are added (staples go straight to the Kitchen Staples section).
    - Existing items keep their id and status (including cleared purchases); quantities and
      sources are refreshed. If a purchased item now needs more than was bought, it goes
      back to Need to Buy with a note.
    - Ingredients no planned recipe needs any more are removed.
    - Hand-added items are always kept. Running it twice changes nothing the second time.
    """
    w = week.isoformat()
    # The write lock is taken first, so two phones pressing Update can't interleave.
    with write_transaction(conn):
        ensure_week(conn, week)
        first_time = conn.execute(
            "SELECT list_signature IS NULL FROM week_plans WHERE week_start = ?", (w,)
        ).fetchone()[0]
        desired = combine(planned_ingredient_lines(conn, week))
        staple_keys = {row[0] for row in conn.execute("SELECT name_key FROM staples")}
        existing = {row["item_key"]: dict(row) for row in conn.execute(
            """SELECT i.*, (SELECT COUNT(*) FROM shopping_item_sources s WHERE s.item_id = i.id) AS source_count
               FROM shopping_items i WHERE i.week_start = ?""",
            (w,),
        )}
        summary = {"added": 0, "staples": 0, "changed": 0, "removed": 0, "first": bool(first_time)}

        for key, item in desired.items():
            totals = json.dumps(item.totals, sort_keys=True)
            row = existing.get(key)
            if row is None:
                status = "staple" if matches_staple(key, staple_keys) else "need"
                cur = conn.execute(
                    """INSERT INTO shopping_items (week_start, item_key, name, quantity, totals_json, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (w, key, item.display[:MAX_NAME], item.quantity, totals, status),
                )
                _replace_sources(conn, cur.lastrowid, item.sources)
                summary["staples" if status == "staple" else "added"] += 1
                continue

            new_sources = [(s.recipe_id, s.recipe_name, s.amount, s.times) for s in item.sources]
            status, note, cleared = row["status"], row["change_note"], row["cleared"]
            quantity_changed = row["totals_json"] != totals or row["quantity"] != item.quantity
            if quantity_changed and row["source_count"]:
                if row["quantity"]:
                    note = f"Was {row['quantity']}, now {item.quantity or 'some'}"
                # Compare with what was actually bought, so "less, then back to the same" isn't bought twice.
                bought = json.loads(row["bought_totals"] or row["totals_json"])
                if status == "bought" and needs_more(bought, item.totals):
                    # Don't assume what was bought covers the bigger amount (even if it was cleared).
                    status, cleared = "need", 0
                    note = f"Need more: was {row['quantity'] or 'some'}, now {item.quantity}"
            if quantity_changed or row["stale"] or new_sources != _source_rows(conn, row["id"]):
                conn.execute(
                    """UPDATE shopping_items SET quantity = ?, totals_json = ?, status = ?, change_note = ?,
                           cleared = ?, stale = 0, rev = rev + 1, updated_at = datetime('now') WHERE id = ?""",
                    (item.quantity, totals, status, note, cleared, row["id"]),
                )
                _replace_sources(conn, row["id"], item.sources)
                summary["changed"] += 1

        for key, row in existing.items():
            if key in desired or not row["source_count"]:
                continue  # still needed, or never came from a recipe (hand-added)
            if row["manual"]:
                # Someone also added it by hand, so it stays; it just isn't from a recipe now.
                conn.execute("DELETE FROM shopping_item_sources WHERE item_id = ?", (row["id"],))
                conn.execute(
                    """UPDATE shopping_items SET quantity = '', totals_json = '{}', change_note = '',
                           rev = rev + 1, updated_at = datetime('now') WHERE id = ?""",
                    (row["id"],),
                )
                summary["changed"] += 1
            else:
                # Only a recipe needed it, and no planned recipe does now: it leaves the list,
                # whatever its section (its sources go with it via ON DELETE CASCADE).
                conn.execute("DELETE FROM shopping_items WHERE id = ?", (row["id"],))
                summary["removed"] += 1

        conn.execute(
            "UPDATE week_plans SET list_signature = ?, list_built_at = datetime('now') WHERE week_start = ?",
            (plan_signature(conn, week), w),
        )
        if first_time or any(summary[k] for k in ("added", "staples", "changed", "removed")):
            _bump_list(conn, week)
    return summary


# ---- Changing items ----

def _get_item(conn: sqlite3.Connection, week: date, item_id: int) -> dict:
    row = conn.execute(
        """SELECT i.*, (SELECT COUNT(*) FROM shopping_item_sources s WHERE s.item_id = i.id) AS source_count
           FROM shopping_items i WHERE i.id = ? AND i.week_start = ?""",
        (item_id, week.isoformat()),
    ).fetchone()
    if row is None:
        raise Missing("That item was already removed from the list.")
    return dict(row)


def _check_rev(item: dict, rev: int | None) -> None:
    # A stale page (older rev) must not overwrite a newer change from another phone.
    if rev is not None and item["rev"] != rev:
        raise Conflict("This item was just changed on another device.")


def set_status(conn: sqlite3.Connection, week: date, item_id: int, status: str, rev: int | None) -> int:
    """Move an item between Need to Buy / Purchased / Already Have. Returns its new rev."""
    if status not in ("need", "bought", "have"):
        raise ListError("Unknown list section.")
    with write_transaction(conn):
        item = _get_item(conn, week, item_id)
        if item["status"] == status:
            return item["rev"]  # already done (perhaps by the other phone): nothing to do
        _check_rev(item, rev)
        # Moving an excluded staple onto the list is a deliberate choice, so it counts as
        # added by hand and stays even if the recipe that needed it is later unplanned.
        manual = 1 if item["status"] == "staple" else item["manual"]
        conn.execute(
            """UPDATE shopping_items SET status = ?, manual = ?, change_note = '', rev = rev + 1,
                   updated_at = datetime('now'), bought_totals = CASE WHEN ? = 'bought' THEN totals_json ELSE '' END
               WHERE id = ?""",
            (status, manual, status, item_id),
        )
        _bump_list(conn, week)
    return item["rev"] + 1


def clear_purchased(conn: sqlite3.Connection, week: date) -> int:
    """Clear Purchased: hide every purchased item. Returns how many were cleared.

    The rows stay (marked cleared) so an unchanged Update knows they were already bought
    and doesn't put them back on the list. Adding one again by hand brings it back.
    """
    with write_transaction(conn):
        count = conn.execute(
            """UPDATE shopping_items SET cleared = 1, change_note = '', rev = rev + 1, updated_at = datetime('now')
               WHERE week_start = ? AND status = 'bought' AND cleared = 0""",
            (week.isoformat(),),
        ).rowcount
        if count:
            _bump_list(conn, week)
    return count


def _clean_text(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def add_item(conn: sqlite3.Connection, week: date, name: str, quantity: str = "") -> int:
    """Add a grocery by hand. If it's already on the list (even cleared), bring it back to Need to Buy."""
    name, quantity = _clean_text(name, MAX_NAME), _clean_text(quantity, MAX_QTY)
    key = name_key(name)
    if not key:
        raise ListError("Type what you need to buy.")
    w = week.isoformat()
    with write_transaction(conn):
        ensure_week(conn, week)
        row = conn.execute("SELECT id, manual_qty FROM shopping_items WHERE week_start = ? AND item_key = ?",
                           (w, key)).fetchone()
        if row:
            conn.execute(
                """UPDATE shopping_items SET manual = 1, manual_qty = ?, status = 'need', stale = 0, cleared = 0,
                       rev = rev + 1, updated_at = datetime('now') WHERE id = ?""",
                (quantity or row["manual_qty"], row["id"]),
            )
            item_id = row["id"]
        else:
            item_id = conn.execute(
                """INSERT INTO shopping_items (week_start, item_key, name, manual, manual_qty)
                   VALUES (?, ?, ?, 1, ?)""",
                (w, key, name, quantity),
            ).lastrowid
        _bump_list(conn, week)
    return item_id


def edit_item(conn: sqlite3.Connection, week: date, item_id: int, name: str, quantity: str, rev: int | None) -> int:
    """Rename a hand-added item or change its quantity. Returns the new rev."""
    name, quantity = _clean_text(name, MAX_NAME), _clean_text(quantity, MAX_QTY)
    with write_transaction(conn):
        item = _get_item(conn, week, item_id)
        _check_rev(item, rev)
        if item["source_count"]:
            name = item["name"]  # recipe items keep their name; only the extra quantity changes
        key = name_key(name)
        if not key:
            raise ListError("The item needs a name.")
        clash = conn.execute(
            "SELECT 1 FROM shopping_items WHERE week_start = ? AND item_key = ? AND id != ?",
            (week.isoformat(), key, item_id),
        ).fetchone()
        if clash:
            raise ListError(f"{name} is already on the list.")
        conn.execute(
            """UPDATE shopping_items SET name = ?, item_key = ?, manual_qty = ?, rev = rev + 1,
                   updated_at = datetime('now') WHERE id = ?""",
            (name, key, quantity, item_id),
        )
        _bump_list(conn, week)
    return item["rev"] + 1


def delete_item(conn: sqlite3.Connection, week: date, item_id: int, rev: int | None) -> None:
    """Delete a hand-added item. Recipe items can't be deleted (mark them Already Have instead)."""
    with write_transaction(conn):
        item = _get_item(conn, week, item_id)
        _check_rev(item, rev)
        if item["source_count"]:
            if not item["manual"]:
                raise ListError("This comes from a planned recipe. Mark it Already have instead.")
            # Remove only the hand-added part; the recipe still needs it.
            conn.execute(
                """UPDATE shopping_items SET manual = 0, manual_qty = '', rev = rev + 1,
                       updated_at = datetime('now') WHERE id = ?""",
                (item_id,),
            )
        else:
            conn.execute("DELETE FROM shopping_items WHERE id = ?", (item_id,))
        _bump_list(conn, week)


# ---- Kitchen Staples ----

def list_staples(conn: sqlite3.Connection, query: str = "") -> list[sqlite3.Row]:
    query = _clean_text(query, MAX_STAPLE).lower()
    rows = conn.execute("SELECT id, name, name_key FROM staples ORDER BY name COLLATE NOCASE").fetchall()
    return [r for r in rows if query in r["name"].lower()] if query else rows


def add_staple(conn: sqlite3.Connection, name: str) -> int:
    name = _clean_text(name, MAX_STAPLE)
    key = name_key(name)
    if not key:
        raise ListError("Type the name of a staple.")
    with write_transaction(conn):
        if conn.execute("SELECT 1 FROM staples WHERE name_key = ?", (key,)).fetchone():
            raise ListError(f"{name} is already a staple.")
        return conn.execute("INSERT INTO staples (name, name_key) VALUES (?, ?)", (name, key)).lastrowid


def rename_staple(conn: sqlite3.Connection, staple_id: int, name: str) -> None:
    name = _clean_text(name, MAX_STAPLE)
    key = name_key(name)
    if not key:
        raise ListError("A staple needs a name.")
    with write_transaction(conn):
        if conn.execute("SELECT 1 FROM staples WHERE id = ?", (staple_id,)).fetchone() is None:
            raise Missing("That staple was already removed.")
        if conn.execute("SELECT 1 FROM staples WHERE name_key = ? AND id != ?", (key, staple_id)).fetchone():
            raise ListError(f"{name} is already a staple.")
        conn.execute("UPDATE staples SET name = ?, name_key = ? WHERE id = ?", (name, key, staple_id))


def delete_staple(conn: sqlite3.Connection, staple_id: int) -> None:
    with write_transaction(conn):
        conn.execute("DELETE FROM staples WHERE id = ?", (staple_id,))


def add_staple_to_list(conn: sqlite3.Connection, week: date, staple_id: int) -> int:
    """We ran out: put a staple on this week's list. It stays a permanent staple."""
    staple = conn.execute("SELECT name, name_key FROM staples WHERE id = ?", (staple_id,)).fetchone()
    if staple is None:
        raise Missing("That staple was already removed.")
    w = week.isoformat()
    with write_transaction(conn):
        ensure_week(conn, week)
        # Match the list item the same way generation does ("unsalted butter" counts as Butter).
        rows = conn.execute("SELECT id, item_key, status FROM shopping_items WHERE week_start = ?", (w,)).fetchall()
        row = next((r for r in rows if matches_staple(r["item_key"], {staple["name_key"]})), None)
        if row is None:
            item_id = conn.execute(
                "INSERT INTO shopping_items (week_start, item_key, name, manual) VALUES (?, ?, ?, 1)",
                (w, staple["name_key"], staple["name"]),
            ).lastrowid
        else:
            item_id = row["id"]
            if row["status"] == "need":
                return item_id  # already on the list; never add a duplicate
            conn.execute(
                """UPDATE shopping_items SET status = 'need', manual = 1, stale = 0, cleared = 0, rev = rev + 1,
                       updated_at = datetime('now') WHERE id = ?""",
                (item_id,),
            )
        _bump_list(conn, week)
    return item_id


def make_item_staple(conn: sqlite3.Connection, week: date, item_id: int, rev: int | None) -> None:
    """'We always have this': save the item as a permanent staple and move it out of Need to Buy."""
    with write_transaction(conn):
        item = _get_item(conn, week, item_id)
        _check_rev(item, rev)
        conn.execute("INSERT OR IGNORE INTO staples (name, name_key) VALUES (?, ?)",
                     (item["name"][:MAX_STAPLE], item["item_key"]))
        conn.execute(
            """UPDATE shopping_items SET status = 'staple', change_note = '', rev = rev + 1,
                   updated_at = datetime('now') WHERE id = ?""",
            (item_id,),
        )
        _bump_list(conn, week)
