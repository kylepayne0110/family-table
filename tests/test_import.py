# json builds JSON-LD test pages.
import json

# os/time age files for the cleanup test.
import os
import time

# socket is blocked to prove extraction never touches the network.
import socket

# sqlite3 builds an "old version" database for the migration test.
import sqlite3

# pytest provides fixtures.
import pytest

# Project modules and helpers.
from app import recipe_import
from app.database import connect, init_db
from app.recipe_import import (
    extract_from_json_ld, extract_recipe, parse_duration, split_ingredient, to_form_values,
    cleanup_temp_photos, _servings,
)
from app.safe_fetch import FetchError, FetchResult
from tests.conftest import csrf_from, make_image, recipe_id_from


PAGE_URL = "https://www.example-food.test/recipes/skillet-lasagna"
IMAGE_URL = "https://cdn.example-food.test/lasagna.jpg"


def page(*json_ld_blocks, extra="") -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in json_ld_blocks)
    return f"<html><head><title>Food Blog</title>{scripts}</head><body>{extra}<p>Story...</p></body></html>"


RECIPE = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Skillet Lasagna",
    "description": "All the flavor of lasagna &amp; none of the layering.",
    "image": [{"@type": "ImageObject", "url": IMAGE_URL, "width": 1200}],
    "recipeCuisine": ["Italian", "American"],
    "recipeCategory": "Dinner",
    "prepTime": "PT15M",
    "cookTime": "PT30M",
    "totalTime": "PT45M",
    "recipeYield": ["6", "6 servings"],
    "recipeIngredient": [
        "1 lb ground beef", "1 (24 oz) jar marinara sauce", "8 lasagna noodles, broken",
        "1 ½ cups ricotta", "2 cups shredded mozzarella", "Salt and pepper to taste",
    ],
    "recipeInstructions": [
        {"@type": "HowToSection", "name": "Make the sauce", "itemListElement": [
            {"@type": "HowToStep", "text": "Brown the beef in a large skillet."},
            {"@type": "HowToStep", "text": "Stir in the marinara and 1 cup of water."},
        ]},
        {"@type": "HowToSection", "name": "Finish", "itemListElement": [
            {"@type": "HowToStep", "name": "Simmer", "text": "Add noodles, cover, and simmer 20 minutes."},
            {"@type": "HowToStep", "text": "Dollop ricotta, top with mozzarella, and melt."},
        ]},
    ],
}
GRAPH_PAGE = page({"@context": "https://schema.org", "@graph": [
    {"@type": "WebSite", "name": "Food Blog"},
    {"@type": "Person", "name": "Author", "image": "https://cdn.example-food.test/author.jpg"},
    {**RECIPE, "@context": None},
]})


# ---- Parsing helpers ----

@pytest.mark.parametrize("value, minutes", [
    ("PT15M", 15), ("PT1H30M", 90), ("P0DT2H", 120), ("PT0M", None), ("pt45m", 45),
    (20, 20), ("", None), (None, None), ("about an hour", None),
])
def test_parse_duration(value, minutes):
    assert parse_duration(value) == minutes


@pytest.mark.parametrize("line, expected", [
    ("2 cups all-purpose flour", ("2 cups", "all-purpose flour")),
    ("1 ½ cups ricotta", ("1 ½ cups", "ricotta")),
    ("1½ tsp. salt", ("1½ tsp.", "salt")),
    ("1 1/2 lbs chicken thighs", ("1 1/2 lbs", "chicken thighs")),
    ("1 (15 oz) can black beans", ("1 (15 oz) can", "black beans")),
    ("2-3 cloves garlic, minced", ("2-3 cloves", "garlic, minced")),
    ("3 eggs", ("3", "eggs")),
    ("2 tomatoes", ("2", "tomatoes")),
    ("Salt and pepper to taste", ("", "Salt and pepper to taste")),
    ("<b>1 cup</b> sugar &amp; spice", ("1 cup", "sugar & spice")),
    # Formats seen on real recipe sites:
    ("2 and 1/4 cups (281g) all-purpose flour", ("2 and 1/4 cups (281g)", "all-purpose flour")),
    ("1 1/2 cups (355 ml) warm water", ("1 1/2 cups (355 ml)", "warm water")),
    ("▢ 1½ cups cooked chickpeas", ("1½ cups", "cooked chickpeas")),
    ("8 tablespoons of salted butter", ("8 tablespoons", "salted butter")),
    ("100g plain flour", ("100g", "plain flour")),
])
def test_split_ingredient(line, expected):
    assert split_ingredient(line) == expected


