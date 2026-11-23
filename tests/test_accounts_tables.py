"""Individual accounts, shared Tables, invites, removal, switching, and keeping Tables private."""

# re reads links out of pages; sqlite3 builds a legacy database.
import re
import sqlite3

# pytest provides fixtures.
import pytest

# TestClient drives a second person's browser against the same app.
from fastapi.testclient import TestClient

# Project modules.
from app.database import connect
from app.main import create_app
from tests.conftest import PASSWORD, USERNAME, csrf_from, login, make_image
from tests.plan_helpers import FETCH, W, add_recipe, meal_id, plan, query, table_id

NEW_PASSWORD = "a long kitchen password"


def browser(client) -> TestClient:
    """Another phone: same server and database, its own cookies."""
    return TestClient(client.app, follow_redirects=False)


def signup_page_token(c, url="/signup") -> str:
    return csrf_from(c.get(url).text)


def signup(c, username, name, password=NEW_PASSWORD, invite=""):
    token = signup_page_token(c, f"/signup?invite={invite}" if invite else "/signup")
    return c.post("/signup", data={"username": username, "display_name": name, "password": password,
                                   "csrf_token": token, "invite": invite})


def form_post(c, url, data=None, page="/", fetch=False):
    token = csrf_from(c.get(page).text)
    return c.post(url, data={**(data or {}), "csrf_token": token}, headers=FETCH if fetch else {})


def make_invite(c) -> str:
    html = form_post(c, "/table/invite", {"table_id": ""}, page="/table").text
    match = re.search(r'value="https?://[^"]*/invite/([^"]+)"', html)
    assert match, "no invite link on the page"
    return match.group(1)


def rename_my_name(c, name):
    form_post(c, "/account/name", {"display_name": name}, page="/account")


@pytest.fixture
def kyle(auth_client):
    """The migrated family login, renamed to Kyle, with its Table called Payne's Table."""
    rename_my_name(auth_client, "Kyle")
    form_post(auth_client, "/table/rename", {"name": "Payne's Table"}, page="/table")
    return auth_client


# ---- Signing up and a first Table ----

def test_new_person_creates_a_table_and_starts_using_it(client):
    response = signup(client, "nat", "Nat")
    assert response.status_code == 303 and response.headers["location"] == "/welcome"
    welcome = client.get("/welcome").text
    assert "A Table is where people share" in welcome
    assert 'value="My Table"' in welcome
    response = form_post(client, "/tables/new", {"name": "My Table"}, page="/welcome")
    assert response.status_code == 303
    plan_page = client.get("/plan").text
    assert "My Table" in plan_page
    # One Table: no switcher, the name links to settings.
    assert "data-table-switch" not in plan_page
    assert 'href="/table"' in plan_page
    # A new Table starts with the default Kitchen Staples.
    assert "Butter" in client.get("/staples").text


def test_signup_validates_and_does_not_reveal_more_than_needed(client):
    assert signup(client, "x", "Nat").status_code == 422                       # username too short
    assert "at least 10" in signup(client, "nat", "Nat", password="short").text
    assert signup(client, "nat", "Nat").status_code == 303
    client.cookies.clear()
    taken = signup(client, "NAT", "Someone")
    assert taken.status_code == 422 and "taken" in taken.text


def test_signup_needs_csrf(client):
    client.get("/signup")
    response = client.post("/signup", data={"username": "nat", "display_name": "Nat", "password": NEW_PASSWORD,
                                            "csrf_token": "forged"})
    assert response.status_code == 400
    assert query_users(client) == [USERNAME]


def query_users(client):
    conn = connect(client.app.state.settings.database_path)
    try:
        return [r[0] for r in conn.execute("SELECT username FROM users ORDER BY id")]
    finally:
        conn.close()


def test_unknown_username_and_wrong_password_look_the_same(client):
    a, b = login(client, username="nobody-here"), login(client, password="wrong password")
    assert a.status_code == b.status_code == 401
    assert a.text.count("don&#39;t match") == b.text.count("don&#39;t match") == 1


