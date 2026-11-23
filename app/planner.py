# Weekly dinner plans: one optional recipe per day, Monday through Sunday.
# Days before the household's today are history: their dinners are kept but leave the active board.

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


def ensure_week(conn: sqlite3.Connection, table_id: int, week: date) -> None:
    """Create the Table's row for the week if it doesn't exist (call inside a transaction)."""
    conn.execute("INSERT OR IGNORE INTO week_plans (table_id, week_start) VALUES (?, ?)", (table_id, week.isoformat()))


def versions(conn: sqlite3.Connection, table_id: int, week: date) -> tuple[int, int]:
    """(plan_version, list_version) for a Table's week; (0, 0) if nothing is stored yet."""
    row = conn.execute(
        "SELECT plan_version, list_version FROM week_plans WHERE table_id = ? AND week_start = ?",
        (table_id, week.isoformat()),
    ).fetchone()
    return (row["plan_version"], row["list_version"]) if row else (0, 0)


def _bump_plan(conn: sqlite3.Connection, table_id: int, week: date) -> None:
    conn.execute(
        """UPDATE week_plans SET plan_version = plan_version + 1, updated_at = datetime('now')
           WHERE table_id = ? AND week_start = ?""",
        (table_id, week.isoformat()),
    )


# ---- Reading a plan ----

def day_date(week: date, day: int) -> date:
    return week + timedelta(days=day)


def is_past(week: date, day: int, today: date) -> bool:
    """True once the day is over in the household's time zone (a dinner stays up until midnight)."""
    return day_date(week, day) < today


def get_plan(conn: sqlite3.Connection, table_id: int, week: date, today: date | None = None) -> list[dict]:
    """Seven day dicts: name, date, is_today, is_past, and the planned recipe (or None).

    `recipe` includes `meal_id` and the dinner's serving count.
    """
    today = today or date.today()
    # A dinner is a recipe from the box or a packaged meal (recipe_id NULL, details in packaged_meals).
    rows = conn.execute(
        """SELECT p.day, p.id AS meal_id, p.servings AS meal_servings, r.id,
                  COALESCE(r.name, pk.name) AS name, r.photo_filename, r.prep_minutes, r.cook_minutes, r.servings,
                  CASE WHEN p.recipe_id IS NULL THEN 'packaged' ELSE 'recipe' END AS kind,
                  pk.brand, pk.package_size
           FROM planned_meals p LEFT JOIN recipes r ON r.id = p.recipe_id
           LEFT JOIN packaged_meals pk ON pk.meal_id = p.id
           WHERE p.table_id = ? AND p.week_start = ? AND (r.id IS NOT NULL OR pk.id IS NOT NULL)""",
        (table_id, week.isoformat()),
    ).fetchall()
    by_day = {row["day"]: dict(row) for row in rows}
    days = []
    for index, name in enumerate(DAY_NAMES):
        when = day_date(week, index)
        days.append({
            "index": index, "name": name, "date": when,
            "is_today": when == today, "is_past": when < today, "recipe": by_day.get(index),
        })
    return days


def get_meal(conn: sqlite3.Connection, meal_id: int) -> dict | None:
    """One planned dinner with its recipe's basics, or None."""
    row = conn.execute(
        """SELECT p.id, p.table_id, p.week_start, p.day, p.recipe_id, p.servings AS meal_servings,
                  COALESCE(r.name, pk.name) AS recipe_name, r.servings AS recipe_servings,
                  CASE WHEN p.recipe_id IS NULL THEN 'packaged' ELSE 'recipe' END AS kind
           FROM planned_meals p LEFT JOIN recipes r ON r.id = p.recipe_id
           LEFT JOIN packaged_meals pk ON pk.meal_id = p.id
           WHERE p.id = ? AND (r.id IS NOT NULL OR pk.id IS NOT NULL)""",
        (meal_id,),
    ).fetchone()
    if row is None:
        return None
    meal = dict(row)
    meal["week"] = date.fromisoformat(meal["week_start"])
    meal["date"] = day_date(meal["week"], meal["day"])
    meal["day_name"] = DAY_NAMES[meal["day"]]
    # The serving count shown and used everywhere: the dinner's own choice, else the recipe's yield.
    meal["servings"] = meal["meal_servings"] or meal["recipe_servings"]
    return meal


def recipe_choices(conn: sqlite3.Connection, table_id: int, query: str = "") -> list[sqlite3.Row]:
    """A Table's recipes for the picker, filtered by name words."""
    sql = "SELECT id, name, photo_filename, prep_minutes, cook_minutes FROM recipes WHERE table_id = ?"
    params: list = [table_id]
    for term in query.split()[:8]:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    return conn.execute(sql + " ORDER BY name COLLATE NOCASE", params).fetchall()


# ---- Changing a plan ----

def _check_day(day: int, week: date | None = None, today: date | None = None) -> None:
    if not 0 <= day <= 6:
        raise PlanError("That day isn't in this week.")
    if today is not None and week is not None and is_past(week, day, today):
        raise PlanError("That day is over, so its dinner stays in the history.")


