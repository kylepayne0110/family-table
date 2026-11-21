# sqlite3 builds an "old version" database for the migration test.
import sqlite3

# pytest provides parametrize.
import pytest

# TestClient runs the app; create_app builds it from test settings.
from fastapi.testclient import TestClient

# Project modules.
from app.main import create_app
from tests.conftest import create_recipe, login, make_image, recipe_id_from
from tests.plan_helpers import FETCH, TACOS, W, add_recipe, all_items, item, plan, post, query, shop_week, token


GET_PAGES = [
    "/plan", f"/plan?week={W}", f"/plan/{W}/live", f"/plan/{W}/day/0/choose",
    "/shopping", f"/shopping?week={W}", f"/shopping/{W}/live", "/staples",
]
POSTS = [
    f"/plan/{W}/day/0", f"/plan/{W}/day/0/clear", f"/plan/{W}/move",
    f"/shopping/{W}/items", f"/shopping/{W}/items/1/status", f"/shopping/{W}/items/1/edit",
    f"/shopping/{W}/items/1/delete", f"/shopping/{W}/items/1/staple", f"/shopping/{W}/clear-purchased",
    "/staples", "/staples/1/edit", "/staples/1/delete", "/staples/1/list",
]


@pytest.mark.parametrize("url", GET_PAGES)
def test_pages_require_sign_in(client, url):
    response = client.get(url)
    assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("url", POSTS)
def test_actions_require_sign_in(client, url):
    response = client.post(url, data={"name": "x", "status": "bought", "recipe_id": "1"})
    assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("url", POSTS)
def test_actions_require_csrf(auth_client, settings, url):
    rid = add_recipe(settings, "Tacos", TACOS)
    plan(auth_client, 0, rid)
    shop_week(auth_client, settings)
    before = (query(settings, "SELECT * FROM planned_meals"), query(settings, "SELECT * FROM shopping_items"),
              query(settings, "SELECT * FROM staples"))
    data = {"name": "Hacked", "status": "bought", "recipe_id": str(rid), "rev": "1", "choice": "remove",
            "from_day": "0", "to_day": "3", "week": W}
    assert auth_client.post(url, data=data).status_code == 403
    assert auth_client.post(url, data={**data, "csrf_token": "wrong"}, headers=FETCH).status_code == 403
    after = (query(settings, "SELECT * FROM planned_meals"), query(settings, "SELECT * FROM shopping_items"),
             query(settings, "SELECT * FROM staples"))
    assert before == after


def test_user_text_is_escaped(auth_client, settings):
    evil = '<script>alert("x")</script>'
    post(auth_client, f"/shopping/{W}/items", {"name": evil, "quantity": "<b>2</b>"})
    page = auth_client.get(f"/shopping?week={W}").text
    assert evil not in page and "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in page
    assert "<b>2</b>" not in page
    live = auth_client.get(f"/shopping/{W}/live", headers=FETCH).json()["html"]
    assert evil not in live

    post(auth_client, "/staples", {"name": evil, "week": W})
    assert evil not in auth_client.get(f"/staples?week={W}").text

    rid = add_recipe(auth_client.app.state.settings, evil, [("1", evil)])
    plan(auth_client, 0, rid)
    assert evil not in auth_client.get(f"/plan?week={W}").text
    shop_week(auth_client, auth_client.app.state.settings)
    assert evil not in auth_client.get(f"/shopping?week={W}").text
    mid = query(auth_client.app.state.settings, "SELECT id FROM planned_meals")[0][0]
    assert evil not in auth_client.get(f"/plan/meals/{mid}").text
    assert evil not in auth_client.get(f"/plan/meals/{mid}/ingredients").text


def test_long_input_is_trimmed(auth_client, settings):
    post(auth_client, f"/shopping/{W}/items", {"name": "x" * 500, "quantity": "9" * 500})
    it = all_items(settings)[0]
    assert len(it["name"]) == 120 and len(it["manual_qty"]) == 40


def test_items_from_another_week_cannot_be_changed(auth_client, settings):
    post(auth_client, f"/shopping/{W}/items", {"name": "Milk"})
    it = item(settings, "Milk")
    response = post(auth_client, f"/shopping/2026-11-16/items/{it['id']}/status",
                    {"status": "bought", "rev": it["rev"]}, fetch=True)
    assert response.status_code == 404
    assert item(settings, "Milk")["status"] == "need"


