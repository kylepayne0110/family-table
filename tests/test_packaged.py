"""Scan a packaged meal: barcodes, the (mocked) product lookup, planning it for a day, and directions.

Every lookup here uses a stand-in for Open Food Facts; no test touches the network.
The stand-in answers are shaped like real Open Food Facts responses.
"""

# json builds stand-in API answers; re reads pages; sqlite3 builds an old database.
import json
import re
import sqlite3

# datetime/zoneinfo move the household clock past Friday.
from datetime import datetime
from zoneinfo import ZoneInfo

# pytest provides fixtures.
import pytest

# TestClient starts the app against an upgraded database; a second client is the other phone.
from fastapi.testclient import TestClient

# Project modules.
from app import packaged
from app.main import create_app
from app.safe_fetch import FetchError
from tests.conftest import login, make_image
from tests.plan_helpers import FETCH, TACOS, W, add_recipe, plan, post, query, token

FRIDAY = 4
ORANGE_CHICKEN = "013800619761"     # real Open Food Facts record (Stouffer's Orange Chicken, 22 oz)
WITH_DIRECTIONS = "036800123458"    # stand-in product whose record includes preparation text
UNKNOWN = "012345678905"

# What Open Food Facts returned for the real Stouffer's barcode (fields this app asks for).
OFF_ANSWERS = {
    ORANGE_CHICKEN: {"code": "0013800619761", "status": 1, "product": {
        "code": "0013800619761", "product_name": "ORANGE CHICKEN", "product_name_en": "ORANGE CHICKEN",
        "brands": "Stouffer's", "quantity": "22 oz"}},
    WITH_DIRECTIONS: {"code": WITH_DIRECTIONS, "status": 1, "product": {
        "code": WITH_DIRECTIONS, "product_name": "Lasagna with Meat & Sauce", "brands": "Test Kitchen, Other",
        "quantity": "10.5 oz",
        "preparation_en": "Cook thoroughly. Microwave: Cut film to vent. Cook on high 6 minutes. Stir. "
                          "Conventional oven: Preheat to 375°F. Bake 45 minutes."}},
}


class FakeResult:
    def __init__(self, body):
        self.body = json.dumps(body).encode()


def fake_fetch(url, max_bytes, accept="", user_agent=""):
    assert url.startswith("https://world.openfoodfacts.org/api/v2/product/")
    assert "FamilyTable" in user_agent and "@" not in user_agent      # identifies the app, no personal data
    code = re.search(r"/product/(\d+)\.json", url).group(1)
    if code == "999999999993":
        raise FetchError("timeout")
    if code not in OFF_ANSWERS:
        raise FetchError("http", 404)                                  # how Open Food Facts says "unknown"
    return FakeResult(OFF_ANSWERS[code])


@pytest.fixture(autouse=True)
def mocked_lookup(monkeypatch):
    packaged.clear_cache()
    monkeypatch.setattr(packaged, "fetch", fake_fetch)
    yield
    packaged.clear_cache()


def scan_url(day=FRIDAY, week=W):
    return f"/plan/{week}/day/{day}/packaged"


def add_packaged(client, data, photo=None, day=FRIDAY, field="label_photo_file"):
    payload = {"name": "", "brand": "", "package_size": "", "barcode": "", "oven": "", "microwave": "", "other": "",
               **data, "csrf_token": token(client)}
    files = {field: ("label.jpg", photo, "image/jpeg")} if photo else None
    return client.post(scan_url(day), data=payload, files=files)


def counts(settings):
    return {t: query(settings, f"SELECT COUNT(*) FROM {t}")[0][0]
            for t in ("recipes", "shopping_items", "shopping_item_sources", "planned_meals", "packaged_meals")}


# ---- Barcodes and directions (pure) ----

@pytest.mark.parametrize("code, ok", [
    ("013800619761", True), ("0013800619761", True), ("96385074", True), ("013800619762", False),
    ("12345", False), ("abc", False), ("", False),
])
def test_barcode_check_digits(code, ok):
    assert (packaged.barcode_problem(code) is None) == ok


def test_upc_e_is_expanded_and_digits_are_cleaned():
    assert packaged.normalize_barcode("0425 2614") == "042100005264"
    assert packaged.normalize_barcode("0138-0061-9761") == "013800619761"


