# re pulls pieces out of rendered pages.
import re

# threading simulates two phones changing the list at the same moment.
import threading

# timedelta picks a different week.
from datetime import timedelta

# pytest provides fixtures.
import pytest

# TestClient opens a second, separate browser session.
from fastapi.testclient import TestClient

# Project modules for direct database checks.
from app import shopping
from app.database import connect

# Shared helpers.
from tests.conftest import login
from tests.plan_helpers import (BURGERS, FETCH, PARM, TACOS, W, WEEK, add_recipe, all_items, generate, item,
                                plan, post, query, state, token)


def section_html(html: str, label: str) -> str:
    """The <ul> for Need to Buy, or the <details> for a collapsed section."""
    if label == "need":
        return re.search(r'data-section="need".*?</ul>', html, re.S).group(0)
    match = re.search(rf'<details class="shop-section" data-keep-open="{label}">.*?</details>', html, re.S)
    return match.group(0) if match else ""


def status_of(settings, name):
    return item(settings, name)["status"]


def set_status(client, settings, name, status, fetch=False, rev=None):
    it = item(settings, name)
    data = {"status": status, "rev": it["rev"] if rev is None else rev}
    return post(client, f"/shopping/{W}/items/{it['id']}/status", data, fetch=fetch)


@pytest.fixture
def week_of_tacos(auth_client, settings):
    """Taco Bowls Monday, Burgers Wednesday, list generated."""
    ids = {"tacos": add_recipe(settings, "Taco Bowls", TACOS), "burgers": add_recipe(settings, "Burgers", BURGERS),
           "parm": add_recipe(settings, "Chicken Parmesan", PARM)}
    plan(auth_client, 0, ids["tacos"])
    plan(auth_client, 2, ids["burgers"])
    generate(auth_client)
    return ids


# ---- Generating a list ----

def test_generate_gathers_and_combines_ingredients(auth_client, settings, week_of_tacos):
    names = sorted(i["name"] for i in all_items(settings))
    assert names == ["Black pepper", "Burger buns", "Butter", "Corn tortillas", "Ground beef", "Onion", "Salt", "Tomatoes"]
    assert item(settings, "Ground beef")["display_qty"] == "3 lbs"
    assert item(settings, "Onion")["display_qty"] == "2"


def test_generate_redirects_with_summary(auth_client, settings):
    rid = add_recipe(settings, "Taco Bowls", TACOS)
    plan(auth_client, 0, rid)
    response = generate(auth_client)
    assert response.status_code == 303
    page = auth_client.get(response.headers["location"]).text
    assert "Shopping list ready: 4 to buy, 1 kitchen staple set aside." in page


def test_sources_are_kept_and_linked(auth_client, settings, week_of_tacos):
    beef = item(settings, "Ground beef")
    assert [(s["recipe_name"], s["amount"]) for s in beef["sources"]] == [("Taco Bowls", "1 lb"), ("Burgers", "2 lbs")]
    page = auth_client.get(f"/shopping?week={W}").text
    assert f'<a href="/recipes/{week_of_tacos["tacos"]}">Taco Bowls</a> (1 lb)' in page
    assert f'<a href="/recipes/{week_of_tacos["burgers"]}">Burgers</a> (2 lbs)' in page


def test_unit_conversion_through_the_whole_flow(auth_client, settings):
    a = add_recipe(settings, "Chili", [("16 oz", "ground beef"), ("1 tsp", "cumin")])
    b = add_recipe(settings, "Meatloaf", [("1 lb", "lean ground beef"), ("2 teaspoons", "ground cumin")])
    plan(auth_client, 0, a)
    plan(auth_client, 1, b)
    generate(auth_client)
    assert item(settings, "Ground beef")["display_qty"] == "2 lbs"
    assert item(settings, "Cumin")["display_qty"] == "1 tbsp"


