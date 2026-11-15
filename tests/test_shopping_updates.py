"""Update Shopping List removes what the plan no longer needs; Clear Purchased hides finished items."""

# sqlite3 builds an "older version" database for the migration test.
import sqlite3

# timedelta picks a different week.
from datetime import timedelta

# pytest provides fixtures.
import pytest

# TestClient opens a second, separate browser session.
from fastapi.testclient import TestClient

# Project modules.
from app.main import create_app
from tests.conftest import login
from tests.plan_helpers import (BURGERS, PARM, TACOS, W, WEEK, add_recipe, all_items, generate, item, plan, post,
                                query, state)
from tests.test_shopping import section_html, set_status


@pytest.fixture
def week(auth_client, settings):
    """Taco Bowls Monday, Burgers Tuesday, Chicken Parmesan Wednesday; list generated."""
    ids = {"tacos": add_recipe(settings, "Taco Bowls", TACOS), "burgers": add_recipe(settings, "Burgers", BURGERS),
           "parm": add_recipe(settings, "Chicken Parmesan", PARM)}
    plan(auth_client, 0, ids["tacos"])
    plan(auth_client, 1, ids["burgers"])
    plan(auth_client, 2, ids["parm"])
    generate(auth_client)
    return ids


def rows(settings, week=WEEK):
    """Every stored row for the week, cleared ones included: {name: (status, cleared)}."""
    return {name: (status, cleared) for name, status, cleared in query(
        settings, "SELECT name, status, cleared FROM shopping_items WHERE week_start = ?", (week.isoformat(),))}


def names(settings, week=WEEK):
    return {i["name"] for i in all_items(settings, week)}


def page(client):
    return client.get(f"/shopping?week={W}").text


def clear(client, fetch=False):
    return post(client, f"/shopping/{W}/clear-purchased", fetch=fetch)


def unplan(client, day):
    post(client, f"/plan/{W}/day/{day}/clear")


# ---- Removing dinners and pressing Update ----

def test_removing_a_recipe_and_updating_removes_its_ingredients(auth_client, settings, week):
    assert "Burger buns" in names(settings)
    unplan(auth_client, 1)   # no more Burgers
    generate(auth_client)
    assert "Burger buns" not in rows(settings)          # gone from the database, not just hidden
    assert query(settings, "SELECT COUNT(*) FROM shopping_item_sources WHERE recipe_name = 'Burgers'") == [(0,)]


def test_removing_a_recipe_without_updating_changes_nothing(auth_client, settings, week):
    before = all_items(settings)
    unplan(auth_client, 1)
    assert all_items(settings) == before
    assert "The meal plan changed since this list was made." in page(auth_client)
    assert "Update Shopping List" in page(auth_client)


def test_shared_ingredient_stays_with_a_smaller_quantity(auth_client, settings, week):
    assert item(settings, "Ground beef")["display_qty"] == "3 lbs"   # Tacos 1 lb + Burgers 2 lbs
    assert item(settings, "Onion")["display_qty"] == "3"
    unplan(auth_client, 1)
    generate(auth_client)
    beef = item(settings, "Ground beef")
    assert beef["display_qty"] == "1 lb" and [s["recipe_name"] for s in beef["sources"]] == ["Taco Bowls"]
    assert item(settings, "Onion")["display_qty"] == "2"


def test_quantity_increases_when_a_dinner_is_added(auth_client, settings, week):
    plan(auth_client, 4, week["tacos"])
    generate(auth_client)
    beef = item(settings, "Ground beef")
    assert beef["display_qty"] == "4 lbs" and beef["status"] == "need"
    assert beef["sources"][0]["times"] == 2


def test_update_message_says_what_was_removed(auth_client, settings, week):
    unplan(auth_client, 1)
    response = generate(auth_client)
    assert "removed=3" in response.headers["location"]   # Burger buns, plus the Salt and Black pepper staples
    assert "3 no longer needed" in auth_client.get(response.headers["location"]).text


# ---- Purchased amounts when the plan changes ----