@pytest.mark.parametrize("text, servings", [
    ("4 servings", 4), ("Serves 4-6", 4), ("6", 6), ("24 cookies", None), ("", None), ("500 servings", None),
])
def test_servings(text, servings):
    assert _servings(text) == servings


# ---- Extraction ----

def test_json_ld_graph_with_sections():
    recipe = extract_from_json_ld(GRAPH_PAGE, PAGE_URL)
    assert recipe.name == "Skillet Lasagna"
    assert recipe.description == "All the flavor of lasagna & none of the layering."
    assert (recipe.prep_minutes, recipe.cook_minutes, recipe.total_minutes) == (15, 30, 45)
    assert recipe.image_url == IMAGE_URL  # the recipe photo, not the author portrait
    assert recipe.ingredients == RECIPE["recipeIngredient"]  # same order
    assert recipe.steps == [
        ("Make the sauce", "Brown the beef in a large skillet."),
        ("", "Stir in the marinara and 1 cup of water."),
        ("Simmer", "Add noodles, cover, and simmer 20 minutes."),  # a step's own name wins over its section
        ("", "Dollop ricotta, top with mozzarella, and melt."),
    ]


def test_instruction_formats():
    as_text = {**RECIPE, "recipeInstructions": "Boil water.\nCook pasta.\n\nDrain."}
    as_strings = {**RECIPE, "recipeInstructions": ["Boil water.", "Cook pasta.", "Drain."]}
    as_steps = {**RECIPE, "recipeInstructions": [{"@type": "HowToStep", "text": t} for t in ["Boil water.", "Cook pasta.", "Drain."]]}
    nested_list = {**RECIPE, "recipeInstructions": [[{"@type": "HowToStep", "text": "Boil water."}], "Cook pasta.", "Drain."]}
    for variant in (as_text, as_strings, as_steps, nested_list):
        steps = extract_from_json_ld(page(variant), PAGE_URL).steps
        assert [body for _, body in steps] == ["Boil water.", "Cook pasta.", "Drain."], variant["recipeInstructions"]


def test_type_as_list_and_string_image():
    variant = {**RECIPE, "@type": ["Recipe", "NewsArticle"], "image": "https://cdn.example-food.test/a.jpg"}
    assert extract_from_json_ld(page(variant), PAGE_URL).image_url == "https://cdn.example-food.test/a.jpg"


def test_full_pipeline_merges_extractors():
    recipe = extract_recipe(GRAPH_PAGE, PAGE_URL)
    assert recipe.name == "Skillet Lasagna"
    assert recipe.ingredients[0] == "1 lb ground beef" and len(recipe.ingredients) == 6
    assert len(recipe.steps) == 4
    assert recipe.image_url == IMAGE_URL


def test_missing_optional_fields():
    minimal = {"@context": "https://schema.org", "@type": "Recipe", "name": "Toast",
               "recipeIngredient": ["2 slices bread", "butter"]}
    recipe = extract_recipe(page(minimal), PAGE_URL)
    values, notes = to_form_values(recipe, PAGE_URL)
    assert values["name"] == "Toast"
    assert values["description"] == "" and values["cuisine"] == ""
    assert values["prep_minutes"] == values["cook_minutes"] == values["servings"] == ""
    assert values["steps"] == []
    assert recipe.image_url == ""
    assert any("instructions" in n for n in notes)


def test_total_time_only_goes_to_cook_with_note():
    variant = {**RECIPE, "prepTime": None, "cookTime": None, "totalTime": "PT50M"}
    values, notes = to_form_values(extract_from_json_ld(page(variant), PAGE_URL), PAGE_URL)
    assert values["prep_minutes"] == "" and values["cook_minutes"] == "50"
    assert any("total time" in n for n in notes)


