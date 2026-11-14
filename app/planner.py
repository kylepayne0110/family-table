# Weekly dinner plans: one optional recipe per day, Monday through Sunday.

# hashlib fingerprints a plan so we can tell when the shopping list is out of date.
import hashlib

# sqlite3 types the database connection.
import sqlite3

# date/timedelta work out weeks and their days.
from datetime import date, timedelta

# write_transaction locks the database for each change.
from app.database import write_transaction


DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class PlanError(Exception):
    """A plan change that can't be made (bad day, missing recipe)."""


# ---- Weeks ----

def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def parse_week(text: str | None, today: date | None = None) -> date | None:
    """'2026-11-09' -> that Monday. Blank means this week; anything invalid is None."""
    if not text:
        return monday_of(today or date.today())
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    # Keep weeks to a sensible range and always start on a Monday.
    if day.weekday() != 0 or not 2000 <= day.year <= 2100:
        return None
    return day


def week_label(week: date) -> str:
    """'Nov 9 – 15, 2026' or 'Dec 28, 2026 – Jan 3, 2027'."""
    end = week + timedelta(days=6)
    if week.year != end.year:
        return f"{week:%b %-d, %Y} – {end:%b %-d, %Y}"
    if week.month != end.month:
        return f"{week:%b %-d} – {end:%b %-d}, {end.year}"
    return f"{week:%b %-d} – {end:%-d}, {end.year}"


def ensure_week(conn: sqlite3.Connection, week: date) -> None:
    """Create the week's row if it doesn't exist (call inside a transaction)."""
    conn.execute("INSERT OR IGNORE INTO week_plans (week_start) VALUES (?)", (week.isoformat(),))


def versions(conn: sqlite3.Connection, week: date) -> tuple[int, int]:
    """(plan_version, list_version) for a week; (0, 0) if nothing is stored yet."""
    row = conn.execute(
        "SELECT plan_version, list_version FROM week_plans WHERE week_start = ?", (week.isoformat(),)
    ).fetchone()
    return (row["plan_version"], row["list_version"]) if row else (0, 0)


def _bump_plan(conn: sqlite3.Connection, week: date) -> None:
    conn.execute(
        "UPDATE week_plans SET plan_version = plan_version + 1, updated_at = datetime('now') WHERE week_start = ?",
        (week.isoformat(),),
    )


# ---- Reading a plan ----

def get_plan(conn: sqlite3.Connection, week: date, today: date | None = None) -> list[dict]:
    """Seven day dicts: name, date, is_today, and the planned recipe (or None)."""
    today = today or date.today()
    rows = conn.execute(
        """SELECT p.day, r.id, r.name, r.photo_filename, r.prep_minutes, r.cook_minutes
           FROM planned_meals p JOIN recipes r ON r.id = p.recipe_id
           WHERE p.week_start = ?""",
        (week.isoformat(),),
    ).fetchall()
    by_day = {row["day"]: dict(row) for row in rows}
    days = []
    for index, name in enumerate(DAY_NAMES):
        day_date = week + timedelta(days=index)
        days.append({
            "index": index, "name": name, "date": day_date,
            "is_today": day_date == today, "recipe": by_day.get(index),
        })
    return days


def plan_signature(conn: sqlite3.Connection, week: date) -> str:
    """Fingerprint of the ingredients the plan needs.

    Moving meals between days keeps the same fingerprint (same groceries), while
    adding, removing, or editing a planned recipe's ingredients changes it.
    """
    rows = conn.execute(
        """SELECT p.recipe_id, i.amount, i.name
           FROM planned_meals p JOIN ingredients i ON i.recipe_id = p.recipe_id
           WHERE p.week_start = ?
           ORDER BY p.recipe_id, p.day, i.position""",
        (week.isoformat(),),
    ).fetchall()
    meals = conn.execute(
        "SELECT recipe_id FROM planned_meals WHERE week_start = ? ORDER BY recipe_id", (week.isoformat(),)
    ).fetchall()
    text = repr(([m[0] for m in meals], [tuple(r) for r in rows]))
    return hashlib.sha256(text.encode()).hexdigest()