def test_bought_amount_that_now_falls_short_goes_back_to_need(auth_client, settings, week):
    unplan(auth_client, 2)
    generate(auth_client)
    set_status(auth_client, settings, "Ground beef", "bought")       # bought 3 lbs
    plan(auth_client, 4, week["tacos"])                              # now 4 lbs needed
    generate(auth_client)
    beef = item(settings, "Ground beef")
    assert beef["status"] == "need" and beef["display_qty"] == "4 lbs"
    assert beef["change_note"].startswith("Need more")


def test_bought_amount_that_still_covers_a_smaller_need_stays_bought(auth_client, settings, week):
    set_status(auth_client, settings, "Ground beef", "bought")       # bought 3 lbs
    unplan(auth_client, 1)                                           # now 1 lb
    generate(auth_client)
    assert item(settings, "Ground beef")["status"] == "bought"


def test_going_down_then_back_up_to_what_was_bought_needs_nothing_more(auth_client, settings, week):
    set_status(auth_client, settings, "Ground beef", "bought")       # bought 3 lbs
    unplan(auth_client, 1)
    generate(auth_client)                                            # 1 lb
    plan(auth_client, 1, week["burgers"])
    generate(auth_client)                                            # 3 lbs again: already bought
    beef = item(settings, "Ground beef")
    assert beef["status"] == "bought" and beef["display_qty"] == "3 lbs"
    plan(auth_client, 4, week["tacos"])
    generate(auth_client)                                            # 4 lbs: more than was bought
    assert item(settings, "Ground beef")["status"] == "need"


def test_obsolete_purchased_and_have_items_also_leave(auth_client, settings, week):
    set_status(auth_client, settings, "Burger buns", "bought")
    set_status(auth_client, settings, "Tomatoes", "have")
    unplan(auth_client, 0)
    unplan(auth_client, 1)
    generate(auth_client)
    assert "Burger buns" not in rows(settings) and "Tomatoes" not in rows(settings)


def test_progress_survives_an_update_that_changes_other_items(auth_client, settings, week):
    set_status(auth_client, settings, "Chicken breasts", "bought")
    set_status(auth_client, settings, "Marinara sauce", "have")
    beef_id = item(settings, "Ground beef")["id"]
    unplan(auth_client, 1)
    generate(auth_client)
    assert item(settings, "Chicken breasts")["status"] == "bought"
    assert item(settings, "Marinara sauce")["status"] == "have"
    assert item(settings, "Ground beef")["id"] == beef_id


# ---- Manual groceries ----

def test_manual_items_survive_any_update(auth_client, settings, week):
    for name in ("Milk", "Bananas", "Diapers", "Paper towels"):
        post(auth_client, f"/shopping/{W}/items", {"name": name})
    set_status(auth_client, settings, "Bananas", "bought")
    set_status(auth_client, settings, "Diapers", "have")
    for day in (0, 1, 2):
        unplan(auth_client, day)          # every dinner removed
    generate(auth_client)
    assert rows(settings) == {"Milk": ("need", 0), "Bananas": ("bought", 0), "Diapers": ("have", 0),
                              "Paper towels": ("need", 0)}


def test_manual_item_named_like_a_removed_ingredient_stays(auth_client, settings, week):
    post(auth_client, f"/shopping/{W}/items", {"name": "Burger buns", "quantity": "1 bag"})
    unplan(auth_client, 1)
    generate(auth_client)
    buns = item(settings, "Burger buns")
    assert buns["manual"] and buns["display_qty"] == "1 bag" and buns["sources"] == []


def test_manual_items_still_edit_delete_and_undo_after_updates(auth_client, settings, week):
    post(auth_client, f"/shopping/{W}/items", {"name": "Trash bags"})
    unplan(auth_client, 1)
    generate(auth_client)
    it = item(settings, "Trash bags")
    post(auth_client, f"/shopping/{W}/items/{it['id']}/edit", {"name": "Trash bags", "quantity": "1 box", "rev": it["rev"]})
    assert item(settings, "Trash bags")["display_qty"] == "1 box"
    set_status(auth_client, settings, "Trash bags", "bought")
    set_status(auth_client, settings, "Trash bags", "need")    # undo
    it = item(settings, "Trash bags")
    post(auth_client, f"/shopping/{W}/items/{it['id']}/delete", {"rev": it["rev"]})
    assert "Trash bags" not in rows(settings)