def test_oven_and_microwave_directions_are_kept_apart_word_for_word():
    text = OFF_ANSWERS[WITH_DIRECTIONS]["product"]["preparation_en"]
    split = packaged.split_directions(text)
    assert split == {"oven": "Preheat to 375°F. Bake 45 minutes.",
                     "microwave": "Cut film to vent. Cook on high 6 minutes. Stir.",
                     "other": "Cook thoroughly."}
    assert packaged.split_directions("Heat until hot.") == {"oven": "", "microwave": "", "other": "Heat until hot."}


def test_lookup_reads_name_brand_size_and_reports_missing_directions():
    product = packaged.lookup(ORANGE_CHICKEN)
    assert (product.found, product.name, product.brand, product.package_size) == (True, "ORANGE CHICKEN", "Stouffer's", "22 oz")
    assert product.as_dict()["has_directions"] is False
    assert product.source_url == "https://world.openfoodfacts.org/product/0013800619761"
    assert packaged.lookup(UNKNOWN).found is False and not packaged.lookup(UNKNOWN).error
    offline = packaged.lookup("999999999993")
    assert offline.found is False and "still add it by hand" in offline.error


# ---- The page and lookup endpoint ----

def test_scan_page_offers_camera_photo_digits_and_manual_entry(auth_client):
    page = auth_client.get(scan_url()).text
    assert "Scan a packaged <em>meal</em>" in page and "Friday" in page
    assert "data-scan-camera" in page and 'capture="environment"' in page
    assert 'inputmode="numeric"' in page and "Enter the meal's name instead" in page
    assert "Add to Friday" in page and "won't change" in page
    assert "/static/vendor/zxing-0.21.3.min.js" in page


def test_lookup_endpoint(auth_client):
    data = auth_client.get(f"/packaged/lookup?barcode={ORANGE_CHICKEN}", headers=FETCH).json()
    assert data["found"] and data["name"] == "ORANGE CHICKEN" and data["package_size"] == "22 oz"
    assert auth_client.get(f"/packaged/lookup?barcode={UNKNOWN}").json()["found"] is False
    bad = auth_client.get("/packaged/lookup?barcode=013800619762")
    assert bad.status_code == 422 and "add up" in bad.json()["problem"]


def test_lookup_works_without_javascript(auth_client):
    page = auth_client.get(scan_url() + f"?barcode={WITH_DIRECTIONS}").text
    assert "Found on Open Food Facts" in page and "Lasagna with Meat &amp; Sauce" in page and "10.5 oz" in page
    assert ">Preheat to 375°F. Bake 45 minutes.</textarea>" in page
    assert ">Cut film to vent. Cook on high 6 minutes. Stir.</textarea>" in page


def test_lookup_and_scan_need_sign_in(client):
    assert client.get(f"/packaged/lookup?barcode={ORANGE_CHICKEN}").status_code == 303
    assert client.get(scan_url()).status_code == 303
    assert client.post(scan_url(), data={"name": "x"}).status_code == 303


def test_adding_needs_csrf(auth_client, settings):
    before = counts(settings)
    assert auth_client.post(scan_url(), data={"name": "Pizza"}).status_code == 403
    assert counts(settings) == before


# ---- Adding to Friday ----