# ---- Invites ----

def test_invite_signup_lands_directly_in_the_table(kyle, settings):
    add_recipe(settings, "Grandma's Chili", [("1 lb", "beef")])
    token = make_invite(kyle)
    nat = browser(kyle)
    page = nat.get(f"/invite/{token}")
    assert page.status_code == 200
    assert "Kyle invited you to Payne&#39;s Table" in page.text
    # The invite survives the sign-up form and joins right away.
    signup_form = nat.get(f"/signup?invite={token}").text
    assert "Kyle invited you to Payne&#39;s Table" in signup_form
    response = signup(nat, "nat", "Nat", invite=token)
    assert response.status_code == 303 and response.headers["location"] == "/?msg=joined"
    home = nat.get("/?msg=joined").text
    assert "Grandma&#39;s Chili" in home and "Payne&#39;s Table" in home
    assert "Nat" in kyle.get("/table").text


def test_invite_survives_signing_in_to_an_existing_account(kyle):
    nat = browser(kyle)
    signup(nat, "nat", "Nat")
    form_post(nat, "/tables/new", {"name": "Nat's Table"}, page="/welcome")
    nat.cookies.clear()
    token = make_invite(kyle)
    login_page = nat.get(f"/login?invite={token}").text
    assert "Kyle invited you to Payne&#39;s Table" in login_page
    response = nat.post("/login", data={"username": "nat", "password": NEW_PASSWORD, "invite": token,
                                        "csrf_token": csrf_from(login_page)})
    assert response.headers["location"] == "/?msg=joined"
    # Now in two Tables, so the header offers the switcher with the current one checked.
    page = nat.get("/").text
    assert "data-table-switch" in page
    assert re.search(r'is-current"\s+aria-current="true">.*?Payne&#39;s Table', page, re.S)


def test_accepting_twice_and_reusing_a_link(kyle):
    token = make_invite(kyle)
    nat, sam = browser(kyle), browser(kyle)
    signup(nat, "nat", "Nat", invite=token)
    # Nat taps the link again: harmless, she's simply a member.
    assert "already a member" in nat.get(f"/invite/{token}").text
    again = form_post(nat, f"/invite/{token}", page="/")
    assert again.status_code == 303
    assert query(kyle.app.state.settings, "SELECT COUNT(*) FROM memberships m JOIN users u ON u.id = m.user_id "
                                          "WHERE u.username = 'nat'") == [(1,)]
    # Someone else can't use Nat's link.
    signup(sam, "sam", "Sam")
    page = sam.get(f"/invite/{token}")
    assert page.status_code == 410 and "already been used" in page.text


def test_expired_and_revoked_invites_explain_themselves(kyle, settings):
    token = make_invite(kyle)
    conn = connect(settings.database_path)
    with conn:
        conn.execute("UPDATE invites SET expires_at = datetime('now', '-1 minute')")
    conn.close()
    nat = browser(kyle)
    assert "expired" in nat.get(f"/invite/{token}").text
    signup(nat, "nat", "Nat", invite=token)   # the account is made, but the expired link doesn't join
    assert "Payne" not in nat.get("/welcome").text.split("<main")[1]

    token2 = make_invite(kyle)
    invite_id = query(settings, "SELECT id FROM invites ORDER BY id DESC LIMIT 1")[0][0]
    form_post(kyle, "/table/revoke", {"invite_id": invite_id}, page="/table")
    assert "cancelled" in nat.get(f"/invite/{token2}").text
    assert "find that invite" in nat.get("/invite/not-a-real-token-123").text


def test_invite_tokens_are_stored_hashed(kyle, settings):
    token = make_invite(kyle)
    stored = query(settings, "SELECT token_hash FROM invites")[0][0]
    assert token not in stored and len(stored) == 64


# ---- Removing members and keeping access in step ----