# ---- Kitchen Staples ----

def test_staples_stay_excluded_and_leave_with_their_recipe(auth_client, settings, week):
    assert item(settings, "Butter")["status"] == "staple"
    unplan(auth_client, 0)               # Taco Bowls was the only recipe with butter
    generate(auth_client)
    assert "Butter" not in rows(settings)


def test_a_staple_we_put_on_the_list_is_kept_when_its_recipe_goes(auth_client, settings, week):
    set_status(auth_client, settings, "Butter", "need")    # ran out: Add to list
    unplan(auth_client, 0)
    generate(auth_client)
    butter = item(settings, "Butter")
    assert butter["status"] == "need" and butter["manual"]


def test_a_staple_added_from_the_staples_page_survives_updates(auth_client, settings, week):
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    post(auth_client, f"/staples/{milk}/list", {"week": W})
    for day in (0, 1, 2):
        unplan(auth_client, day)
    generate(auth_client)
    assert item(settings, "Milk")["status"] == "need"


# ---- Clear Purchased ----

def test_purchased_items_leave_need_to_buy(auth_client, settings, week):
    response = set_status(auth_client, settings, "Onion", "bought", fetch=True)
    html = response.json()["html"]
    assert "Onion" not in section_html(html, "need") and "Onion" in section_html(html, "bought")


def test_clear_purchased_hides_only_purchased_items(auth_client, settings, week):
    set_status(auth_client, settings, "Onion", "bought")
    set_status(auth_client, settings, "Ground beef", "bought")
    set_status(auth_client, settings, "Tomatoes", "have")
    need_before = state(settings)["need_count"]
    assert "Clear purchased" in section_html(page(auth_client), "bought")

    response = clear(auth_client, fetch=True)
    assert response.status_code == 200
    html = response.json()["html"]
    assert section_html(html, "bought") == ""                      # the Purchased section is gone
    assert "Tomatoes" in section_html(html, "have")                # Already have is separate
    assert state(settings)["need_count"] == need_before
    assert rows(settings)["Onion"] == ("bought", 1)                # remembered, not deleted


def test_clear_purchased_with_nothing_purchased_changes_nothing(auth_client, settings, week):
    version = state(settings)["version"]
    clear(auth_client)
    assert state(settings)["version"] == version
    assert "Clear purchased" not in page(auth_client)


def test_milk_eggs_bread_example(auth_client, settings):
    """Bought milk and eggs, cleared them, pressed Update with the same meals: only bread is left."""
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    post(auth_client, f"/staples/{milk}/delete", {"week": W})          # milk isn't a staple for this family
    rid = add_recipe(settings, "French Toast", [("1 cup", "milk"), ("4", "eggs"), ("8 slices", "bread")])
    plan(auth_client, 5, rid)
    generate(auth_client)
    set_status(auth_client, settings, "Milk", "bought")
    set_status(auth_client, settings, "Eggs", "bought")
    clear(auth_client)
    assert [i["name"] for i in state(settings)["sections"]["need"]] == ["Bread"]

    for _ in range(2):   # unchanged updates, twice: nothing comes back, nothing is duplicated
        response = generate(auth_client)
        assert "changed=0" in response.headers["location"] and "removed=0" in response.headers["location"]
        assert [i["name"] for i in state(settings)["sections"]["need"]] == ["Bread"]
        assert rows(settings) == {"Milk": ("bought", 1), "Eggs": ("bought", 1), "Bread": ("need", 0)}


def test_cleared_item_comes_back_when_the_plan_needs_more(auth_client, settings, week):
    set_status(auth_client, settings, "Ground beef", "bought")    # 3 lbs
    clear(auth_client)
    plan(auth_client, 4, week["tacos"])                           # 4 lbs
    generate(auth_client)
    beef = item(settings, "Ground beef")
    assert beef["status"] == "need" and not beef["cleared"] and beef["change_note"].startswith("Need more")