def test_scan_review_and_add_to_friday(auth_client, settings):
    before = counts(settings)
    auth_client.get(f"/packaged/lookup?barcode={ORANGE_CHICKEN}", headers=FETCH)
    response = add_packaged(auth_client, {"barcode": ORANGE_CHICKEN, "name": "ORANGE CHICKEN", "brand": "Stouffer's",
                                          "package_size": "22 oz"}, photo=make_image(size=(3000, 4000)))
    assert response.status_code == 303
    meal_id = int(re.search(r"/plan/meals/(\d+)\?msg=planned", response.headers["location"]).group(1))

    after = counts(settings)
    assert after["recipes"] == before["recipes"]                       # never in the recipe box
    assert after["shopping_items"] == before["shopping_items"] == 0    # shopping list untouched
    assert after["packaged_meals"] == 1
    assert query(settings, "SELECT day, recipe_id FROM planned_meals") == [(FRIDAY, None)]
    row = query(settings, "SELECT barcode, name, brand, package_size, product_source, label_photo FROM packaged_meals")[0]
    assert row[:5] == (ORANGE_CHICKEN, "ORANGE CHICKEN", "Stouffer's", "22 oz", "openfoodfacts")

    board = auth_client.get(f"/plan?week={W}").text                   # refresh the planner
    friday = re.search(r'id="day-4".*?</li>', board, re.S).group(0)
    assert "ORANGE CHICKEN" in friday and "Packaged" in friday and "Add ingredients" not in friday
    page = auth_client.get(f"/plan/meals/{meal_id}").text              # reopen it
    assert "Stouffer&#39;s · 22 oz · UPC 013800619761" in page and "Open Food Facts" in page
    assert f'src="/photos/{row[5]}"' in page and "Tap to enlarge" in page
    assert auth_client.get(f"/photos/{row[5]}").status_code == 200
    from PIL import Image
    with Image.open(settings.upload_dir / row[5]) as stored:
        assert max(stored.size) == 2800                                # kept large enough to read the small print
    assert "Added to the plan. The shopping list wasn&#39;t changed." in auth_client.get(response.headers["location"]).text


def test_other_household_phone_sees_the_meal(auth_client, settings):
    add_packaged(auth_client, {"name": "Frozen Lasagna", "oven": "Bake at 375°F for 45 minutes."})
    meal_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    with TestClient(auth_client.app, follow_redirects=False) as other:
        login(other)
        page = other.get(f"/plan/meals/{meal_id}").text
        assert "Frozen Lasagna" in page and "Bake at 375°F for 45 minutes." in page and "Typed in by your family." in page


def test_directions_from_the_lookup_are_labelled_and_separate(auth_client, settings):
    product = packaged.lookup(WITH_DIRECTIONS)
    add_packaged(auth_client, {"barcode": WITH_DIRECTIONS, "name": product.name, "oven": product.oven,
                               "microwave": product.microwave + " Let stand 1 minute.", "other": product.other})
    row = query(settings, "SELECT oven_source, microwave_source, other_source FROM packaged_meals")[0]
    assert row == ("openfoodfacts", "typed", "openfoodfacts")         # the edited section counts as typed
    meal_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    page = auth_client.get(f"/plan/meals/{meal_id}").text
    oven = re.search(r'<h3 class="pk-method__title">Oven</h3>.*?</div>', page, re.S).group(0)
    micro = re.search(r'<h3 class="pk-method__title">Microwave</h3>.*?</div>', page, re.S).group(0)
    assert "Preheat to 375°F. Bake 45 minutes." in oven and "From Open Food Facts" in oven
    assert "Cook on high 6 minutes" in micro and "Typed in by your family." in micro


def test_unknown_barcode_can_still_be_added_by_name(auth_client, settings):
    page = auth_client.get(scan_url() + f"?barcode={UNKNOWN}").text
    assert f"No product found for {UNKNOWN}." in page
    response = add_packaged(auth_client, {"barcode": UNKNOWN, "name": "Store-brand Mac & Cheese"})
    assert response.status_code == 303
    assert query(settings, "SELECT name, product_source FROM packaged_meals") == [("Store-brand Mac & Cheese", "manual")]


def test_lookup_outage_still_lets_the_meal_be_added(auth_client, settings):
    page = auth_client.get(scan_url() + "?barcode=999999999993").text
    assert "didn&#39;t answer" in page
    assert add_packaged(auth_client, {"barcode": "999999999993", "name": "Pot pie"}).status_code == 303


def test_name_only_without_a_barcode(auth_client, settings):
    assert add_packaged(auth_client, {"name": "Frozen pizza"}).status_code == 303
    assert query(settings, "SELECT barcode, name FROM packaged_meals") == [("", "Frozen pizza")]


def test_bad_input_is_refused_and_kept(auth_client, settings):
    response = add_packaged(auth_client, {"name": "", "brand": "Banquet"})
    assert response.status_code == 422 and "Give the meal a name" in response.text and 'value="Banquet"' in response.text
    response = add_packaged(auth_client, {"name": "Pizza", "barcode": "013800619762"})
    assert response.status_code == 422 and "add up" in response.text
    response = add_packaged(auth_client, {"name": "Pizza"}, photo=b"not a photo")
    assert response.status_code == 422 and "valid photo" in response.text
    assert counts(settings)["planned_meals"] == 0