@pytest.fixture
def family(kyle, settings):
    """Kyle and Nat at Payne's Table, with a recipe (and photo) Nat added and a planned dinner."""
    token = make_invite(kyle)
    nat = browser(kyle)
    signup(nat, "nat", "Nat", invite=token)
    token_page = nat.get("/recipes/new").text
    response = nat.post("/recipes/new", data={
        "csrf_token": csrf_from(token_page), "table_id": table_id(settings), "name": "Nat's Nuggets",
        "category": "kids-pick", "ingredient_amount": ["2"], "ingredient_name": ["chicken breasts"],
        "step_title": [""], "step_body": ["Bake."], "servings": "4",
    }, files={"photo": ("n.jpg", make_image(), "image/jpeg")})
    recipe = int(response.headers["location"].split("/")[2].split("?")[0])
    plan(kyle, 1, recipe)
    return kyle, nat, recipe


def test_removed_member_loses_every_route_but_keeps_their_account(family, settings):
    kyle, nat, recipe = family
    photo = query(settings, "SELECT photo_filename FROM recipes WHERE id = ?", (recipe,))[0][0]
    mid = meal_id(settings, 1)
    # Nat has the planner open; it polls.
    live = f"/plan/{W}/live?table={table_id(settings)}&since=-1"
    assert nat.get(live, headers=FETCH).status_code == 200
    assert nat.get(f"/photos/{photo}").status_code == 200

    nat_id = query(settings, "SELECT id FROM users WHERE username = 'nat'")[0][0]
    page = kyle.get("/table").text
    assert "lose access to" in page and "Their account and any other Tables" in page
    assert form_post(kyle, "/table/remove", {"member_id": nat_id}, page="/table").status_code == 422  # not confirmed
    form_post(kyle, "/table/remove", {"member_id": nat_id, "confirm": "yes"}, page="/table")

    # The open page learns at its next poll, and every direct route is closed.
    poll = nat.get(live, headers=FETCH)
    assert poll.status_code == 404 and poll.json()["access"] == "lost"
    assert nat.get(f"/recipes/{recipe}").status_code == 404
    assert nat.get(f"/photos/{photo}").status_code == 404
    assert nat.get(f"/plan/meals/{mid}").status_code == 404
    assert nat.get(f"/plan/meals/{mid}/chat", headers=FETCH).status_code == 404
    assert nat.get(f"/shopping/{W}/live?table={table_id(settings)}", headers=FETCH).status_code == 404
    # Changes sent from the stale page are refused too.
    stale = nat.post(f"/plan/{W}/day/2/clear", data={"csrf_token": csrf_from(nat.get("/account").text),
                                                       "table_id": table_id(settings)}, headers=FETCH)
    assert stale.status_code == 404
    # The plain app sends Nat to start or join a Table.
    assert nat.get("/").headers["location"] == "/welcome"
    # Account kept; her recipe stays with the Table, credited to her.
    assert query(settings, "SELECT COUNT(*) FROM users WHERE username = 'nat'") == [(1,)]
    assert "Added by Nat" in kyle.get(f"/recipes/{recipe}").text


def test_rejoining_after_removal(family, settings):
    kyle, nat, recipe = family
    nat_id = query(settings, "SELECT id FROM users WHERE username = 'nat'")[0][0]
    form_post(kyle, "/table/remove", {"member_id": nat_id, "confirm": "yes"}, page="/table")
    token = make_invite(kyle)
    form_post(nat, f"/invite/{token}", page=f"/invite/{token}")
    assert nat.get(f"/recipes/{recipe}").status_code == 200


def test_last_member_cannot_leave_and_nobody_removes_themselves(kyle, settings):
    response = form_post(kyle, "/table/leave", {"confirm": "yes"}, page="/table")
    assert response.status_code == 422 and "only member" in response.text
    me = query(settings, "SELECT id FROM users WHERE username = ?", (USERNAME,))[0][0]
    response = form_post(kyle, "/table/remove", {"member_id": me, "confirm": "yes"}, page="/table")
    assert response.status_code == 422
    assert query(settings, "SELECT COUNT(*) FROM memberships") == [(1,)]


