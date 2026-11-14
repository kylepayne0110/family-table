# Shared helpers for the meal planner and shopping list tests.

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


def add_recipe(settings, name: str, ingredients: list[tuple[str, str]], photo: str | None = None) -> int:
    """Save a recipe straight to the test database (much faster than the form)."""
    conn = connect(settings.database_path)
    try:
        data = RecipeData(name=name, description="", category="weeknight", cuisine="", prep_minutes=10,
                          cook_minutes=20, servings=4, card_tag="", last_made=None,
                          ingredients=ingredients, steps=[("", "Cook it.")])
        return save_recipe(conn, data, photo)
    finally:
        conn.close()


def token(client) -> str:
    return csrf_from(client.get(f"/plan?week={W}").text)


def post(client, url: str, data: dict | None = None, fetch: bool = False, csrf: str | None = None):
    payload = {**(data or {}), "csrf_token": csrf if csrf is not None else token(client)}
    return client.post(url, data=payload, headers=FETCH if fetch else {})


def plan(client, day: int, recipe_id: int, week: str = W):
    return post(client, f"/plan/{week}/day/{day}", {"recipe_id": recipe_id})


def generate(client, week: str = W):
    return post(client, f"/plan/{week}/shopping-list")


def state(settings, week: date = WEEK) -> dict:
    conn = connect(settings.database_path)
    try:
        return list_state(conn, week)
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
