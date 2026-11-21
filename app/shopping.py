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
from app.ingredients import (
    format_quantity, matches_staple, name_key, needs_more, parse_line, quantity_totals,
)

# Plan helpers: the week row and one planned dinner.
from app.planner import ensure_week, get_meal

# The same serving math the recipe view and the assistant use.
from app.scaling import scale_ingredients, servings_factor


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

    return {
        "sections": sections,
        "need_count": len(sections["need"]),
        "bought_count": len(sections["bought"]),
        "shopping_total": len(sections["need"]) + len(sections["bought"]),
        "total": len(items),   # includes cleared items, so a fully shopped list isn't "empty"
        "version": meta["list_version"] if meta else 0,
    }


# ---- Shared helpers for list changes ----

def _bump_list(conn: sqlite3.Connection, week: date) -> None:
    conn.execute(
        "UPDATE week_plans SET list_version = list_version + 1, updated_at = datetime('now') WHERE week_start = ?",
        (week.isoformat(),),
    )


# ---- Adding one planned dinner's ingredients ----

def _staple_keys(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name_key FROM staples")}


def meal_lines(conn: sqlite3.Connection, meal: dict) -> list[dict]:
    """The dinner's ingredients at its current serving count (what the checklist and chat show)."""
    rows = conn.execute(
        "SELECT id, amount, name FROM ingredients WHERE recipe_id = ? ORDER BY position", (meal["recipe_id"],)
    ).fetchall()
    return scale_ingredients(rows, servings_factor(meal["servings"], meal["recipe_servings"]))


def _meal_sources(conn: sqlite3.Connection, meal_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT s.ingredient, s.amount, s.servings, i.name AS item_name, i.status, i.cleared
           FROM shopping_item_sources s JOIN shopping_items i ON i.id = s.item_id
           WHERE s.meal_id = ? ORDER BY s.rowid""",
        (meal_id,),
    )]


def meal_checklist(conn: sqlite3.Connection, meal: dict) -> dict:
    """What "Add ingredients" shows for one dinner.

    Regular ingredients start checked; Kitchen Staples and anything this dinner already put
    on the list start unchecked. `outdated` is True when servings changed after adding.
    """
    staples = _staple_keys(conn)
    added: dict[str, list[dict]] = {}
    for src in _meal_sources(conn, meal["id"]):
        added.setdefault(src["ingredient"], []).append(src)
    lines = meal_lines(conn, meal)
    for line in lines:
        keys = [p.key for p in parse_line(line["amount"], line["name"])]
        line["staple"] = bool(keys) and all(matches_staple(k, staples) for k in keys)
        line["added"] = added.get(line["original_name"], [])
        line["added_amount"] = " + ".join(dict.fromkeys(a["amount"] for a in line["added"] if a["amount"]))
        line["outdated"] = any(a["servings"] != meal["servings"] for a in line["added"])
        line["checked"] = not line["staple"] and not line["added"]
    return {
        "lines": lines,
        "selected": sum(1 for line in lines if line["checked"]),
        "outdated": any(line["outdated"] for line in lines),
        "added_count": sum(1 for line in lines if line["added"]),
        "added_servings": next((a["servings"] for line in lines for a in line["added"]), None),
    }


def _amount_parts(amount: str, name: str):
    """Parsed pieces of a source amount; '1/4 cup + 2 tbsp' is read as two pieces."""
    pieces = [piece for piece in amount.split(" + ")] if " + " in amount else [amount]
    return [p for piece in pieces for p in parse_line(piece, name)]


def _recalculate(conn: sqlite3.Connection, item_id: int, key: str, item_name: str) -> tuple[str, dict]:
    """An item's quantity and totals from every recipe amount that asked for it."""
    parts = []
    for src in conn.execute(
        "SELECT amount, ingredient, times FROM shopping_item_sources WHERE item_id = ? ORDER BY rowid", (item_id,)
    ):
        parsed = _amount_parts(src["amount"], src["ingredient"] or item_name)
        matching = [p for p in parsed if p.key == key] or parsed[:1]
        parts.extend(p for p in matching for _ in range(src["times"]))
    return format_quantity(parts), quantity_totals(parts)


def _apply_meal_lines(conn: sqlite3.Connection, meal: dict, lines: list[dict]) -> list[str]:
    """Put these scaled lines from one dinner on its week's list. Returns the item names touched.

    Each dinner's amount for an ingredient replaces what that dinner added before (it never
    stacks), so the same selection can't double an item. Other dinners' amounts, purchases,
    and hand-added items are kept.
    """
    w = meal["week_start"]
    names = []
    for line in lines:
        for parsed in parse_line(line["amount"], line["name"]):
            row = conn.execute("SELECT * FROM shopping_items WHERE week_start = ? AND item_key = ?",
                               (w, parsed.key)).fetchone()
            if row is None:
                item_id = conn.execute(
                    "INSERT INTO shopping_items (week_start, item_key, name, status) VALUES (?, ?, ?, 'need')",
                    (w, parsed.key, (parsed.display[:1].upper() + parsed.display[1:])[:MAX_NAME]),
                ).lastrowid
                item_name = parsed.display
            else:
                item_id, item_name = row["id"], row["name"]
            conn.execute("DELETE FROM shopping_item_sources WHERE item_id = ? AND meal_id = ? AND ingredient = ?",
                         (item_id, meal["id"], line["original_name"]))
            conn.execute(
                """INSERT INTO shopping_item_sources (item_id, recipe_id, recipe_name, amount, times, meal_id,
                       ingredient, servings) VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                (item_id, meal["recipe_id"], meal["recipe_name"], line["amount"], meal["id"],
                 line["original_name"], meal["servings"]),
            )
            quantity, totals = _recalculate(conn, item_id, parsed.key, item_name)
            status, cleared, note = "need", 0, ""
            if row is not None:
                status, cleared, note = row["status"], row["cleared"], row["change_note"]
                if status == "staple":
                    status = "need"        # chosen on purpose, so it goes on Need to Buy
                elif status in ("bought", "have") or cleared:
                    basis = json.loads(row["bought_totals"] or row["totals_json"] or "{}")
                    if needs_more(basis, totals):
                        status, cleared = "need", 0
                        note = f"Need more: was {row['quantity'] or 'some'}, now {quantity or 'some'}"
            conn.execute(
                """UPDATE shopping_items SET quantity = ?, totals_json = ?, status = ?, cleared = ?, change_note = ?,
                       stale = 0, rev = rev + 1, updated_at = datetime('now') WHERE id = ?""",
                (quantity, json.dumps(totals, sort_keys=True), status, cleared, note, item_id),
            )
            names.append(item_name[:1].upper() + item_name[1:])
    return list(dict.fromkeys(names))


def _seen_request(conn: sqlite3.Connection, request_key: str | None, meal_id: int) -> bool:
    """True if this exact request was already applied; otherwise remember it."""
    if not request_key:
        return False
    if conn.execute("SELECT 1 FROM meal_list_requests WHERE request_key = ?", (request_key,)).fetchone():
        return True
    conn.execute("INSERT INTO meal_list_requests (request_key, meal_id) VALUES (?, ?)", (request_key, meal_id))
    return False


def add_meal_ingredients(conn: sqlite3.Connection, meal_id: int, indexes: list[int],
                         request_key: str | None, servings_seen: int | None = None) -> dict:
    """Add the chosen ingredients of one planned dinner to that week's shared list.

    `request_key` comes from the checklist form: a double tap or a retried request with the
    same key changes nothing the second time. `servings_seen` is the serving count the
    checklist showed; if it changed meanwhile, nothing is added and the caller shows the new amounts.
    """
    with write_transaction(conn):
        meal = get_meal(conn, meal_id)
        if meal is None:
            raise Missing("That dinner isn't on the plan anymore.")
        if _seen_request(conn, request_key, meal_id):
            return {"duplicate": True, "names": []}
        if servings_seen is not None and servings_seen != meal["servings"]:
            raise Conflict("The serving count just changed, so here are the new amounts. Check them and add again.")
        lines = meal_lines(conn, meal)
        chosen = [lines[i] for i in dict.fromkeys(indexes) if 0 <= i < len(lines)]
        if not chosen:
            raise ListError("Choose at least one ingredient to add.")
        ensure_week(conn, meal["week"])
        names = _apply_meal_lines(conn, meal, chosen)
        _bump_list(conn, meal["week"])
    return {"duplicate": False, "names": names}


def update_meal_ingredients(conn: sqlite3.Connection, meal_id: int, request_key: str | None) -> dict:
    """After a serving change: bring this dinner's amounts on the list up to date (only when asked)."""
    with write_transaction(conn):
        meal = get_meal(conn, meal_id)
        if meal is None:
            raise Missing("That dinner isn't on the plan anymore.")
        if _seen_request(conn, request_key, meal_id):
            return {"duplicate": True, "names": []}
        added = {src["ingredient"] for src in _meal_sources(conn, meal_id)}
        chosen = [line for line in meal_lines(conn, meal) if line["original_name"] in added]
        if not chosen:
            raise ListError("Nothing from this dinner is on the shopping list yet.")
        names = _apply_meal_lines(conn, meal, chosen)
        _bump_list(conn, meal["week"])
    return {"duplicate": False, "names": names}


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