def test_unknown_quantities_and_different_ingredients(auth_client, settings):
    rid = add_recipe(settings, "Garlic Bread", [("", "fresh parsley"), ("3 cloves", "garlic"), ("1 tsp", "garlic powder")])
    plan(auth_client, 0, rid)
    generate(auth_client)
    assert item(settings, "Parsley")["display_qty"] == ""          # not invented
    assert item(settings, "Garlic")["display_qty"] == "3 cloves"
    assert item(settings, "Garlic powder")["status"] == "staple"   # separate item, and a default staple


def test_staples_are_excluded_into_their_own_section(auth_client, settings, week_of_tacos):
    assert status_of(settings, "Butter") == "staple"
    assert status_of(settings, "Salt") == "staple"
    page = auth_client.get(f"/shopping?week={W}").text
    assert "Butter" not in section_html(page, "need")
    staples = section_html(page, "staple")
    assert "Kitchen Staples <span>(3 excluded)</span>" in staples
    assert "Butter" in staples and "Black pepper" in staples
    assert "Ground beef" in section_html(page, "need")


def test_empty_plan_generates_nothing(auth_client, settings):
    response = generate(auth_client)
    assert response.headers["location"] == f"/plan?week={W}&msg=empty"
    assert "Nothing to shop for yet" in auth_client.get(response.headers["location"]).text
    assert query(settings, "SELECT COUNT(*) FROM shopping_items") == [(0,)]


def test_empty_week_shows_helpful_list_page(auth_client):
    page = auth_client.get(f"/shopping?week={W}").text
    assert "Nothing on the list yet" in page and "Add an item..." in page


# ---- Kitchen Staples ----

def test_default_staples_are_seeded_once(auth_client, settings):
    names = [r[0] for r in query(settings, "SELECT name FROM staples ORDER BY id")]
    assert names == ["Salt", "Black pepper", "Garlic powder", "Onion powder", "Paprika", "Chili powder",
                     "Olive oil", "Butter", "Milk"]
    # A deleted default does not come back on the next startup.
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    post(auth_client, f"/staples/{milk}/delete", {"week": W})
    from app.database import init_db
    init_db(settings.database_path)
    assert "Milk" not in [r[0] for r in query(settings, "SELECT name FROM staples")]


def test_add_rename_and_delete_staples(auth_client, settings):
    response = post(auth_client, "/staples", {"name": "Cumin", "week": W})
    assert response.headers["location"] == f"/staples?week={W}&msg=added"
    page = auth_client.get(response.headers["location"]).text
    assert "Cumin" in page and "Staple added." in page
    sid = query(settings, "SELECT id FROM staples WHERE name = 'Cumin'")[0][0]

    post(auth_client, f"/staples/{sid}/edit", {"name": "Ground cumin", "week": W})
    assert query(settings, "SELECT name FROM staples WHERE id = ?", (sid,)) == [("Ground cumin",)]
    post(auth_client, f"/staples/{sid}/delete", {"week": W})
    assert query(settings, "SELECT COUNT(*) FROM staples WHERE id = ?", (sid,)) == [(0,)]


def test_duplicate_staples_are_refused(auth_client, settings):
    response = post(auth_client, "/staples", {"name": "butter", "week": W})
    assert response.status_code == 422 and "already a staple" in response.text
    response = post(auth_client, "/staples", {"name": "   ", "week": W})
    assert response.status_code == 422
    salt = query(settings, "SELECT id FROM staples WHERE name = 'Salt'")[0][0]
    response = post(auth_client, f"/staples/{salt}/edit", {"name": "Butter", "week": W})
    assert response.status_code == 422


def test_staple_search(auth_client):
    page = auth_client.get(f"/staples?week={W}&q=powder").text
    assert "Garlic powder" in page and "Onion powder" in page and "Paprika" not in page


def test_new_staples_are_excluded_next_time(auth_client, settings):
    post(auth_client, "/staples", {"name": "Corn tortillas", "week": W})
    rid = add_recipe(settings, "Taco Bowls", TACOS)
    plan(auth_client, 0, rid)
    generate(auth_client)
    assert status_of(settings, "Corn tortillas") == "staple"


