"""Past dinners leaving the planner, per-dinner shopping, serving changes, and the Cooking Assistant."""

# json builds stand-in model replies; re reads rendered pages; sqlite3 builds an old database.
import json
import re
import sqlite3

# datetime/zoneinfo set the household clock to exact moments.
from datetime import datetime
from zoneinfo import ZoneInfo

# pytest provides fixtures.
import pytest

# TestClient starts the app against an upgraded database.
from fastapi.testclient import TestClient

# Project modules.
from app.database import connect
from app.main import create_app
from app.meals import RecipeData, save_recipe
from tests.conftest import login
from tests.plan_helpers import (FETCH, W, add_ingredients, all_items, checked, checklist, item, meal_id, plan, post,
                                query)

CHICAGO = ZoneInfo("America/Chicago")

NUGGETS_INGREDIENTS = [("1 1/2 lbs", "chicken breasts"), ("2 cups", "breadcrumbs"), ("", "salt to taste")]
NUGGETS_STEPS = [
    ("Prep", "Heat the oven to 400°F. Cut 1 1/2 lbs chicken breasts into 1-inch pieces."),
    ("Coat", "Toss the chicken in 2 cups breadcrumbs until coated."),
    ("Bake", "Bake the coated chicken for 15 minutes, then flip and bake 5 minutes more."),
]


def save(settings, name, ingredients, steps, servings=4):
    conn = connect(settings.database_path)
    try:
        data = RecipeData(name=name, description="", category="kids-pick", cuisine="", prep_minutes=15,
                          cook_minutes=20, servings=servings, card_tag="", last_made=None,
                          ingredients=ingredients, steps=steps)
        return save_recipe(conn, data, None)
    finally:
        conn.close()


@pytest.fixture
def nuggets(auth_client, settings):
    return save(settings, "Homemade Chicken Nuggets", NUGGETS_INGREDIENTS, NUGGETS_STEPS)


def set_clock(client, *args):
    client.app.state.clock.frozen = datetime(*args, tzinfo=CHICAGO)


def day_card(html: str, day: int) -> str:
    match = re.search(rf'<li class="day-card[^"]*" id="day-{day}"[^>]*>(.*?)</li>', html, re.S)
    assert match, f"no card for day {day}"
    return match.group(1)


def servings(client, mid, value=None, reset=False):
    data = {"reset": "1"} if reset else {"servings": str(value)}
    return post(client, f"/plan/meals/{mid}/servings", data, fetch=True)


def chat(client, mid, text, key=None):
    data = {"message": text}
    if key:
        data["request_key"] = key
    return post(client, f"/plan/meals/{mid}/chat", data, fetch=True)


def messages(settings, mid):
    return query(settings, "SELECT role, body, kind FROM meal_messages WHERE meal_id = ? ORDER BY id", (mid,))


def last_reply(settings, mid):
    return messages(settings, mid)[-1][1]


# ---- 1. Past dinners leave the active planner at local midnight ----

def test_friday_dinner_shows_until_midnight_and_clears_on_saturday(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)                       # Friday, Nov 13
    add_ingredients(auth_client, settings, 4)           # groceries already on the list
    groceries = all_items(settings)

    set_clock(auth_client, 2026, 11, 13, 23, 59)        # 11:59 p.m. Friday, household time
    page = auth_client.get(f"/plan?week={W}").text
    assert "Homemade Chicken Nuggets" in day_card(page, 4)
    assert "tonight" in day_card(page, 4)

    set_clock(auth_client, 2026, 11, 14, 0, 0)          # midnight: Saturday
    page = auth_client.get(f"/plan?week={W}").text
    friday = day_card(page, 4)
    assert "Homemade Chicken Nuggets" not in friday and "This day is over." in friday
    assert "Add dinner" not in friday
    history = re.search(r'<section class="plan-history".*?</section>', page, re.S).group(0)
    assert "Friday, Nov 13" in history and "Homemade Chicken Nuggets" in history

    # Kept: the plan row (history), the recipe, and the shopping list.
    assert query(settings, "SELECT day, recipe_id FROM planned_meals") == [(4, nuggets)]
    assert auth_client.get(f"/recipes/{nuggets}").status_code == 200
    assert all_items(settings) == groceries