def test_leaving_keeps_the_table_for_the_others(family, settings):
    kyle, nat, recipe = family
    response = form_post(nat, "/table/leave", {"confirm": "yes"}, page="/table")
    assert response.headers["location"] == "/welcome"
    assert kyle.get(f"/recipes/{recipe}").status_code == 200
    assert nat.get(f"/recipes/{recipe}").status_code == 404


def test_any_member_can_rename(family):
    kyle, nat, _ = family
    form_post(nat, "/table/rename", {"name": "The Paynes"}, page="/table")
    assert "The Paynes" in kyle.get("/plan").text


# ---- Another household can't see in ----

@pytest.fixture
def neighbors(kyle, settings):
    """A separate household with its own Table."""
    other = browser(kyle)
    signup(other, "lee", "Lee")
    form_post(other, "/tables/new", {"name": "Lee's Table"}, page="/welcome")
    return other


def test_other_household_cannot_reach_private_records(kyle, neighbors, settings):
    secret = add_recipe(settings, "Secret Family Sauce", [("1 cup", "secret spice")])
    conn = connect(settings.database_path)
    with conn:
        conn.execute("UPDATE recipes SET photo_filename = 'b' || substr(hex(randomblob(16)), 1, 31) || '.webp' WHERE id = ?",
                     (secret,))
    conn.close()
    photo = query(settings, "SELECT photo_filename FROM recipes WHERE id = ?", (secret,))[0][0].lower()
    (settings.upload_dir / photo).write_bytes(make_image("WEBP", (10, 10)))
    conn = connect(settings.database_path)
    with conn:
        conn.execute("UPDATE recipes SET photo_filename = ? WHERE id = ?", (photo, secret))
    conn.close()
    plan(kyle, 0, secret)
    mid = meal_id(settings, 0)
    kyle_table = table_id(settings, "Payne's Table")
    form_post(kyle, f"/shopping/{W}/items", {"name": "Saffron", "table_id": kyle_table}, page="/shopping")
    item = query(settings, "SELECT id, rev FROM shopping_items WHERE name = 'Saffron'")[0]

    lee = neighbors
    assert kyle.get(f"/photos/{photo}").status_code == 200
    # Direct links and guessed ids
    assert lee.get(f"/recipes/{secret}").status_code == 404
    assert lee.get(f"/recipes/{secret}/edit").status_code == 404
    assert lee.get(f"/photos/{photo}").status_code == 404
    assert lee.get(f"/plan/meals/{mid}").status_code == 404
    assert lee.get(f"/plan/meals/{mid}/ingredients").status_code == 404
    # Asking for Kyle's Table by id
    assert lee.get(f"/plan?table={kyle_table}").status_code == 404
    assert lee.get(f"/shopping?table={kyle_table}").status_code == 404
    assert lee.get(f"/shopping/{W}/live?table={kyle_table}", headers=FETCH).status_code == 404
    # Search and lists show only Lee's Table
    assert "Secret Family Sauce" not in lee.get("/?q=secret").text
    assert "Secret Family Sauce" not in lee.get(f"/plan/{W}/day/0/choose").text
    assert "Saffron" not in lee.get("/shopping").text
    # Writes with a forged table_id or someone else's ids
    token = csrf_from(lee.get("/").text)
    assert lee.post(f"/plan/{W}/day/3", data={"csrf_token": token, "table_id": kyle_table,
                                              "recipe_id": secret}).status_code == 404
    lee.post(f"/plan/{W}/day/3", data={"csrf_token": token, "recipe_id": secret})   # own Table, someone else's recipe
    assert query(settings, "SELECT COUNT(*) FROM planned_meals WHERE recipe_id = ?", (secret,)) == [(1,)]
    lee.post(f"/shopping/{W}/items/{item[0]}/status", data={"csrf_token": token, "status": "bought", "rev": item[1]})
    assert query(settings, "SELECT status FROM shopping_items WHERE id = ?", (item[0],)) == [("need",)]
    assert lee.post(f"/recipes/{secret}/delete", data={"csrf_token": token}).status_code == 404
    assert lee.post(f"/plan/meals/{mid}/chat", data={"csrf_token": token, "message": "What's in the sauce?"},
                    headers=FETCH).status_code == 404
    assert query(settings, "SELECT COUNT(*) FROM recipes WHERE id = ?", (secret,)) == [(1,)]