def _label_photos(conn: sqlite3.Connection, where: str, params: tuple) -> list[str]:
    """Label photos of packaged meals about to be removed (the caller deletes the files).

    `where` is always one of the fixed conditions written in this module, never request data.
    """
    return [r[0] for r in conn.execute(
        f"""SELECT pk.label_photo FROM packaged_meals pk JOIN planned_meals p ON p.id = pk.meal_id
            WHERE {where} AND pk.label_photo IS NOT NULL""", params)]


def set_meal(conn: sqlite3.Connection, table_id: int, week: date, day: int, recipe_id: int,
             today: date | None = None) -> list[str]:
    """Plan a recipe for a day, replacing whatever was there (the "Change" picker).

    Choosing the recipe that's already planned keeps that dinner (and its servings and chat).
    With `today`, days that are already over can't be changed. Returns label photos of a
    replaced packaged meal, for the caller to delete.
    """
    _check_day(day, week, today)
    w = week.isoformat()
    with write_transaction(conn):
        # Only a recipe from this Table's own book can be planned here.
        if conn.execute("SELECT 1 FROM recipes WHERE id = ? AND table_id = ?", (recipe_id, table_id)).fetchone() is None:
            raise PlanError("That recipe isn't in the box anymore.")
        ensure_week(conn, table_id, week)
        # "recipe_id IS NULL" covers a packaged meal (NULL != x is never true in SQL).
        photos = _label_photos(conn, "p.table_id = ? AND p.week_start = ? AND p.day = ? "
                                     "AND (p.recipe_id IS NULL OR p.recipe_id != ?)", (table_id, w, day, recipe_id))
        conn.execute("""DELETE FROM planned_meals WHERE table_id = ? AND week_start = ? AND day = ?
                        AND (recipe_id IS NULL OR recipe_id != ?)""", (table_id, w, day, recipe_id))
        if conn.execute("SELECT 1 FROM planned_meals WHERE table_id = ? AND week_start = ? AND day = ?",
                        (table_id, w, day)).fetchone() is None:
            conn.execute("INSERT INTO planned_meals (table_id, week_start, day, recipe_id) VALUES (?, ?, ?, ?)",
                         (table_id, w, day, recipe_id))
        _bump_plan(conn, table_id, week)
    return photos


def clear_meal(conn: sqlite3.Connection, table_id: int, week: date, day: int, today: date | None = None) -> list[str]:
    """Leave a day blank (leftovers, eating out, or no plan). Returns label photos to delete."""
    _check_day(day, week, today)
    w = week.isoformat()
    with write_transaction(conn):
        ensure_week(conn, table_id, week)
        photos = _label_photos(conn, "p.table_id = ? AND p.week_start = ? AND p.day = ?", (table_id, w, day))
        conn.execute("DELETE FROM planned_meals WHERE table_id = ? AND week_start = ? AND day = ?", (table_id, w, day))
        _bump_plan(conn, table_id, week)
    return photos


def move_meal(conn: sqlite3.Connection, table_id: int, week: date, from_day: int, to_day: int,
              today: date | None = None) -> None:
    """Move a dinner to another day. If that day has a dinner, the two swap.

    The rows keep their ids, so each dinner's servings, shopping additions, and chat go with it.
    """
    _check_day(from_day, week, today)
    _check_day(to_day, week, today)
    if from_day == to_day:
        return
    w = week.isoformat()
    with write_transaction(conn):
        ensure_week(conn, table_id, week)
        meals = dict(conn.execute(
            "SELECT day, id FROM planned_meals WHERE table_id = ? AND week_start = ? AND day IN (?, ?)",
            (table_id, w, from_day, to_day),
        ).fetchall())
        if from_day not in meals:
            raise PlanError("There's no dinner on that day to move.")
        conn.execute("UPDATE planned_meals SET day = ? WHERE id = ?", (to_day, meals[from_day]))
        if to_day in meals:
            conn.execute("UPDATE planned_meals SET day = ? WHERE id = ?", (from_day, meals[to_day]))
        _bump_plan(conn, table_id, week)


def set_meal_servings(conn: sqlite3.Connection, meal_id: int, servings: int | None) -> dict:
    """Cook one planned dinner for a different number of people (None = back to the recipe's yield).

    Only this dinner changes: the saved recipe, other dinners, and the shopping list stay as they are.
    """
    with write_transaction(conn):
        meal = get_meal(conn, meal_id)
        if meal is None:
            raise PlanError("That dinner isn't on the plan anymore.")
        if meal["kind"] != "recipe":
            raise PlanError("Servings can only be changed for recipes from the box.")
        if servings is not None and not meal["recipe_servings"]:
            raise PlanError("First tell us how many servings the recipe makes as written.")
        if servings == meal["recipe_servings"]:
            servings = None   # same as the recipe: store it as "not changed"
        conn.execute("UPDATE planned_meals SET servings = ? WHERE id = ?", (servings, meal_id))
        _bump_plan(conn, meal["table_id"], meal["week"])
    return get_meal(conn, meal_id)


def set_recipe_yield(conn: sqlite3.Connection, recipe_id: int, servings: int) -> None:
    """Record how many servings an older recipe makes. Only fills a missing value."""
    with write_transaction(conn):
        conn.execute(
            "UPDATE recipes SET servings = ?, updated_at = datetime('now') WHERE id = ? AND servings IS NULL",
            (servings, recipe_id),
        )