def test_midnight_uses_the_household_time_zone_not_utc(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    # 05:30 UTC Saturday is still 11:30 p.m. Friday in Chicago.
    auth_client.app.state.clock.frozen = datetime(2026, 11, 14, 5, 30, tzinfo=ZoneInfo("UTC"))
    assert "Homemade Chicken Nuggets" in day_card(auth_client.get(f"/plan?week={W}").text, 4)


def test_time_zone_is_configurable(tmp_path):
    from app.config import Settings
    from tests.conftest import PASSWORD_HASH, USERNAME
    settings = Settings(data_dir=tmp_path / "d", username=USERNAME, password_hash=PASSWORD_HASH,
                        timezone="Pacific/Auckland")
    app = create_app(settings)
    app.state.clock.frozen = datetime(2026, 11, 13, 12, 0, tzinfo=ZoneInfo("UTC"))   # already Saturday in NZ
    assert app.state.clock.today().isoformat() == "2026-11-14"


def test_planner_page_left_open_overnight_gets_a_fresh_board(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    set_clock(auth_client, 2026, 11, 13, 20, 0)
    data = auth_client.get(f"/plan/{W}/live?since=-1&today=", headers=FETCH).json()
    assert data["today"] == "2026-11-13"
    assert auth_client.get(f"/plan/{W}/live?since={data['version']}&today=2026-11-13").status_code == 204

    set_clock(auth_client, 2026, 11, 14, 7, 0)          # the phone wakes up Saturday morning
    fresh = auth_client.get(f"/plan/{W}/live?since={data['version']}&today=2026-11-13").json()
    assert fresh["today"] == "2026-11-14" and "Homemade Chicken Nuggets" not in day_card(fresh["html"], 4)


def test_days_that_are_over_cannot_be_changed(auth_client, settings, nuggets):
    plan(auth_client, 0, nuggets)                       # Monday
    set_clock(auth_client, 2026, 11, 11, 9, 0)          # Wednesday
    assert plan(auth_client, 1, nuggets).status_code == 422
    assert post(auth_client, f"/plan/{W}/day/0/clear").status_code == 422
    assert post(auth_client, f"/plan/{W}/move", {"from_day": 0, "to_day": 4}).status_code == 422
    assert auth_client.get(f"/plan/{W}/day/1/choose").status_code == 404
    assert query(settings, "SELECT day FROM planned_meals") == [(0,)]
    assert plan(auth_client, 2, nuggets).status_code == 303   # today is still open


# ---- 2. Add ingredients one dinner at a time ----

def test_checklist_starts_regular_checked_and_staples_unchecked(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    html = checklist(auth_client, meal_id(settings, 4))
    assert checked(html) == [0, 1]                       # chicken, breadcrumbs; salt is a staple
    assert "Add 2 items" in html
    assert "1 1/2 lbs" in html and "2 cups" in html


def test_friday_nuggets_example_adds_only_chicken(auth_client, settings, nuggets):
    saturday = save(settings, "Spaghetti", [("1 lb", "spaghetti"), ("1 jar", "marinara sauce")], [("", "Boil.")])
    plan(auth_client, 4, nuggets)
    plan(auth_client, 5, saturday)
    post(auth_client, f"/shopping/{W}/items", {"name": "Paper towels"})     # already on the list

    response = add_ingredients(auth_client, settings, 4, indexes=[0])      # breadcrumbs unchecked: "Add 1 item"
    assert response.status_code == 200
    assert "Added Chicken breasts to the shopping list." == response.json()["message"]
    assert sorted(i["name"] for i in all_items(settings)) == ["Chicken breasts", "Paper towels"]
    chicken = item(settings, "Chicken breasts")
    assert chicken["display_qty"] == "1 1/2 lbs" and chicken["status"] == "need"
    assert [s["recipe_name"] for s in chicken["sources"]] == ["Homemade Chicken Nuggets"]


def test_double_tap_or_retry_adds_the_selection_once(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    first = add_ingredients(auth_client, settings, 4, indexes=[0], key="same-key-123")
    again = add_ingredients(auth_client, settings, 4, indexes=[0], key="same-key-123")
    assert first.status_code == again.status_code == 200
    assert again.json()["message"] == "Those were already added."
    assert item(settings, "Chicken breasts")["display_qty"] == "1 1/2 lbs"
    assert query(settings, "SELECT COUNT(*) FROM shopping_item_sources") == [(1,)]


def test_checklist_marks_what_this_dinner_already_added(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    add_ingredients(auth_client, settings, 4, indexes=[0])
    html = checklist(auth_client, meal_id(settings, 4))
    assert checked(html) == [1]                          # chicken is "On the list", so unchecked now
    assert "On the list" in html


def test_two_dinners_with_the_same_recipe_each_count(auth_client, settings, nuggets):
    plan(auth_client, 1, nuggets)
    plan(auth_client, 4, nuggets)
    add_ingredients(auth_client, settings, 1, indexes=[0])
    add_ingredients(auth_client, settings, 4, indexes=[0])
    assert item(settings, "Chicken breasts")["display_qty"] == "3 lbs"


# ---- 3. Serving adjustments ----

def test_changing_servings_scales_this_dinner_only(auth_client, settings, nuggets):
    plan(auth_client, 1, nuggets)
    plan(auth_client, 4, nuggets)
    tuesday, friday = meal_id(settings, 1), meal_id(settings, 4)
    response = servings(auth_client, friday, 8)
    assert response.status_code == 200 and response.json()["servings"] == 8
    recipe_html = response.json()["recipe"]
    assert "3 lbs" in recipe_html and "4 cups" in recipe_html and "salt to taste" in recipe_html

    page = auth_client.get(f"/plan/meals/{friday}").text      # remembered for this dinner
    assert 'value="8"' in page and "Cut 3 lbs chicken breasts" in page and "Toss the chicken in 4 cups breadcrumbs" in page
    assert "400°F" in page and "15 minutes" in page and "1-inch" in page   # temperatures and times as written
    assert "1 1/2 lbs" in auth_client.get(f"/plan/meals/{tuesday}").text   # the other dinner is unchanged
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (nuggets,)) == [(4,)]
    assert query(settings, "SELECT amount FROM ingredients WHERE recipe_id = ? ORDER BY position", (nuggets,)) == [
        ("1 1/2 lbs",), ("2 cups",), ("",)]


def test_repeated_changes_always_scale_from_the_original(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    for value in (3, 7, 5, 3):
        servings(auth_client, mid, value)
    html = servings(auth_client, mid, 8).json()["recipe"]
    assert "3 lbs" in html and "4 cups" in html               # exactly double, no drift
    html = servings(auth_client, mid, 3).json()["recipe"]
    assert "1 1/8 lbs" in html and "1 1/2 cups" in html       # readable fractions


def test_reset_returns_to_the_recipe_yield(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    servings(auth_client, mid, 8)
    html = servings(auth_client, mid, reset=True).json()["recipe"]
    assert "1 1/2 lbs" in html and query(settings, "SELECT servings FROM planned_meals") == [(None,)]


def test_servings_form_works_without_javascript(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    response = post(auth_client, f"/plan/meals/{mid}/servings", {"servings_typed": "6"})
    assert response.status_code == 303 and response.headers["location"] == f"/plan/meals/{mid}?msg=servings"
    assert "2 1/4 lbs" in auth_client.get(response.headers["location"]).text
    assert post(auth_client, f"/plan/meals/{mid}/servings", {"servings": "0"}, fetch=True).status_code == 422


def test_older_recipe_without_servings_asks_for_its_yield(auth_client, settings):
    rid = save(settings, "Grandma's Soup", [("2 cups", "broth")], [("", "Simmer.")], servings=None)
    plan(auth_client, 4, rid)
    mid = meal_id(settings, 4)
    page = auth_client.get(f"/plan/meals/{mid}").text
    assert "How many servings does this recipe make as written?" in page and "2 cups" in page
    assert servings(auth_client, mid, 8).status_code == 422          # can't scale without the yield
    post(auth_client, f"/plan/meals/{mid}/yield", {"servings": "4"})
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (rid,)) == [(4,)]
    assert "4 cups" in servings(auth_client, mid, 8).json()["recipe"]


def test_changing_servings_leaves_the_list_until_asked(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    add_ingredients(auth_client, settings, 4, indexes=[0])
    servings(auth_client, mid, 8)
    assert item(settings, "Chicken breasts")["display_qty"] == "1 1/2 lbs"   # not touched
    html = checklist(auth_client, mid)
    assert "You changed servings after adding groceries" in html and "Update list to 8 servings" in html

    post(auth_client, f"/plan/meals/{mid}/ingredients/update", {"request_key": "update-key-1"}, fetch=True)
    assert item(settings, "Chicken breasts")["display_qty"] == "3 lbs"
    assert "Breadcrumbs" not in {i["name"] for i in all_items(settings)}       # only what was added before


def test_checklist_from_before_a_serving_change_is_refused(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    servings(auth_client, mid, 8)
    response = add_ingredients(auth_client, settings, 4, indexes=[0], servings_seen=4)   # stale form
    assert response.status_code == 409 and all_items(settings) == []
    assert "3 lbs" in response.json()["checklist"]


# ---- Cooking Assistant ----

def test_unit_conversion_is_answered_by_the_app(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    chat(auth_client, mid, "How many cups are in one US quart?")
    assert last_reply(settings, mid) == "1 US quart = 4 cups."
    chat(auth_client, mid, "How many cups is 500 grams?")
    assert last_reply(settings, mid).startswith("Which ingredient is it?")
    chat(auth_client, mid, "How many cups is 500 grams of flour?")
    assert "about 4 1/8 cups" in last_reply(settings, mid)
    chat(auth_client, mid, "How many cups is 500 grams of saffron?")
    assert "rather not guess" in last_reply(settings, mid)


def test_make_this_for_eight_uses_the_serving_selector(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    response = chat(auth_client, mid, "Make this for eight tonight.")
    data = response.json()
    assert data["servings"] == 8 and "3 lbs" in data["recipe"]          # the visible recipe updates at once
    assert query(settings, "SELECT servings FROM planned_meals WHERE id = ?", (mid,)) == [(8,)]
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (nuggets,)) == [(4,)]
    assert "set for 8 servings" in last_reply(settings, mid)


def test_quantity_question_uses_tonights_servings(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    servings(auth_client, mid, 8)
    chat(auth_client, mid, "How much chicken do I need tonight?")
    assert last_reply(settings, mid) == "For tonight's 8 servings: 3 lbs chicken breasts."


def test_model_gets_the_open_recipe_and_answers_instructions(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    servings(auth_client, mid, 8)
    seen = {}

    def model(messages, response_format=None):
        seen["messages"] = messages
        return json.dumps({"reply": "Bake the coated chicken for 15 minutes, then flip it.", "action": "none"})

    auth_client.app.state.ask_model = model
    chat(auth_client, mid, "What do I do after coating the chicken?")
    system = seen["messages"][0]["content"]
    assert "Tonight's dinner is for 8 servings." in system and "- 3 lbs chicken breasts" in system
    assert "Toss the chicken in 4 cups breadcrumbs" in system and "400°F" in system
    assert seen["messages"][-1] == {"role": "user", "content": "What do I do after coating the chicken?"}
    assert last_reply(settings, mid) == "Bake the coated chicken for 15 minutes, then flip it."


def test_model_can_only_propose_and_app_checks_it(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    auth_client.app.state.ask_model = lambda m, f=None: json.dumps(
        {"reply": "Done!", "action": "set_servings", "servings": 500})          # out of range: ignored
    chat(auth_client, mid, "we have a big crowd")
    assert query(settings, "SELECT servings FROM planned_meals") == [(None,)]


def test_permanent_edit_needs_confirmation_and_keeps_the_baseline(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    servings(auth_client, mid, 8)                                            # tonight: 8, recipe: 4
    data = chat(auth_client, mid, "From now on, use panko instead of regular breadcrumbs.").json()
    assert "Nothing is saved until you confirm" in data["log"]
    assert "<del>breadcrumbs</del>" in data["log"] and "<ins>panko</ins>" in data["log"]
    assert "<del>Toss the chicken in 2 cups breadcrumbs until coated.</del>" in data["log"]   # saved wording
    assert query(settings, "SELECT name FROM ingredients WHERE recipe_id = ? AND position = 1", (nuggets,)) == [
        ("breadcrumbs",)]                                                    # nothing saved yet

    pid = query(settings, "SELECT id FROM recipe_edit_proposals")[0][0]
    data = post(auth_client, f"/plan/meals/{mid}/proposals/{pid}/confirm", fetch=True).json()
    assert query(settings, "SELECT amount, name FROM ingredients WHERE recipe_id = ? ORDER BY position", (nuggets,)) == [
        ("1 1/2 lbs", "chicken breasts"), ("2 cups", "panko"), ("", "salt to taste")]
    assert query(settings, "SELECT body FROM steps WHERE recipe_id = ? AND position = 1", (nuggets,)) == [
        ("Toss the chicken in 2 cups panko until coated.",)]
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (nuggets,)) == [(4,)]   # baseline kept
    assert query(settings, "SELECT servings FROM planned_meals WHERE id = ?", (mid,)) == [(8,)]
    assert "4 cups panko" in data["recipe"]                                  # tonight still for 8
    # Confirming twice changes nothing more.
    post(auth_client, f"/plan/meals/{mid}/proposals/{pid}/confirm", fetch=True)
    assert query(settings, "SELECT status FROM recipe_edit_proposals") == [("confirmed",)]


def test_cancelled_edit_changes_nothing(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    chat(auth_client, mid, "Replace breadcrumbs with crushed cornflakes from now on")
    pid = query(settings, "SELECT id FROM recipe_edit_proposals")[0][0]
    before = query(settings, "SELECT name FROM ingredients ORDER BY id") + query(settings, "SELECT body FROM steps ORDER BY id")
    post(auth_client, f"/plan/meals/{mid}/proposals/{pid}/cancel", fetch=True)
    after = query(settings, "SELECT name FROM ingredients ORDER BY id") + query(settings, "SELECT body FROM steps ORDER BY id")
    assert before == after
    assert query(settings, "SELECT status FROM recipe_edit_proposals") == [("cancelled",)]
    assert last_reply(settings, mid) == "Okay, I left the saved recipe as it was."


def test_edit_proposal_goes_stale_if_the_recipe_changed(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    chat(auth_client, mid, "From now on use panko instead of breadcrumbs")
    conn = connect(settings.database_path)
    with conn:
        conn.execute("UPDATE ingredients SET name = 'seasoned breadcrumbs' WHERE recipe_id = ? AND position = 1", (nuggets,))
    conn.close()
    pid = query(settings, "SELECT id FROM recipe_edit_proposals")[0][0]
    post(auth_client, f"/plan/meals/{mid}/proposals/{pid}/confirm", fetch=True)
    assert query(settings, "SELECT status FROM recipe_edit_proposals") == [("stale",)]
    assert query(settings, "SELECT body FROM steps WHERE recipe_id = ? AND position = 1", (nuggets,)) == [
        ("Toss the chicken in 2 cups breadcrumbs until coated.",)]       # all or nothing


def test_unclear_request_gets_one_clarifying_question(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    chat(auth_client, mid, "Use panko instead of breadcrumbs")
    assert messages(settings, mid)[-1] == (
        "assistant", "Do you want me to change the saved recipe from now on, or is this just for tonight?", "clarify")
    assert query(settings, "SELECT COUNT(*) FROM recipe_edit_proposals") == [(0,)]
    chat(auth_client, mid, "just tonight")
    assert "The saved recipe stays the same" in last_reply(settings, mid)
    chat(auth_client, mid, "Use panko instead of breadcrumbs")
    chat(auth_client, mid, "from now on please")
    assert query(settings, "SELECT status FROM recipe_edit_proposals") == [("pending",)]


def test_assistant_offline_keeps_everything_else_working(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    response = chat(auth_client, mid, "Can I make these in an air fryer?")   # needs the model (offline in tests)
    assert response.status_code == 200
    assert messages(settings, mid)[-1][2] == "offline"
    assert "Ollama isn't answering" in last_reply(settings, mid)
    assert servings(auth_client, mid, 6).status_code == 200                 # serving selector still works
    assert add_ingredients(auth_client, settings, 4, indexes=[0]).status_code == 200   # checklist too
    assert "Cooking Assistant" in auth_client.get(f"/plan/meals/{mid}").text


def test_real_ollama_connection_failure_is_reported(settings):
    from app import ollama
    from dataclasses import replace
    unreachable = replace(settings, ollama_url="http://127.0.0.1:9", ollama_timeout=2)
    with pytest.raises(ollama.AssistantUnavailable):
        ollama.chat(unreachable, [{"role": "user", "content": "hi"}])


# ---- Conversations belong to one planned dinner ----

def test_conversation_resumes_and_stays_separate_per_dinner(auth_client, settings, nuggets):
    plan(auth_client, 1, nuggets)
    plan(auth_client, 4, nuggets)                               # same recipe, two dinners
    tuesday, friday = meal_id(settings, 1), meal_id(settings, 4)
    chat(auth_client, friday, "How many cups are in one US quart?", key="friday-msg-1")
    chat(auth_client, friday, "How many cups are in one US quart?", key="friday-msg-1")   # retried send
    assert [m[0] for m in messages(settings, friday)] == ["user", "assistant"]

    page = auth_client.get(f"/plan/meals/{friday}").text        # reopening after the phone locked
    assert "1 US quart = 4 cups." in page
    assert "1 US quart = 4 cups." not in auth_client.get(f"/plan/meals/{tuesday}").text
    assert messages(settings, tuesday) == []
    log = auth_client.get(f"/plan/meals/{friday}/chat", headers=FETCH).json()["log"]
    assert "1 US quart = 4 cups." in log


def test_conversation_moves_with_the_dinner(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    chat(auth_client, mid, "Make this for 6")
    post(auth_client, f"/plan/{W}/move", {"from_day": 4, "to_day": 5})
    assert meal_id(settings, 5) == mid and len(messages(settings, mid)) == 2
    assert query(settings, "SELECT servings FROM planned_meals WHERE id = ?", (mid,)) == [(6,)]


def test_assistant_needs_sign_in_and_csrf(client, auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    assert auth_client.post(f"/plan/meals/{mid}/chat", data={"message": "hi"}).status_code == 403
    assert messages(settings, mid) == []
    assert auth_client.get("/plan/meals/999").status_code == 404


# ---- One complete flow: recipe view, checklist, list, and assistant agree ----

def test_four_serving_dinner_for_eight_end_to_end(auth_client, settings, nuggets):
    plan(auth_client, 4, nuggets)
    mid = meal_id(settings, 4)
    assert "1 1/2 lbs" in auth_client.get(f"/plan/meals/{mid}").text        # serves 4 as saved

    view = servings(auth_client, mid, 8).json()["recipe"]                     # change to 8
    assert '<span class="ingredient__amount">3 lbs</span>' in view

    html = checklist(auth_client, mid)
    assert '<span class="check-row__amount">3 lbs</span>' in html             # checklist agrees
    add_ingredients(auth_client, settings, 4, indexes=[0])                    # add only the chicken
    assert item(settings, "Chicken breasts")["display_qty"] == "3 lbs"        # list agrees

    chat(auth_client, mid, "How much chicken do I need tonight?")
    assert "3 lbs chicken breasts" in last_reply(settings, mid)               # assistant agrees


# ---- Upgrading a database from before planned dinners had ids ----

def test_old_planned_meals_table_is_upgraded_without_losing_dinners(settings):
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True)
    old = sqlite3.connect(db_path)
    old.executescript("""
        CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL, cuisine TEXT NOT NULL DEFAULT '', prep_minutes INTEGER, cook_minutes INTEGER,
            servings INTEGER, photo_filename TEXT, card_tag TEXT NOT NULL DEFAULT '', last_made TEXT,
            source_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE week_plans (week_start TEXT PRIMARY KEY, plan_version INTEGER NOT NULL DEFAULT 0,
            list_version INTEGER NOT NULL DEFAULT 0, list_signature TEXT, list_built_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE planned_meals (week_start TEXT NOT NULL REFERENCES week_plans(week_start) ON DELETE CASCADE,
            day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE, PRIMARY KEY (week_start, day));
        INSERT INTO recipes (id, name, category, servings) VALUES (3, 'Chili', 'soups', 6);
        INSERT INTO week_plans (week_start) VALUES ('2026-11-09');
        INSERT INTO planned_meals VALUES ('2026-11-09', 2, 3), ('2026-11-09', 5, 3);
    """)
    old.commit()
    old.close()
    for _ in range(2):   # start twice: the upgrade happens once and is safe to repeat
        app = create_app(settings)
        with TestClient(app, follow_redirects=False) as c:
            login(c)
            assert c.get(f"/plan?week={W}").status_code == 200
    assert query(settings, "SELECT id, day, recipe_id, servings FROM planned_meals ORDER BY day") == [
        (1, 2, 3, None), (2, 5, 3, None)]