def test_adding_an_excluded_staple_back_keeps_it_a_staple(auth_client, settings, week_of_tacos):
    assert set_status(auth_client, settings, "Butter", "need").status_code == 303
    page = auth_client.get(f"/shopping?week={W}").text
    assert "Butter" in section_html(page, "need")
    assert ("Butter",) in query(settings, "SELECT name FROM staples")    # still a permanent staple


def test_add_staple_to_list_from_the_staples_page(auth_client, settings):
    milk = query(settings, "SELECT id FROM staples WHERE name = 'Milk'")[0][0]
    response = post(auth_client, f"/staples/{milk}/list", {"week": W})
    assert response.headers["location"] == f"/staples?week={W}&msg=listed"
    post(auth_client, f"/staples/{milk}/list", {"week": W})               # twice: still one entry
    milk_items = [i for i in all_items(settings) if i["name"] == "Milk"]
    assert len(milk_items) == 1 and milk_items[0]["status"] == "need" and milk_items[0]["manual"]
    assert ("Milk",) in query(settings, "SELECT name FROM staples")


def test_staple_from_page_matches_existing_recipe_item(auth_client, settings, week_of_tacos):
    butter = query(settings, "SELECT id FROM staples WHERE name = 'Butter'")[0][0]
    post(auth_client, f"/staples/{butter}/list", {"week": W})
    butter_items = [i for i in all_items(settings) if "butter" in i["item_key"]]
    assert len(butter_items) == 1 and butter_items[0]["status"] == "need"


def test_mark_grocery_as_permanent_staple(auth_client, settings, week_of_tacos):
    it = item(settings, "Corn tortillas")
    post(auth_client, f"/shopping/{W}/items/{it['id']}/staple", {"rev": it["rev"]})
    assert status_of(settings, "Corn tortillas") == "staple"
    assert ("Corn tortillas",) in query(settings, "SELECT name FROM staples")


def test_manual_add_does_not_duplicate_existing_items(auth_client, settings, week_of_tacos):
    post(auth_client, f"/shopping/{W}/items", {"name": "Onions", "quantity": "1 bag"})
    onions = [i for i in all_items(settings) if i["item_key"] == "onion"]
    assert len(onions) == 1
    assert onions[0]["manual"] == 1 and onions[0]["display_qty"] == "2 + 1 bag"


# ---- Shopping ----

def test_add_manual_items(auth_client, settings):
    assert post(auth_client, f"/shopping/{W}/items", {"name": "Paper towels"}).status_code == 303
    response = post(auth_client, f"/shopping/{W}/items", {"name": "Bananas", "quantity": "6"}, fetch=True)
    assert response.status_code == 200
    html = response.json()["html"]
    assert "Bananas" in html and "Paper towels" in html and "Added by hand" in html
    assert item(settings, "Bananas")["display_qty"] == "6"


def test_blank_manual_item_is_refused(auth_client):
    response = post(auth_client, f"/shopping/{W}/items", {"name": "  "}, fetch=True)
    assert response.status_code == 422 and response.json()["error"] == "Type what you need to buy."


def test_edit_and_delete_manual_items(auth_client, settings):
    post(auth_client, f"/shopping/{W}/items", {"name": "Trash bags"})
    it = item(settings, "Trash bags")
    post(auth_client, f"/shopping/{W}/items/{it['id']}/edit", {"name": "Tall trash bags", "quantity": "1 box", "rev": it["rev"]})
    edited = item(settings, "Tall trash bags")
    assert edited["display_qty"] == "1 box" and edited["id"] == it["id"]
    post(auth_client, f"/shopping/{W}/items/{it['id']}/delete", {"rev": edited["rev"]})
    assert all_items(settings) == []