def test_staples_belong_to_one_table(kyle, neighbors, settings):
    form_post(kyle, "/staples", {"name": "Za'atar"}, page="/staples")
    assert "Za&#39;atar" in kyle.get("/staples").text
    assert "Za&#39;atar" not in neighbors.get("/staples").text
    staple = query(settings, "SELECT id FROM staples WHERE name = ?", ("Za'atar",))[0][0]
    form_post(neighbors, f"/staples/{staple}/delete", page="/staples")
    assert query(settings, "SELECT COUNT(*) FROM staples WHERE id = ?", (staple,)) == [(1,)]


# ---- Two Tables: switching ----

@pytest.fixture
def two_tables(kyle, settings):
    form_post(kyle, "/tables/new", {"name": "Lake House"}, page="/welcome")
    lake = table_id(settings, "Lake House")
    home = table_id(settings, "Payne's Table")
    form_post(kyle, f"/shopping/{W}/items", {"name": "Firewood", "table_id": lake}, page="/shopping")
    form_post(kyle, "/tables/switch", {"switch_to": home, "section": "shopping"}, page="/")
    form_post(kyle, f"/shopping/{W}/items", {"name": "Milk", "table_id": home}, page="/shopping")
    return kyle, home, lake


def test_switching_on_groceries_shows_the_other_list_at_once(two_tables):
    kyle, home, lake = two_tables
    page = kyle.get(f"/shopping?week={W}").text
    assert "Milk" in page and "Firewood" not in page
    response = form_post(kyle, "/tables/switch", {"switch_to": lake, "section": "shopping"}, page="/shopping")
    assert response.headers["location"] == "/shopping"
    page = kyle.get(f"/shopping?week={W}").text
    assert "Firewood" in page and "Milk" not in page
    assert re.search(r'data-active-table-name>Lake House<', page)


def test_a_page_drawn_for_one_table_never_changes_another(two_tables, settings):
    kyle, home, lake = two_tables
    old_page = kyle.get(f"/shopping?week={W}").text     # drawn for Payne's Table
    assert f'name="table_id" value="{home}"' in old_page
    form_post(kyle, "/tables/switch", {"switch_to": lake, "section": "shopping"}, page="/shopping")
    # The old tab adds an item: it goes to the Table that tab showed, not the newly current one.
    kyle.post(f"/shopping/{W}/items", data={"csrf_token": csrf_from(old_page), "table_id": home, "name": "Eggs"})
    assert query(settings, "SELECT table_id FROM shopping_items WHERE name = 'Eggs'") == [(home,)]


def test_remembered_table_falls_back_when_access_changes(two_tables, settings):
    kyle, home, lake = two_tables
    form_post(kyle, "/tables/switch", {"switch_to": lake, "section": "plan"}, page="/")
    conn = connect(settings.database_path)
    with conn:   # someone else removes Kyle from the Lake House (simulated directly)
        conn.execute("DELETE FROM memberships WHERE table_id = ? AND user_id = (SELECT id FROM users WHERE username = ?)",
                     (lake, USERNAME))
    conn.close()
    page = kyle.get("/plan")
    assert page.status_code == 200 and "Payne&#39;s Table" in page.text


def test_switching_needs_membership_and_csrf(two_tables, neighbors, settings):
    kyle, home, lake = two_tables
    lee_table = table_id(settings, "Lee's Table")
    assert form_post(kyle, "/tables/switch", {"switch_to": lee_table}, page="/").status_code == 404
    assert kyle.post("/tables/switch", data={"switch_to": lake, "csrf_token": "x"}).status_code == 403


# ---- Sessions and recovery ----