def test_cleared_item_stays_hidden_when_the_plan_needs_less(auth_client, settings, week):
    set_status(auth_client, settings, "Ground beef", "bought")
    clear(auth_client)
    unplan(auth_client, 1)
    generate(auth_client)
    assert "Ground beef" not in names(settings) and rows(settings)["Ground beef"] == ("bought", 1)


def test_cleared_item_leaves_for_good_when_its_recipe_goes(auth_client, settings, week):
    set_status(auth_client, settings, "Burger buns", "bought")
    clear(auth_client)
    unplan(auth_client, 1)
    generate(auth_client)
    assert "Burger buns" not in rows(settings)


def test_adding_a_cleared_item_again_puts_it_back_to_buy(auth_client, settings, week):
    post(auth_client, f"/shopping/{W}/items", {"name": "Paper towels"})
    for name in ("Paper towels", "Onion"):
        set_status(auth_client, settings, name, "bought")
    clear(auth_client)

    post(auth_client, f"/shopping/{W}/items", {"name": "Paper towels"})    # we need more
    post(auth_client, f"/shopping/{W}/items", {"name": "Onions"})          # a recipe item, wanted again
    assert item(settings, "Paper towels")["status"] == "need"
    assert item(settings, "Onion")["status"] == "need"
    assert len([k for (k,) in query(settings, "SELECT item_key FROM shopping_items") if k == "paper towel"]) == 1

    set_status(auth_client, settings, "Paper towels", "bought")            # buy it again, clear again
    clear(auth_client)
    assert rows(settings)["Paper towels"] == ("bought", 1)


def test_a_cleared_staple_can_be_added_again_from_the_staples_page(auth_client, settings):
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    post(auth_client, f"/staples/{milk}/list", {"week": W})
    set_status(auth_client, settings, "Milk", "bought")
    clear(auth_client)
    post(auth_client, f"/staples/{milk}/list", {"week": W})
    assert item(settings, "Milk")["status"] == "need"


def test_fully_shopped_list_is_not_shown_as_empty(auth_client, settings):
    post(auth_client, f"/shopping/{W}/items", {"name": "Eggs"})
    set_status(auth_client, settings, "Eggs", "bought")
    clear(auth_client)
    html = page(auth_client)
    assert "Nothing on the list yet" not in html and "Everything's in the cart." in html
    assert "<strong data-need-count>0</strong>" in html


def test_clear_purchased_works_without_javascript(auth_client, settings, week):
    set_status(auth_client, settings, "Onion", "bought")
    response = clear(auth_client)
    assert response.status_code == 303 and response.headers["location"] == f"/shopping?week={W}"


def test_clear_purchased_needs_a_valid_form(auth_client, client, settings, week):
    set_status(auth_client, settings, "Onion", "bought")
    assert post(auth_client, f"/shopping/{W}/clear-purchased", csrf="wrong").status_code == 403
    assert client.post(f"/shopping/{W}/clear-purchased").status_code in (303, 403)
    assert item(settings, "Onion")["status"] == "bought" and not item(settings, "Onion")["cleared"]


# ---- Weeks stay separate ----

def test_clearing_one_week_leaves_other_weeks_alone(auth_client, settings, week):
    next_week = WEEK + timedelta(days=7)
    plan(auth_client, 0, week["tacos"], week=next_week.isoformat())
    generate(auth_client, week=next_week.isoformat())
    set_status(auth_client, settings, "Ground beef", "bought")
    post(auth_client, f"/shopping/{next_week.isoformat()}/items", {"name": "Eggs"})
    clear(auth_client)
    assert rows(settings)["Ground beef"] == ("bought", 1)
    assert rows(settings, next_week)["Ground beef"] == ("need", 0)    # never carried into another week
    generate(auth_client, week=next_week.isoformat())
    assert rows(settings, next_week)["Ground beef"] == ("need", 0)


