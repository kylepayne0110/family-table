"""Clear Purchased hides finished items; re-adding a dinner never brings back what was already bought."""

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
from tests.plan_helpers import (BURGERS, PARM, TACOS, W, WEEK, add_ingredients, add_recipe, all_items, item, plan,
                                post, query, shop_week, state)
from tests.test_shopping import section_html, set_status


@pytest.fixture
def week(auth_client, settings):
    """Taco Bowls Monday, Burgers Tuesday, Chicken Parmesan Wednesday; each dinner's ingredients added."""
    ids = {"tacos": add_recipe(settings, "Taco Bowls", TACOS), "burgers": add_recipe(settings, "Burgers", BURGERS),
           "parm": add_recipe(settings, "Chicken Parmesan", PARM)}
    plan(auth_client, 0, ids["tacos"])
    plan(auth_client, 1, ids["burgers"])
    plan(auth_client, 2, ids["parm"])
    shop_week(auth_client, settings)
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


# ---- Unplanning a dinner never touches the list ----

def test_unplanning_a_dinner_keeps_its_groceries_and_progress(auth_client, settings, week):
    set_status(auth_client, settings, "Chicken breasts", "bought")
    for name in ("Milk", "Paper towels"):
        post(auth_client, f"/shopping/{W}/items", {"name": name})
    before = all_items(settings)
    for day in (0, 1, 2):
        unplan(auth_client, day)
    assert all_items(settings) == before                        # same items, ids, statuses
    assert item(settings, "Chicken breasts")["status"] == "bought"


def test_a_staple_added_from_the_staples_page_stays(auth_client, settings, week):
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    post(auth_client, f"/staples/{milk}/list", {"week": W})
    add_ingredients(auth_client, settings, 0, indexes=[0, 1, 2, 3])
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
    """Bought milk and eggs, cleared them, added the same dinner again: only bread is left."""
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    post(auth_client, f"/staples/{milk}/delete", {"week": W})          # milk isn't a staple for this family
    rid = add_recipe(settings, "French Toast", [("1 cup", "milk"), ("4", "eggs"), ("8 slices", "bread")])
    plan(auth_client, 5, rid)
    add_ingredients(auth_client, settings, 5)
    set_status(auth_client, settings, "Milk", "bought")
    set_status(auth_client, settings, "Eggs", "bought")
    clear(auth_client)
    assert [i["name"] for i in state(settings)["sections"]["need"]] == ["Bread"]

    for _ in range(2):   # the same dinner added again, twice: nothing comes back, nothing is duplicated
        assert add_ingredients(auth_client, settings, 5, indexes=[0, 1, 2]).status_code == 200
        assert [i["name"] for i in state(settings)["sections"]["need"]] == ["Bread"]
        assert rows(settings) == {"Milk": ("bought", 1), "Eggs": ("bought", 1), "Bread": ("need", 0)}


def test_cleared_item_comes_back_when_the_plan_needs_more(auth_client, settings, week):
    set_status(auth_client, settings, "Ground beef", "bought")    # 3 lbs
    clear(auth_client)
    plan(auth_client, 4, week["tacos"])                           # another Taco Bowls: 4 lbs
    add_ingredients(auth_client, settings, 4)
    beef = item(settings, "Ground beef")
    assert beef["status"] == "need" and not beef["cleared"] and beef["change_note"].startswith("Need more")


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
    add_ingredients(auth_client, settings, 0, week=next_week.isoformat())
    set_status(auth_client, settings, "Ground beef", "bought")
    post(auth_client, f"/shopping/{next_week.isoformat()}/items", {"name": "Eggs"})
    clear(auth_client)
    assert rows(settings)["Ground beef"] == ("bought", 1)
    assert rows(settings, next_week)["Ground beef"] == ("need", 0)    # never carried into another week


# ---- Both phones see it ----

@pytest.fixture
def second_phone(auth_client):
    with TestClient(auth_client.app, follow_redirects=False) as other:
        login(other)
        yield other


def test_other_phone_sees_added_ingredients_and_clear(auth_client, second_phone, settings, week):
    version = second_phone.get(f"/shopping/{W}/live?since=-1").json()["version"]
    add_ingredients(auth_client, settings, 0, indexes=[4])            # butter from Taco Bowls
    data = second_phone.get(f"/shopping/{W}/live?since={version}").json()
    assert "Butter" in section_html(data["html"], "need")

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