def test_sessions_can_be_listed_and_ended(kyle, settings):
    phone = browser(kyle)
    login(phone)
    page = kyle.get("/account").text
    assert "(this device)" in page and page.count('action="/account/end-session"') == 1
    ref = re.search(r'name="session" value="([0-9a-f]{12})"', page).group(1)
    form_post(kyle, "/account/end-session", {"session": ref}, page="/account")
    assert phone.get("/").status_code == 303   # that phone is signed out


def test_changing_password_signs_out_other_devices(kyle):
    phone = browser(kyle)
    login(phone)
    response = form_post(kyle, "/account/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                         page="/account")
    assert response.headers["location"] == "/account?msg=password"
    assert phone.get("/").status_code == 303
    assert kyle.get("/").status_code == 200
    kyle.cookies.clear()
    assert login(kyle, password=NEW_PASSWORD).status_code == 303


def test_one_time_reset_link(kyle, settings):
    from app.accounts import create_reset_link
    conn = connect(settings.database_path)
    token = create_reset_link(conn, USERNAME)
    conn.close()
    other = browser(kyle)
    page = other.get(f"/reset/{token}")
    assert page.status_code == 200
    response = other.post(f"/reset/{token}", data={"password": NEW_PASSWORD, "csrf_token": csrf_from(page.text)})
    assert response.headers["location"] == "/login?reset=1"
    assert kyle.get("/").status_code == 303                 # signed out everywhere
    assert other.get(f"/reset/{token}").status_code == 404  # used once
    assert login(other, password=NEW_PASSWORD).status_code == 303
    assert "Ask the person who looks after it" in other.get("/forgot").text


def test_secure_cookie_uses_host_prefix(tmp_path):
    from app.config import Settings
    from tests.conftest import PASSWORD_HASH
    secure = Settings(data_dir=tmp_path / "d", username=USERNAME, password_hash=PASSWORD_HASH, cookie_secure=True)
    with TestClient(create_app(secure), base_url="https://testserver", follow_redirects=False) as c:
        response = login(c)
        cookie = next(h for h in response.headers.get_list("set-cookie") if h.startswith("__Host-ft_session="))
        assert "Secure" in cookie and "Path=/" in cookie and "HttpOnly" in cookie


# ---- Upgrading the single-family database ----

def legacy_database(path):
    from app.migrations import LEGACY_ADDED_COLUMNS, LEGACY_SCHEMA
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.executescript(LEGACY_SCHEMA)
    for table, column, definition in LEGACY_ADDED_COLUMNS:   # the last single-family release
        if column not in [r[1] for r in old.execute(f"PRAGMA table_info({table})")]:
            old.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    old.executescript("""
        INSERT INTO recipes (id, name, category, servings, photo_filename) VALUES
            (3, 'Chili', 'soups', 4, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.webp'), (9, 'Tacos', 'weeknight', 4, NULL);
        INSERT INTO ingredients (recipe_id, position, amount, name) VALUES (3, 0, '1 lb', 'beef'), (9, 0, '8', 'tortillas');
        INSERT INTO steps (recipe_id, position, body) VALUES (3, 0, 'Simmer.');
        INSERT INTO notes (recipe_id, author, body) VALUES (3, 'Mom', 'More beans.');
        INSERT INTO week_plans (week_start, plan_version, list_version) VALUES ('2026-11-09', 4, 2);
        INSERT INTO planned_meals (id, week_start, day, recipe_id, servings) VALUES (11, '2026-11-09', 0, 3, 8);
        INSERT INTO planned_meals (id, week_start, day, recipe_id) VALUES (12, '2026-11-09', 5, NULL);
        INSERT INTO packaged_meals (meal_id, name, label_photo) VALUES (12, 'Frozen Pizza', 'cccccccccccccccccccccccccccccccc.webp');
        INSERT INTO shopping_items (id, week_start, item_key, name, quantity, status) VALUES (5, '2026-11-09', 'beef', 'Beef', '2 lbs', 'bought');
        INSERT INTO shopping_item_sources (item_id, recipe_id, recipe_name, amount, meal_id, ingredient, servings)
            VALUES (5, 3, 'Chili', '2 lbs', 11, 'beef', 8);
        INSERT INTO meal_messages (meal_id, role, body) VALUES (11, 'user', 'How much beef?');
        INSERT INTO staples (name, name_key) VALUES ('Salt', 'salt');
        INSERT INTO app_meta (key, value) VALUES ('staples_seeded', 'yes');
        INSERT INTO sessions (token_hash, csrf_token, expires_at) VALUES ('old', 'x', '2099-01-01 00:00:00');
    """)
    old.commit()
    old.close()


def test_upgrade_moves_everything_into_one_table_after_a_backup(settings):
    legacy_database(settings.database_path)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / ("a" * 32 + ".webp")).write_bytes(make_image("WEBP", (10, 10)))
    (settings.upload_dir / ("c" * 32 + ".webp")).write_bytes(make_image("WEBP", (10, 10)))
    for _ in range(2):    # starting twice is harmless
        with TestClient(create_app(settings), follow_redirects=False) as c:
            assert login(c).status_code == 303
            home = c.get("/").text
            assert "Chili" in home and "Tacos" in home and "Family&#39;s Table" in home
            assert c.get("/photos/" + "a" * 32 + ".webp").status_code == 200
            assert c.get("/photos/" + "c" * 32 + ".webp").status_code == 200   # packaged-meal label
            assert "How much beef?" in c.get("/plan/meals/11").text
            assert "Frozen Pizza" in c.get("/plan/meals/12").text
            assert "Beef" in c.get(f"/shopping?week={W}").text
    tid = query(settings, "SELECT id FROM family_tables")[0][0]
    assert query(settings, "SELECT id, table_id FROM recipes ORDER BY id") == [(3, tid), (9, tid)]
    assert query(settings, "SELECT id, servings FROM planned_meals ORDER BY id") == [(11, 8), (12, None)]
    assert query(settings, "SELECT status FROM shopping_items") == [("bought",)]
    assert query(settings, "SELECT meal_id, servings FROM shopping_item_sources") == [(11, 8)]
    assert query(settings, "SELECT author FROM notes") == [("Mom",)]
    assert query(settings, "SELECT name FROM staples") == [("Salt",)]       # deleted defaults stay deleted
    assert query(settings, "SELECT COUNT(*) FROM sessions WHERE token_hash = 'old'") == [(0,)]
    assert query(settings, "PRAGMA foreign_key_check") == []
    assert query(settings, "PRAGMA user_version") == [(2,)]
    backups = list((settings.data_dir / "backups").glob("*.db"))
    assert len(backups) == 1
    original = sqlite3.connect(backups[0])
    assert original.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 2
    assert "table_id" not in [r[1] for r in original.execute("PRAGMA table_info(recipes)")]
    original.close()