# ---- Both phones see it ----

@pytest.fixture
def second_phone(auth_client):
    with TestClient(auth_client.app, follow_redirects=False) as other:
        login(other)
        yield other


def test_other_phone_sees_update_and_clear(auth_client, second_phone, settings, week):
    version = second_phone.get(f"/shopping/{W}/live?since=-1").json()["version"]
    unplan(auth_client, 1)
    generate(auth_client)
    data = second_phone.get(f"/shopping/{W}/live?since={version}").json()
    assert "Burger buns" not in data["html"]

    set_status(auth_client, settings, "Onion", "bought")
    clear(auth_client)
    data2 = second_phone.get(f"/shopping/{W}/live?since={data['version']}").json()
    assert data2["version"] > data["version"] and section_html(data2["html"], "bought") == ""
    assert second_phone.get(f"/shopping/{W}/live?since={data2['version']}").status_code == 204


def test_undo_from_an_old_page_after_clearing_is_refused(auth_client, second_phone, settings, week):
    set_status(auth_client, settings, "Onion", "bought")
    old = item(settings, "Onion")                       # the other phone still shows it under Purchased
    clear(auth_client)
    response = post(second_phone, f"/shopping/{W}/items/{old['id']}/status",
                    {"status": "need", "rev": old["rev"]}, fetch=True)
    assert response.status_code == 409
    assert rows(settings)["Onion"] == ("bought", 1)


# ---- Upgrading an existing database ----

def test_items_flagged_by_the_old_version_are_cleaned_up_by_update(auth_client, settings, week):
    unplan(auth_client, 1)
    generate(auth_client)
    # Simulate a row the previous version flagged "no longer in this week's meals".
    conn = sqlite3.connect(settings.database_path)
    tid = add_recipe(settings, "Old Dinner", [("1", "leek")])
    conn.execute("INSERT INTO shopping_items (week_start, item_key, name, stale) VALUES (?, 'leek', 'Leek', 1)", (W,))
    conn.execute("INSERT INTO shopping_item_sources (item_id, recipe_id, recipe_name) VALUES (last_insert_rowid(), ?, 'Old Dinner')",
                 (tid,))
    conn.commit()
    conn.close()
    assert state(settings)["out_of_date"]                          # the Update banner offers to tidy it
    generate(auth_client)
    assert "Leek" not in rows(settings) and not state(settings)["out_of_date"]


def test_new_columns_are_added_to_an_old_shopping_table(settings):
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True)
    old = sqlite3.connect(db_path)
    old.executescript("""
        CREATE TABLE week_plans (week_start TEXT PRIMARY KEY, plan_version INTEGER NOT NULL DEFAULT 0,
            list_version INTEGER NOT NULL DEFAULT 0, list_signature TEXT, list_built_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE shopping_items (id INTEGER PRIMARY KEY AUTOINCREMENT, week_start TEXT NOT NULL, item_key TEXT NOT NULL,
            name TEXT NOT NULL, quantity TEXT NOT NULL DEFAULT '', totals_json TEXT NOT NULL DEFAULT '{}',
            manual INTEGER NOT NULL DEFAULT 0, manual_qty TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'need',
            stale INTEGER NOT NULL DEFAULT 0, change_note TEXT NOT NULL DEFAULT '', rev INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (week_start, item_key));
        INSERT INTO week_plans (week_start, list_signature) VALUES ('2026-11-09', '');
        INSERT INTO shopping_items (week_start, item_key, name, manual, status) VALUES ('2026-11-09', 'banana', 'Bananas', 1, 'bought');
    """)
    old.commit()
    old.close()
    for _ in range(2):   # start twice: safe to repeat
        with TestClient(create_app(settings), follow_redirects=False) as c:
            login(c)
            assert "Bananas" in section_html(c.get(f"/shopping?week={W}").text, "bought")
    assert query(settings, "SELECT name, status, cleared, bought_totals FROM shopping_items") == [("Bananas", "bought", 0, "")]