def test_recipe_items_cannot_be_deleted(auth_client, settings, week_of_tacos):
    it = item(settings, "Ground beef")
    response = post(auth_client, f"/shopping/{W}/items/{it['id']}/delete", {"rev": it["rev"]}, fetch=True)
    assert response.status_code == 422 and "Already have" in response.json()["error"]


def test_purchase_moves_item_out_of_need_to_buy(auth_client, settings, week_of_tacos):
    response = set_status(auth_client, settings, "Ground beef", "bought", fetch=True)
    assert response.status_code == 200
    html = response.json()["html"]
    assert "Ground beef" not in section_html(html, "need")
    assert "Ground beef" in section_html(html, "bought")
    assert status_of(settings, "Ground beef") == "bought"


def test_undo_a_purchase(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Onion", "bought")
    set_status(auth_client, settings, "Onion", "need")
    page = auth_client.get(f"/shopping?week={W}").text
    assert "Onion" in section_html(page, "need") and section_html(page, "bought") == ""


def test_already_have_and_back(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Tomatoes", "have")
    page = auth_client.get(f"/shopping?week={W}").text
    assert "Tomatoes" in section_html(page, "have") and "Tomatoes" not in section_html(page, "need")
    assert ("Tomatoes",) not in query(settings, "SELECT name FROM staples")   # not made a staple
    set_status(auth_client, settings, "Tomatoes", "need")
    assert status_of(settings, "Tomatoes") == "need"


def test_counts_update(auth_client, settings, week_of_tacos):
    page = auth_client.get(f"/shopping?week={W}").text
    assert '<strong data-need-count>5</strong> <span data-need-label>items</span> left to buy' in page
    for name in ("Ground beef", "Onion", "Tomatoes", "Corn tortillas"):
        set_status(auth_client, settings, name, "bought")
    page = auth_client.get(f"/shopping?week={W}").text
    assert '<strong data-need-count>1</strong> <span data-need-label>item</span> left to buy' in page
    assert "4 of 5 purchased" in page


def test_checkmarks_survive_a_reload(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Burger buns", "bought")
    for _ in range(2):
        page = auth_client.get(f"/shopping?week={W}").text
        assert 'aria-checked="true" aria-label="Burger buns: purchased.' in page


def test_invalid_status_is_refused(auth_client, settings, week_of_tacos):
    assert set_status(auth_client, settings, "Onion", "eaten", fetch=True).status_code == 422
    assert set_status(auth_client, settings, "Onion", "staple", fetch=True).status_code == 422


# ---- Updating after the plan changes ----

def test_update_after_swapping_a_meal(auth_client, settings, week_of_tacos):
    """We planned Taco Bowls, then switched Monday to Chicken Parmesan."""
    post(auth_client, f"/shopping/{W}/items", {"name": "Paper towels"})
    set_status(auth_client, settings, "Ground beef", "bought")
    beef_id = item(settings, "Ground beef")["id"]

    plan(auth_client, 0, week_of_tacos["parm"])
    assert "The meal plan changed" in auth_client.get(f"/shopping?week={W}").text
    response = generate(auth_client)
    assert "built=update" in response.headers["location"]

    # New ingredients arrive; ones only Taco Bowls needed wait for confirmation.
    assert item(settings, "Chicken breasts")["status"] == "need"
    stale = sorted(i["name"] for i in all_items(settings) if i["stale"])
    assert stale == ["Corn tortillas", "Tomatoes"]
    page = auth_client.get(f"/shopping?week={W}").text
    assert "2 items are no longer in this week's meals" in page
    assert "Corn tortillas, Tomatoes" in page

    # Kept: purchase state, hand-added item, stable ids. Quantity updated (only Burgers now).
    beef = item(settings, "Ground beef")
    assert beef["id"] == beef_id and beef["status"] == "bought" and beef["display_qty"] == "2 lbs"
    assert item(settings, "Paper towels")["status"] == "need"
    assert item(settings, "Onion")["display_qty"] == "2"   # Burgers + Chicken Parmesan

    post(auth_client, f"/shopping/{W}/stale", {"choice": "remove"})
    names = {i["name"] for i in all_items(settings)}
    assert "Tomatoes" not in names and "Corn tortillas" not in names and "Paper towels" in names


def test_keep_items_no_longer_needed(auth_client, settings, week_of_tacos):
    post(auth_client, f"/plan/{W}/day/0/clear")
    generate(auth_client)
    post(auth_client, f"/shopping/{W}/stale", {"choice": "keep"})
    tortillas = item(settings, "Corn tortillas")
    assert tortillas["stale"] == 0 and tortillas["manual"] == 1 and tortillas["manual_qty"] == "8"
    assert tortillas["sources"] == []
    # Keeping them doesn't break a later update.
    generate(auth_client)
    assert item(settings, "Corn tortillas")["manual"] == 1


def test_regenerating_an_unchanged_plan_changes_nothing(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Onion", "bought")
    set_status(auth_client, settings, "Tomatoes", "have")
    before = all_items(settings)
    version = state(settings)["version"]
    response = generate(auth_client)
    assert "changed=0" in response.headers["location"]
    assert all_items(settings) == before          # same ids, revs, statuses, no duplicates
    assert state(settings)["version"] == version
    assert "Nothing changed." in auth_client.get(response.headers["location"]).text


def test_purchased_item_needing_more_goes_back_with_a_note(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Ground beef", "bought")
    plan(auth_client, 4, week_of_tacos["tacos"])      # Taco Bowls a second time
    generate(auth_client)
    beef = item(settings, "Ground beef")
    assert beef["status"] == "need" and beef["display_qty"] == "4 lbs"
    assert beef["change_note"] == "Need more: was 3 lbs, now 4 lbs"
    assert "Need more: was 3 lbs, now 4 lbs" in auth_client.get(f"/shopping?week={W}").text


def test_purchased_item_needing_less_stays_purchased(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Ground beef", "bought")
    post(auth_client, f"/plan/{W}/day/2/clear")      # drop Burgers
    generate(auth_client)
    beef = item(settings, "Ground beef")
    assert beef["status"] == "bought" and beef["change_note"] == "Was 3 lbs, now 1 lb"


def test_staple_overrides_survive_updates(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Butter", "need")
    plan(auth_client, 4, week_of_tacos["parm"])
    generate(auth_client)
    assert status_of(settings, "Butter") == "need"


def test_hand_added_item_that_matches_a_removed_recipe_item_stays(auth_client, settings, week_of_tacos):
    post(auth_client, f"/shopping/{W}/items", {"name": "Corn tortillas", "quantity": "1 pack"})
    post(auth_client, f"/plan/{W}/day/0/clear")
    generate(auth_client)
    tortillas = item(settings, "Corn tortillas")
    assert not tortillas["stale"] and tortillas["display_qty"] == "1 pack"


def test_weeks_have_separate_lists(auth_client, settings, week_of_tacos):
    set_status(auth_client, settings, "Ground beef", "bought")
    next_week = WEEK + timedelta(days=7)
    plan(auth_client, 0, week_of_tacos["tacos"], week=next_week.isoformat())
    generate(auth_client, week=next_week.isoformat())
    assert item(settings, "Ground beef", week=next_week)["status"] == "need"   # purchases don't carry over
    assert item(settings, "Ground beef")["status"] == "bought"                  # last week kept
    assert "Ground beef" in auth_client.get(f"/shopping?week={W}").text


# ---- Sharing between two phones ----

@pytest.fixture
def second_phone(auth_client):
    with TestClient(auth_client.app, follow_redirects=False) as other:
        login(other)
        yield other


def test_changes_reach_the_other_phone(auth_client, second_phone, settings, week_of_tacos):
    version = auth_client.get(f"/shopping/{W}/live?since=-1").json()["version"]
    assert auth_client.get(f"/shopping/{W}/live?since={version}").status_code == 204

    post(second_phone, f"/shopping/{W}/items", {"name": "Milk"}, fetch=True)   # wife adds milk
    data = auth_client.get(f"/shopping/{W}/live?since={version}").json()
    assert data["version"] > version and "Milk" in section_html(data["html"], "need")

    set_status(auth_client, settings, "Ground beef", "bought")                  # I buy the beef
    data2 = second_phone.get(f"/shopping/{W}/live?since={data['version']}").json()
    assert "Ground beef" in section_html(data2["html"], "bought")


def test_stale_page_cannot_overwrite_newer_change(auth_client, second_phone, settings, week_of_tacos):
    onion = item(settings, "Onion")
    set_status(second_phone, settings, "Onion", "have")
    response = set_status(auth_client, settings, "Onion", "bought", fetch=True, rev=onion["rev"])
    assert response.status_code == 409
    assert "Onion" in section_html(response.json()["html"], "have")   # the client gets the truth
    assert status_of(settings, "Onion") == "have"


def test_both_phones_doing_the_same_thing_is_fine(auth_client, second_phone, settings, week_of_tacos):
    rev = item(settings, "Onion")["rev"]
    assert set_status(auth_client, settings, "Onion", "bought", fetch=True, rev=rev).status_code == 200
    assert set_status(second_phone, settings, "Onion", "bought", fetch=True, rev=rev).status_code == 200
    assert status_of(settings, "Onion") == "bought"


def test_stale_edit_is_refused(auth_client, second_phone, settings):
    post(auth_client, f"/shopping/{W}/items", {"name": "Diapers"})
    it = item(settings, "Diapers")
    post(second_phone, f"/shopping/{W}/items/{it['id']}/edit", {"name": "Diapers", "quantity": "2 packs", "rev": it["rev"]})
    response = post(auth_client, f"/shopping/{W}/items/{it['id']}/edit",
                    {"name": "Diapers", "quantity": "1 pack", "rev": it["rev"]}, fetch=True)
    assert response.status_code == 409
    assert item(settings, "Diapers")["display_qty"] == "2 packs"


def test_deleted_item_reports_missing(auth_client, second_phone, settings):
    post(auth_client, f"/shopping/{W}/items", {"name": "Bananas"})
    it = item(settings, "Bananas")
    post(second_phone, f"/shopping/{W}/items/{it['id']}/delete", {"rev": it["rev"]})
    response = post(auth_client, f"/shopping/{W}/items/{it['id']}/status", {"status": "bought", "rev": it["rev"]}, fetch=True)
    assert response.status_code == 404 and "html" in response.json()


def test_concurrent_changes_are_all_saved(auth_client, settings, week_of_tacos):
    """Many simultaneous writes (two phones tapping fast) lose nothing and create no duplicates."""
    errors = []

    def worker(n):
        conn = connect(settings.database_path)
        try:
            shopping.add_item(conn, WEEK, f"Thing {n}")
            shopping.add_item(conn, WEEK, "Milk")          # everyone adds milk
            shopping.generate_list(conn, WEEK)             # and presses Update
        except Exception as exc:  # noqa: BLE001 - collect anything for the assertion
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    keys = [r[0] for r in query(settings, "SELECT item_key FROM shopping_items WHERE week_start = ?", (W,))]
    assert len(keys) == len(set(keys))
    assert sum(1 for k in keys if k.startswith("thing")) == 12 and keys.count("milk") == 1
    assert item(settings, "Ground beef")["display_qty"] == "3 lbs"


def test_list_version_increases_on_every_change(auth_client, settings, week_of_tacos):
    v1 = state(settings)["version"]
    post(auth_client, f"/shopping/{W}/items", {"name": "Eggs"})
    set_status(auth_client, settings, "Eggs", "bought")
    assert state(settings)["version"] == v1 + 2


def test_token_helper_is_per_session(auth_client, second_phone):
    assert token(auth_client) != token(second_phone)