def test_page_without_recipe():
    article = {"@context": "https://schema.org", "@type": "Article", "name": "My trip"}
    assert extract_recipe(page(article), PAGE_URL) is None
    assert extract_recipe("<html><body>Just a blog post</body></html>", PAGE_URL) is None
    assert extract_recipe('<script type="application/ld+json">{broken json</script>', PAGE_URL) is None


def test_extraction_never_uses_the_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("extraction tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert extract_recipe(GRAPH_PAGE, PAGE_URL).name == "Skillet Lasagna"


def test_form_values_mapping():
    values, notes = to_form_values(extract_recipe(GRAPH_PAGE, PAGE_URL), PAGE_URL)
    assert values["cuisine"] == "Italian"
    assert values["servings"] == "6"
    assert values["category"] == "weeknight"  # not guessed beyond the default
    assert values["source_url"] == PAGE_URL
    assert values["ingredients"][1] == {"amount": "1 (24 oz) jar", "name": "marinara sauce"}
    assert values["ingredients"][5] == {"amount": "", "name": "Salt and pepper to taste"}
    assert values["steps"][0] == {"title": "Make the sauce", "body": "Brown the beef in a large skillet."}


# ---- Route tests (network stubbed) ----

@pytest.fixture
def fake_web(monkeypatch):
    """Stub the safe fetcher: URL -> FetchResult or FetchError."""
    responses: dict = {}
    requested: list[str] = []

    def fake_fetch(url, max_bytes, accept="text/html"):
        requested.append(url)
        response = responses.get(url, FetchError("http", 404))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(recipe_import, "fetch", fake_fetch)
    responses["requested"] = requested
    return responses


def html_result(body: str, url=PAGE_URL) -> FetchResult:
    return FetchResult(url=url, content_type="text/html", charset="utf-8", body=body.encode())


def image_result(data: bytes) -> FetchResult:
    return FetchResult(url=IMAGE_URL, content_type="image/jpeg", charset=None, body=data)


def post_import(client, url):
    token = csrf_from(client.get("/recipes/new").text)
    return client.post("/recipes/import", data={"url": url, "csrf_token": token})


def recipe_count(settings) -> int:
    return connect(settings.database_path).execute("SELECT COUNT(*) FROM recipes").fetchone()[0]


def test_import_requires_login(client):
    response = client.post("/recipes/import", data={"url": PAGE_URL})
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_import_requires_csrf(auth_client, fake_web):
    response = auth_client.post("/recipes/import", data={"url": PAGE_URL, "csrf_token": "forged"})
    assert response.status_code == 403
    assert fake_web["requested"] == []  # nothing was fetched


def test_add_page_offers_import(auth_client):
    page_html = auth_client.get("/recipes/new").text
    assert "Import from a <em>website</em>" in page_html
    assert "Paste the link and we'll fill in the details for you." in page_html
    # Editing an existing recipe does not show the import box.


def test_import_prefills_form_without_saving(auth_client, settings, fake_web):
    fake_web[PAGE_URL] = html_result(GRAPH_PAGE)
    fake_web[IMAGE_URL] = image_result(make_image("JPEG", (2000, 1500)))

    response = post_import(auth_client, PAGE_URL)
    assert response.status_code == 200
    body = response.text
    assert 'value="Skillet Lasagna"' in body
    assert "All the flavor of lasagna &amp; none of the layering." in body
    assert 'value="1 (24 oz) jar"' in body and 'value="marinara sauce"' in body
    assert body.index("ground beef") < body.index("marinara sauce") < body.index("ricotta")
    assert 'value="Make the sauce"' in body
    assert 'value="15"' in body and 'value="30"' in body and 'value="6"' in body
    assert f'value="{PAGE_URL}"' in body
    assert "Imported from www.example-food.test" in body

    # The photo waits in the temporary folder, shown via the import preview route.
    temp_photos = list(settings.import_dir.glob("*.webp"))
    assert len(temp_photos) == 1
    assert f'name="imported_photo" value="{temp_photos[0].name}"' in body
    assert auth_client.get(f"/photos/import/{temp_photos[0].name}").status_code == 200

    assert recipe_count(settings) == 0  # nothing saved until the user confirms


def test_save_imported_recipe_after_editing(auth_client, settings, fake_web):
    fake_web[PAGE_URL] = html_result(GRAPH_PAGE)
    fake_web[IMAGE_URL] = image_result(make_image())
    body = post_import(auth_client, PAGE_URL).text
    photo = list(settings.import_dir.glob("*.webp"))[0].name

    # The user edits a few imported values, then saves through the normal form.
    response = auth_client.post("/recipes/new", data={
        "csrf_token": csrf_from(body),
        "name": "Weeknight Skillet Lasagna", "description": "Our version.", "category": "sunday-dinner",
        "cuisine": "Italian", "prep_minutes": "10", "cook_minutes": "30", "servings": "4",
        "card_tag": "", "last_made": "", "source_url": PAGE_URL, "imported_photo": photo,
        "ingredient_amount": ["1 lb", "1 jar"], "ingredient_name": ["ground turkey", "marinara sauce"],
        "step_title": ["", ""], "step_body": ["Brown the turkey.", "Add sauce and noodles."],
    })
    assert response.status_code == 303
    recipe_id = recipe_id_from(response)

    row = connect(settings.database_path).execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    assert row["name"] == "Weeknight Skillet Lasagna" and row["category"] == "sunday-dinner"
    assert row["source_url"] == PAGE_URL
    assert row["photo_filename"] == photo
    assert (settings.upload_dir / photo).exists()        # moved into permanent storage
    assert not (settings.import_dir / photo).exists()    # no longer in the temp folder

    detail = auth_client.get(f"/recipes/{recipe_id}").text
    assert "ground turkey" in detail and "Original recipe" in detail
    assert f'href="{PAGE_URL}" target="_blank" rel="noopener noreferrer nofollow"' in detail
    assert auth_client.get(f"/photos/{photo}").status_code == 200


def test_validation_error_keeps_imported_photo(auth_client, settings, fake_web):
    fake_web[PAGE_URL] = html_result(GRAPH_PAGE)
    fake_web[IMAGE_URL] = image_result(make_image())
    body = post_import(auth_client, PAGE_URL).text
    photo = list(settings.import_dir.glob("*.webp"))[0].name
    response = auth_client.post("/recipes/new", data={
        "csrf_token": csrf_from(body), "name": "", "category": "weeknight", "imported_photo": photo,
    })
    assert response.status_code == 422
    assert f'name="imported_photo" value="{photo}"' in response.text
    assert (settings.import_dir / photo).exists()


def test_discard_imported_photo(auth_client, settings, fake_web):
    fake_web[PAGE_URL] = html_result(GRAPH_PAGE)
    fake_web[IMAGE_URL] = image_result(make_image())
    body = post_import(auth_client, PAGE_URL).text
    photo = list(settings.import_dir.glob("*.webp"))[0].name
    response = auth_client.post("/recipes/new", data={
        "csrf_token": csrf_from(body), "name": "No Photo Please", "category": "weeknight",
        "imported_photo": photo, "discard_imported_photo": "on",
    })
    row = connect(settings.database_path).execute(
        "SELECT photo_filename FROM recipes WHERE id = ?", (recipe_id_from(response),)).fetchone()
    assert row[0] is None


def test_tampered_imported_photo_name_is_ignored(auth_client, settings):
    token = csrf_from(auth_client.get("/recipes/new").text)
    response = auth_client.post("/recipes/new", data={
        "csrf_token": token, "name": "Sneaky", "category": "weeknight", "imported_photo": "../family_table.db",
    })
    assert response.status_code == 303
    assert settings.database_path.exists()
    row = connect(settings.database_path).execute("SELECT photo_filename FROM recipes").fetchone()
    assert row[0] is None


@pytest.mark.parametrize("image", [
    image_result(b"<html>not an image</html>"),
    image_result(make_image("JPEG", (400, 300))[:200]),   # truncated
    FetchError("too_large"),
    FetchError("http", 404),
])
def test_broken_photo_still_imports_recipe(auth_client, settings, fake_web, image):
    fake_web[PAGE_URL] = html_result(GRAPH_PAGE)
    fake_web[IMAGE_URL] = image
    response = post_import(auth_client, PAGE_URL)
    assert response.status_code == 200
    assert 'value="Skillet Lasagna"' in response.text
    assert "couldn&#39;t download the recipe&#39;s photo" in response.text
    assert 'name="imported_photo"' not in response.text
    assert list(settings.import_dir.glob("*.webp")) == []


@pytest.mark.parametrize("error, message", [
    (FetchError("timeout"), "took too long to respond"),
    (FetchError("http", 403), "wouldn&#39;t let us read the page"),
    (FetchError("http", 404), "doesn&#39;t exist anymore"),
    (FetchError("http", 500), "returned an error (500)"),
    (FetchError("too_large"), "too large to import"),
    (FetchError("dns"), "couldn&#39;t find that website"),
    (FetchError("network"), "couldn&#39;t load that page"),
])
def test_fetch_failures_show_friendly_errors(auth_client, settings, fake_web, error, message):
    fake_web[PAGE_URL] = error
    response = post_import(auth_client, PAGE_URL)
    assert response.status_code == 422
    assert message in response.text
    assert f'value="{PAGE_URL}"' in response.text  # the link stays in the box
    assert "Traceback" not in response.text and "FetchError" not in response.text
    assert recipe_count(settings) == 0


def test_page_without_recipe_shows_error(auth_client, fake_web):
    fake_web[PAGE_URL] = html_result("<html><body><h1>About us</h1></body></html>")
    response = post_import(auth_client, PAGE_URL)
    assert response.status_code == 422
    assert "We couldn&#39;t find a recipe on that page." in response.text


def test_non_html_link_rejected(auth_client, fake_web):
    fake_web[PAGE_URL] = FetchResult(url=PAGE_URL, content_type="application/pdf", charset=None, body=b"%PDF")
    assert "isn&#39;t a web page" in post_import(auth_client, PAGE_URL).text


@pytest.mark.parametrize("url, message", [
    ("", "Paste a link"),
    ("not a link", "doesn&#39;t look like a web address"),
    ("ftp://example.com/x", "doesn&#39;t look like a web address"),
    ("http://127.0.0.1:8000/recipes/1", "only import from public websites"),
    ("http://localhost/", "only import from public websites"),
    ("http://169.254.169.254/latest/meta-data/", "only import from public websites"),
])
def test_invalid_and_private_urls_rejected_by_real_fetcher(auth_client, url, message):
    # No stub here: these must be refused before any connection is made.
    response = post_import(auth_client, url)
    assert response.status_code == 422
    assert message in response.text


def test_temp_photo_route_requires_login_and_safe_names(auth_client, settings):
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    name = "a" * 32 + ".webp"
    (settings.import_dir / name).write_bytes(make_image("WEBP", (10, 10)))
    assert auth_client.get(f"/photos/import/{name}").status_code == 200
    assert auth_client.get("/photos/import/..%2Ffamily_table.db").status_code == 404
    auth_client.cookies.clear()
    assert auth_client.get(f"/photos/import/{name}").status_code == 303


def test_cleanup_removes_only_old_temp_photos(tmp_path):
    old, fresh = tmp_path / "old.webp", tmp_path / "fresh.webp"
    old.write_bytes(b"x"); fresh.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    cleanup_temp_photos(tmp_path)
    assert not old.exists() and fresh.exists()


# ---- Source link on manual recipes, and the migration ----

def test_manual_source_url_validation(auth_client):
    token = csrf_from(auth_client.get("/recipes/new").text)
    response = auth_client.post("/recipes/new", data={
        "csrf_token": token, "name": "Bad Link", "category": "weeknight", "source_url": "javascript:alert(1)",
    })
    assert response.status_code == 422
    assert "should start with http:// or https://" in response.text


def test_migration_adds_source_url_and_keeps_old_recipes(tmp_path):
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    old.executescript("""
        CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, cuisine TEXT NOT NULL DEFAULT '',
            prep_minutes INTEGER, cook_minutes INTEGER, servings INTEGER, photo_filename TEXT,
            card_tag TEXT NOT NULL DEFAULT '', last_made TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO recipes (name, category) VALUES ('Grandma''s Soup', 'soups');
    """)
    old.commit(); old.close()

    init_db(db_path)
    init_db(db_path)  # running twice is harmless

    conn = connect(db_path)
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(recipes)")]
    assert columns.count("source_url") == 1
    row = conn.execute("SELECT name, source_url FROM recipes").fetchone()
    assert tuple(row) == ("Grandma's Soup", "")