# ---- Product with no directions: add now, attach directions later ----

def test_no_directions_then_attach_a_label_photo_later(auth_client, settings):
    page = auth_client.get(scan_url() + f"?barcode={ORANGE_CHICKEN}").text
    assert "Open Food Facts has no cooking directions for this product" in page
    add_packaged(auth_client, {"barcode": ORANGE_CHICKEN, "name": "ORANGE CHICKEN", "brand": "Stouffer's"})
    meal_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    assert "No cooking directions yet" in auth_client.get(f"/plan/meals/{meal_id}").text

    response = auth_client.post(f"/plan/meals/{meal_id}/packaged",
                                data={"oven": "", "microwave": "Cook on high 7 minutes.", "other": "", "csrf_token": token(auth_client)},
                                files={"label_photo_camera": ("panel.jpg", make_image(), "image/jpeg")})
    assert response.headers["location"] == f"/plan/meals/{meal_id}?msg=directions"
    photo, source = query(settings, "SELECT label_photo, microwave_source FROM packaged_meals")[0]
    page = auth_client.get(f"/plan/meals/{meal_id}").text
    assert f"/photos/{photo}" in page and "Cook on high 7 minutes." in page and source == "typed"

    # Replacing the photo removes the old file.
    auth_client.post(f"/plan/meals/{meal_id}/packaged",
                     data={"microwave": "Cook on high 7 minutes.", "csrf_token": token(auth_client)},
                     files={"label_photo_file": ("panel2.jpg", make_image(color=(1, 2, 3)), "image/jpeg")})
    assert not (settings.upload_dir / photo).exists()
    assert counts(settings)["shopping_items"] == 0 and counts(settings)["recipes"] == 0


# ---- Never silently replacing a dinner ----

def test_a_scan_never_silently_replaces_a_planned_dinner(auth_client, settings):
    rid = add_recipe(settings, "Taco Bowls", TACOS)
    plan(auth_client, FRIDAY, rid)
    page = auth_client.get(scan_url()).text
    assert "Friday already has <strong>Taco Bowls</strong>" in page and 'name="confirm_replace"' in page

    response = add_packaged(auth_client, {"name": "Frozen Lasagna"}, photo=make_image())
    assert response.status_code == 409 and "Taco Bowls is already planned" in response.text
    assert query(settings, "SELECT recipe_id FROM planned_meals") == [(rid,)]
    assert list(settings.upload_dir.glob("*.webp")) == []               # the refused upload isn't kept

    old_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    response = add_packaged(auth_client, {"name": "Frozen Lasagna", "replace_meal_id": str(old_id), "confirm_replace": "on"})
    assert response.status_code == 303
    assert query(settings, "SELECT p.recipe_id, pk.name FROM planned_meals p JOIN packaged_meals pk ON pk.meal_id = p.id") == [
        (None, "Frozen Lasagna")]
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(1,)]       # the recipe itself stays in the box


def test_confirmation_for_a_different_dinner_doesnt_count(auth_client, settings):
    """Another phone changed Friday after this page loaded: the old confirmation can't replace the new dinner."""
    rid = add_recipe(settings, "Taco Bowls", TACOS)
    plan(auth_client, FRIDAY, rid)
    stale_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    other = add_recipe(settings, "Burgers", [("1 lb", "beef")])
    post(auth_client, f"/plan/{W}/day/{FRIDAY}/clear")
    plan(auth_client, FRIDAY, other)
    response = add_packaged(auth_client, {"name": "Pizza", "replace_meal_id": str(stale_id), "confirm_replace": "on"})
    assert response.status_code == 409 and query(settings, "SELECT recipe_id FROM planned_meals") == [(other,)]