def planned_ingredient_lines(conn: sqlite3.Connection, week: date) -> list[tuple[int, str, str, str, int]]:
    """(recipe_id, recipe_name, amount, ingredient, times) for every planned recipe.

    `times` is how many dinners that recipe is planned for, so its ingredients count that often.
    """
    w = week.isoformat()
    rows = conn.execute(
        """SELECT r.id, r.name, i.amount, i.name, planned.times
           FROM (SELECT recipe_id, COUNT(*) AS times, MIN(day) AS first_day
                 FROM planned_meals WHERE week_start = ? GROUP BY recipe_id) AS planned
           JOIN recipes r ON r.id = planned.recipe_id
           JOIN ingredients i ON i.recipe_id = r.id
           ORDER BY planned.first_day, i.position""",
        (w,),
    ).fetchall()
    return [tuple(row) for row in rows]


def recipe_choices(conn: sqlite3.Connection, query: str = "") -> list[sqlite3.Row]:
    """Recipes for the picker, filtered by name words."""
    sql = "SELECT id, name, photo_filename, prep_minutes, cook_minutes FROM recipes WHERE 1 = 1"
    params: list = []
    for term in query.split()[:8]:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    return conn.execute(sql + " ORDER BY name COLLATE NOCASE", params).fetchall()


# ---- Changing a plan ----

def _check_day(day: int) -> None:
    if not 0 <= day <= 6:
        raise PlanError("That day isn't in this week.")


def set_meal(conn: sqlite3.Connection, week: date, day: int, recipe_id: int) -> None:
    """Plan a recipe for a day (replacing whatever was there)."""
    _check_day(day)
    if conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None:
        raise PlanError("That recipe isn't in the box anymore.")
    with write_transaction(conn):
        ensure_week(conn, week)
        conn.execute(
            """INSERT INTO planned_meals (week_start, day, recipe_id) VALUES (?, ?, ?)
               ON CONFLICT (week_start, day) DO UPDATE SET recipe_id = excluded.recipe_id""",
            (week.isoformat(), day, recipe_id),
        )
        _bump_plan(conn, week)


def clear_meal(conn: sqlite3.Connection, week: date, day: int) -> None:
    """Leave a day blank (leftovers, eating out, or no plan)."""
    _check_day(day)
    with write_transaction(conn):
        ensure_week(conn, week)
        conn.execute("DELETE FROM planned_meals WHERE week_start = ? AND day = ?", (week.isoformat(), day))
        _bump_plan(conn, week)


def move_meal(conn: sqlite3.Connection, week: date, from_day: int, to_day: int) -> None:
    """Move a dinner to another day. If that day has a dinner, the two swap."""
    _check_day(from_day)
    _check_day(to_day)
    if from_day == to_day:
        return
    w = week.isoformat()
    with write_transaction(conn):
        ensure_week(conn, week)
        meals = dict(conn.execute(
            "SELECT day, recipe_id FROM planned_meals WHERE week_start = ? AND day IN (?, ?)",
            (w, from_day, to_day),
        ).fetchall())
        if from_day not in meals:
            raise PlanError("There's no dinner on that day to move.")
        conn.execute("DELETE FROM planned_meals WHERE week_start = ? AND day IN (?, ?)", (w, from_day, to_day))
        conn.execute("INSERT INTO planned_meals (week_start, day, recipe_id) VALUES (?, ?, ?)",
                     (w, to_day, meals[from_day]))
        if to_day in meals:
            conn.execute("INSERT INTO planned_meals (week_start, day, recipe_id) VALUES (?, ?, ?)",
                         (w, from_day, meals[to_day]))
        _bump_plan(conn, week)