def test_bad_values_are_rejected(auth_client):
    assert post(auth_client, f"/shopping/{W}/items/1/explode", {}).status_code == 404
    assert post(auth_client, "/staples/1/explode", {"week": W}).status_code == 404
    assert post(auth_client, "/shopping/2026-11-11/items", {"name": "Milk"}).status_code == 404   # not a Monday
    assert auth_client.get("/shopping/abc/live").status_code == 404


def test_navigation_on_signed_in_pages_only(auth_client, client):
    home = auth_client.get("/").text
    assert 'class="app-nav"' in home and 'href="/plan"' in home and 'href="/shopping"' in home
    assert 'aria-current="page"><svg' in home   # Recipes is the active section
    plan_page = auth_client.get("/plan").text
    assert 'class="app-nav__link is-active" href="/plan" aria-current="page"' in plan_page


def test_login_page_has_no_navigation(client):
    assert 'class="app-nav"' not in client.get("/login").text


# ---- Existing features keep working, and existing data is untouched ----

def snapshot(settings):
    return {table: query(settings, f"SELECT * FROM {table} ORDER BY 1, 2")
            for table in ("recipes", "ingredients", "steps", "notes")}


def test_planner_and_list_never_change_recipe_data(auth_client, settings):
    rid = recipe_id_from(create_recipe(auth_client, photo=make_image(), note_author="Mom", note_body="Extra lime."))
    other = add_recipe(settings, "Burgers", [("2 lbs", "ground beef")])
    photo = query(settings, "SELECT photo_filename FROM recipes WHERE id = ?", (rid,))[0][0]
    before = snapshot(settings)

    plan(auth_client, 0, rid)
    plan(auth_client, 1, other)
    post(auth_client, f"/plan/{W}/move", {"from_day": 0, "to_day": 1})
    shop_week(auth_client, settings)
    post(auth_client, f"/shopping/{W}/items", {"name": "Milk"})
    it = item(settings, "Ground beef")
    post(auth_client, f"/shopping/{W}/items/{it['id']}/status", {"status": "bought", "rev": it["rev"]})
    post(auth_client, f"/plan/{W}/day/1/clear")
    post(auth_client, f"/shopping/{W}/clear-purchased")

    assert snapshot(settings) == before
    assert auth_client.get(f"/photos/{photo}").status_code == 200
    detail = auth_client.get(f"/recipes/{rid}").text
    assert "Chicken Tacos" in detail and "Extra lime." in detail


def test_migration_adds_tables_to_an_old_database(tmp_path, settings):
    """A database from before this feature keeps every row; new tables and staples appear once."""
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True)
    old = sqlite3.connect(db_path)
    old.executescript("""
        CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL, cuisine TEXT NOT NULL DEFAULT '', prep_minutes INTEGER, cook_minutes INTEGER,
            servings INTEGER, photo_filename TEXT, card_tag TEXT NOT NULL DEFAULT '', last_made TEXT,
            source_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE ingredients (id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, amount TEXT NOT NULL DEFAULT '', name TEXT NOT NULL);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO recipes (id, name, category) VALUES (7, 'Grandma''s Chili', 'soups');
        INSERT INTO ingredients (recipe_id, position, amount, name) VALUES (7, 0, '1 lb', 'ground beef');
        INSERT INTO notes (recipe_id, author, body) VALUES (7, 'Dad', 'Double the beans.');
    """)
    old.commit()
    old.close()

    for _ in range(2):   # start the app twice: nothing is duplicated
        with TestClient(create_app(settings), follow_redirects=False) as c:
            login(c)
            assert "Grandma&#39;s Chili" in c.get("/recipes/7").text
    assert query(settings, "SELECT id, name FROM recipes") == [(7, "Grandma's Chili")]
    assert query(settings, "SELECT body FROM notes") == [("Double the beans.",)]
    assert query(settings, "SELECT COUNT(*) FROM staples") == [(9,)]
    tables = {r[0] for r in query(settings, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"week_plans", "planned_meals", "shopping_items", "shopping_item_sources", "staples", "app_meta"} <= tables


def test_csrf_token_comes_from_session(auth_client):
    assert token(auth_client)
