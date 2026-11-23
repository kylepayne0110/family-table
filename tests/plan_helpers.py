# Shared helpers for the meal planner and shopping list tests.

# re reads the checklist form; uuid makes request keys.
import re
import uuid

# date fixes the week under test so results don't depend on today.
from datetime import date

# Project modules used to set up data directly.
from app.database import connect
from app.meals import RecipeData, save_recipe
from app.shopping import list_state

# CSRF scraping helper from the main fixtures.
from tests.conftest import csrf_from


WEEK = date(2026, 11, 9)   # a Monday
W = WEEK.isoformat()
FETCH = {"x-requested-with": "fetch"}


def table_id(settings, name: str | None = None) -> int:
    """The test family's Table (the first one), or the Table with this name."""
    conn = connect(settings.database_path)
    try:
        if name:
            return conn.execute("SELECT id FROM family_tables WHERE name = ?", (name,)).fetchone()[0]
        return conn.execute("SELECT id FROM family_tables ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def add_recipe(settings, name: str, ingredients: list[tuple[str, str]], photo: str | None = None,
               table: int | None = None) -> int:
    """Save a recipe straight to the test database (much faster than the form)."""
    tid = table or table_id(settings)
    conn = connect(settings.database_path)
    try:
        data = RecipeData(name=name, description="", category="weeknight", cuisine="", prep_minutes=10,
                          cook_minutes=20, servings=4, card_tag="", last_made=None,
                          ingredients=ingredients, steps=[("", "Cook it.")])
        return save_recipe(conn, data, photo, table_id=tid)
    finally:
        conn.close()


def token(client) -> str:
    return csrf_from(client.get(f"/plan?week={W}").text)


def post(client, url: str, data: dict | None = None, fetch: bool = False, csrf: str | None = None):
    payload = {**(data or {}), "csrf_token": csrf if csrf is not None else token(client)}
    return client.post(url, data=payload, headers=FETCH if fetch else {})


def plan(client, day: int, recipe_id: int, week: str = W):
    return post(client, f"/plan/{week}/day/{day}", {"recipe_id": recipe_id})


def meal_id(settings, day: int, week: str = W) -> int:
    rows = query(settings, "SELECT id FROM planned_meals WHERE week_start = ? AND day = ?", (week, day))
    assert rows, f"nothing planned on day {day} of {week}"
    return rows[0][0]


def checklist(client, mid: int) -> str:
    """The "Add ingredients" checklist HTML for a planned dinner."""
    return client.get(f"/plan/meals/{mid}/ingredients", headers=FETCH).json()["html"]


def checked(html: str) -> list[int]:
    """Indexes the checklist starts checked (regular ingredients, not staples or already added)."""
    return [int(v) for v in re.findall(r'name="ingredient" value="(\d+)" checked', html)]


def add_ingredients(client, settings, day: int, indexes: list[int] | None = None, week: str = W,
                    key: str | None = None, fetch: bool = True, servings_seen: int | None = None):
    """Tap "Add N items" for one dinner. Default: whatever the checklist starts checked."""
    mid = meal_id(settings, day, week)
    html = checklist(client, mid)
    if indexes is None:
        indexes = checked(html)
    if servings_seen is None:
        found = re.search(r'name="servings_seen" value="(\d*)"', html).group(1)
        servings_seen = int(found) if found else None
    data = {"ingredient": [str(i) for i in indexes], "request_key": key or uuid.uuid4().hex}
    if servings_seen is not None:
        data["servings_seen"] = str(servings_seen)
    return post(client, f"/plan/meals/{mid}/ingredients", data, fetch=fetch)


def shop_week(client, settings, week: str = W):
    """Add each planned dinner's default checklist, Monday to Sunday."""
    for (day,) in query(settings, "SELECT day FROM planned_meals WHERE week_start = ? ORDER BY day", (week,)):
        add_ingredients(client, settings, day, week=week)


def state(settings, week: date = WEEK, table: int | None = None) -> dict:
    tid = table or table_id(settings)
    conn = connect(settings.database_path)
    try:
        return list_state(conn, tid, week)
    finally:
        conn.close()


def all_items(settings, week: date = WEEK) -> list[dict]:
    return [i for section in state(settings, week)["sections"].values() for i in section]


def item(settings, name: str, week: date = WEEK) -> dict:
    matches = [i for i in all_items(settings, week) if i["name"].lower() == name.lower()]
    assert len(matches) == 1, f"{name!r} found {len(matches)} times"
    return matches[0]


def query(settings, sql: str, params=()) -> list:
    conn = connect(settings.database_path)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


TACOS = [("1 lb", "ground beef"), ("1", "onion"), ("2", "tomatoes"), ("8", "corn tortillas"), ("1 tbsp", "butter")]
BURGERS = [("2 lbs", "ground beef"), ("1", "onion, sliced"), ("4", "burger buns"), ("", "salt and pepper")]
PARM = [("2", "chicken breasts"), ("1 cup", "marinara sauce"), ("1 cup", "shredded mozzarella"), ("1", "onion")]