def test_upgrade_without_a_login_keeps_data_for_the_first_account(tmp_path):
    from app.config import Settings
    from tests.conftest import PASSWORD_HASH
    data = tmp_path / "data"
    legacy_database(data / "family_table.db")
    with TestClient(create_app(Settings(data_dir=data, username="", password_hash="")), follow_redirects=False):
        pass
    assert query_path(data / "family_table.db", "SELECT COUNT(*) FROM memberships") == [(0,)]
    # Configuring the login (python -m app.setup_login writes .env) adopts the waiting Table.
    with TestClient(create_app(Settings(data_dir=data, username="kyle", password_hash=PASSWORD_HASH)),
                    follow_redirects=False) as c:
        assert login(c, username="kyle").status_code == 303
        assert "Chili" in c.get("/").text


def query_path(path, sql):
    conn = sqlite3.connect(path)
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def test_failed_upgrade_changes_nothing(settings, monkeypatch):
    from app import migrations
    legacy_database(settings.database_path)
    monkeypatch.setattr(migrations, "_rebuild_children", lambda conn: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        with TestClient(create_app(settings)):
            pass
    assert query(settings, "PRAGMA user_version") == [(0,)]
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(2,)]
    assert "table_id" not in [r[1] for r in query(settings, "PRAGMA table_info(recipes)")]