def test_changing_a_packaged_day_to_a_recipe_removes_its_photo(auth_client, settings):
    add_packaged(auth_client, {"name": "Pizza"}, photo=make_image())
    photo = query(settings, "SELECT label_photo FROM packaged_meals")[0][0]
    rid = add_recipe(settings, "Taco Bowls", TACOS)
    plan(auth_client, FRIDAY, rid)                                       # the explicit "Change" picker
    assert query(settings, "SELECT recipe_id FROM planned_meals") == [(rid,)]
    assert counts(settings)["packaged_meals"] == 0 and not (settings.upload_dir / photo).exists()


def test_remove_moves_and_recipe_only_features(auth_client, settings):
    add_packaged(auth_client, {"name": "Pizza"}, photo=make_image())
    meal_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    post(auth_client, f"/plan/{W}/move", {"from_day": FRIDAY, "to_day": 5})
    assert query(settings, "SELECT id, day FROM planned_meals") == [(meal_id, 5)]   # same dinner, new day
    assert auth_client.get(f"/plan/meals/{meal_id}/ingredients").status_code == 404
    assert post(auth_client, f"/plan/meals/{meal_id}/servings", {"servings": "4"}).status_code == 404
    photo = query(settings, "SELECT label_photo FROM packaged_meals")[0][0]
    post(auth_client, f"/plan/{W}/day/5/clear")
    assert counts(settings)["packaged_meals"] == 0 and not (settings.upload_dir / photo).exists()


# ---- After Friday ----

def test_packaged_meal_stays_in_history_after_friday(auth_client, settings):
    add_packaged(auth_client, {"name": "Frozen Lasagna", "oven": "Bake 45 minutes."})
    meal_id = query(settings, "SELECT id FROM planned_meals")[0][0]
    auth_client.app.state.clock.frozen = datetime(2026, 11, 14, 0, 1, tzinfo=ZoneInfo("America/Chicago"))  # Saturday
    board = auth_client.get(f"/plan?week={W}").text
    assert "Frozen Lasagna" not in re.search(r'id="day-4".*?</li>', board, re.S).group(0)
    history = re.search(r'<section class="plan-history".*?</section>', board, re.S).group(0)
    assert "Friday, Nov 13" in history and "Frozen Lasagna" in history
    assert "Bake 45 minutes." in auth_client.get(f"/plan/meals/{meal_id}").text
    assert auth_client.get(scan_url()).status_code == 404                # the day is over
    assert add_packaged(auth_client, {"name": "Pizza"}).status_code == 422


# ---- Upgrading a database where every planned dinner needed a recipe ----

def test_upgrade_keeps_planned_meals_and_their_conversations(settings):
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True)
    with TestClient(create_app(settings)) as c:     # current schema, then make it look like the previous release
        pass
    old = sqlite3.connect(db_path)
    old.executescript("""
        PRAGMA foreign_keys = OFF;
        PRAGMA legacy_alter_table = ON;
        DROP TABLE packaged_meals;
        ALTER TABLE planned_meals RENAME TO pm_tmp;
        CREATE TABLE planned_meals (id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL REFERENCES week_plans(week_start) ON DELETE CASCADE,
            day INTEGER NOT NULL CHECK (day BETWEEN 0 AND 6),
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            servings INTEGER CHECK (servings IS NULL OR servings BETWEEN 1 AND 100),
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        DROP TABLE pm_tmp;
        INSERT INTO recipes (id, name, category, servings) VALUES (5, 'Chili', 'soups', 4);
        INSERT INTO week_plans (week_start) VALUES ('2026-11-09');
        INSERT INTO planned_meals (id, week_start, day, recipe_id, servings) VALUES (7, '2026-11-09', 2, 5, 8);
        INSERT INTO meal_messages (meal_id, role, body) VALUES (7, 'user', 'How much chili powder?');
    """)
    old.commit()
    old.close()
    for _ in range(2):
        with TestClient(create_app(settings), follow_redirects=False) as c:
            login(c)
            assert c.get("/plan/meals/7").status_code == 200
    assert query(settings, "SELECT id, day, recipe_id, servings FROM planned_meals") == [(7, 2, 5, 8)]
    assert query(settings, "SELECT meal_id, body FROM meal_messages") == [(7, "How much chili powder?")]
    assert [r[3] for r in query(settings, "PRAGMA table_info(planned_meals)") if r[1] == "recipe_id"] == [0]
    assert query(settings, "PRAGMA foreign_key_check") == []
