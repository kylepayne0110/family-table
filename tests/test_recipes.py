# sqlite3 inspects the test database directly.
import sqlite3

# TestClient lets us restart the app.
from fastapi.testclient import TestClient

# Project modules and helpers.
from app.database import connect
from app.main import create_app
from tests.conftest import TEST_NOW, csrf_from, create_recipe, login, recipe_form, recipe_id_from


def db(settings) -> sqlite3.Connection:
    return connect(settings.database_path)


def test_tables_are_created(client, settings):
    names = {row[0] for row in db(settings).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"recipes", "ingredients", "steps", "notes", "sessions"} <= names
    assert db(settings).execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_empty_box_shows_add_prompt(auth_client):
    page = auth_client.get("/").text
    assert "The box is <em>empty</em>" in page
    assert "0</span>" in page and "recipes in the box" in page


def test_create_recipe_persists(auth_client, settings):
    response = create_recipe(auth_client)
    assert response.status_code == 303
    recipe_id = recipe_id_from(response)

    row = db(settings).execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    assert row["name"] == "Chicken Tacos"
    assert row["category"] == "weeknight"
    assert row["prep_minutes"] == 15 and row["servings"] == 4
    assert row["card_tag"] == "Taco Tuesday"

    page = auth_client.get(f"/recipes/{recipe_id}").text
    assert "Chicken Tacos" in page
    assert "No. 01" in page
    assert "0 of 2 ready" in page


def test_ingredients_and_steps_keep_their_order(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(
        auth_client,
        ingredient_amount=["1", "2", "3", "4"],
        ingredient_name=["zucchini", "apple", "mango", "banana"],
        step_title=["", "Middle", ""],
        step_body=["first step", "second step", "third step"],
    ))
    conn = db(settings)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM ingredients WHERE recipe_id = ? ORDER BY position", (recipe_id,))]
    steps = [tuple(r) for r in conn.execute(
        "SELECT title, body FROM steps WHERE recipe_id = ? ORDER BY position", (recipe_id,))]
    assert names == ["zucchini", "apple", "mango", "banana"]
    assert steps == [("", "first step"), ("Middle", "second step"), ("", "third step")]

    # And the page shows them in that order too.
    page = auth_client.get(f"/recipes/{recipe_id}").text
    page = page[page.index("<body"):]   # the <head> mentions "apple" (iPhone home-screen tags)
    assert page.index("zucchini") < page.index("apple") < page.index("mango") < page.index("banana")
    assert page.index("first step") < page.index("second step") < page.index("third step")


def test_edit_recipe(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client))
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}/edit").text)
    response = auth_client.post(f"/recipes/{recipe_id}/edit", data={
        **recipe_form(name="Fish Tacos", category="kids-pick",
                      ingredient_amount=["1 lb"], ingredient_name=["cod"],
                      step_title=[""], step_body=["Fry the fish."]),
        "csrf_token": token,
    })
    assert response.status_code == 303
    assert recipe_id_from(response) == recipe_id  # id is stable

    conn = db(settings)
    row = conn.execute("SELECT name, category FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    assert tuple(row) == ("Fish Tacos", "kids-pick")
    assert [r[0] for r in conn.execute("SELECT name FROM ingredients WHERE recipe_id = ?", (recipe_id,))] == ["cod"]
    assert conn.execute("SELECT COUNT(*) FROM steps WHERE recipe_id = ?", (recipe_id,)).fetchone()[0] == 1


def test_ids_stay_stable_after_delete(auth_client):
    first = recipe_id_from(create_recipe(auth_client, name="One"))
    second = recipe_id_from(create_recipe(auth_client, name="Two"))
    token = csrf_from(auth_client.get(f"/recipes/{second}/edit").text)
    assert auth_client.post(f"/recipes/{second}/delete", data={"csrf_token": token}).status_code == 303
    third = recipe_id_from(create_recipe(auth_client, name="Three"))
    assert (first, second, third) == (1, 2, 3)  # 2 is never reused
    assert auth_client.get(f"/recipes/{second}").status_code == 404


def test_notes_can_be_added_and_shown(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client, note_author="Mom", note_body="Add extra lime."))
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}").text)
    response = auth_client.post(f"/recipes/{recipe_id}/notes",
                                data={"author": "Dad", "body": "Great with salsa verde.", "csrf_token": token})
    assert response.status_code == 303

    page = auth_client.get(f"/recipes/{recipe_id}").text
    assert "Add extra lime." in page and "~ Mom," in page
    assert "Great with salsa verde." in page and "~ Dad," in page
    count = db(settings).execute("SELECT COUNT(*) FROM notes WHERE recipe_id = ?", (recipe_id,)).fetchone()[0]
    assert count == 2


def test_blank_note_is_rejected(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client))
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}").text)
    response = auth_client.post(f"/recipes/{recipe_id}/notes", data={"author": "", "body": "  ", "csrf_token": token})
    assert response.status_code == 422
    assert "Add both the note" in response.text
    assert db(settings).execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_notes_are_html_escaped(auth_client):
    recipe_id = recipe_id_from(create_recipe(auth_client))
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}").text)
    auth_client.post(f"/recipes/{recipe_id}/notes",
                     data={"author": "Kid", "body": "<script>alert(1)</script>", "csrf_token": token})
    page = auth_client.get(f"/recipes/{recipe_id}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_mark_made_today(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client))
    assert "Not yet recorded" in auth_client.get(f"/recipes/{recipe_id}").text
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}").text)
    assert auth_client.post(f"/recipes/{recipe_id}/made", data={"csrf_token": token}).status_code == 303
    stored = db(settings).execute("SELECT last_made FROM recipes WHERE id = ?", (recipe_id,)).fetchone()[0]
    assert stored == TEST_NOW.date().isoformat()   # the household's today
    assert "Made today" in auth_client.get("/").text


def test_search_matches_name_description_and_ingredients(auth_client):
    create_recipe(auth_client, name="Beef Chili", description="Smoky and slow.",
                  ingredient_amount=["1"], ingredient_name=["kidney beans"], category="soups")
    create_recipe(auth_client, name="Pancakes", description="Fluffy Saturday breakfast.",
                  ingredient_amount=["2 cups"], ingredient_name=["buttermilk"])

    def names(query):
        page = auth_client.get("/", params={"q": query}).text
        return {n for n in ("Beef Chili", "Pancakes") if f'stretched-link">{n}<' in page}

    assert names("chili") == {"Beef Chili"}
    assert names("FLUFFY") == {"Pancakes"}
    assert names("buttermilk") == {"Pancakes"}
    assert names("beans smoky") == {"Beef Chili"}  # every word must match
    assert names("lasagna") == set()
    assert "Nothing in the box matches" in auth_client.get("/", params={"q": "lasagna"}).text
    assert names("100%") == set()  # LIKE wildcards are treated literally


def test_category_filter_and_combined_search(auth_client):
    create_recipe(auth_client, name="Tomato Soup", category="soups")
    create_recipe(auth_client, name="Tomato Pasta", category="weeknight")
    create_recipe(auth_client, name="Roast Chicken", category="sunday-dinner")

    def names(**params):
        page = auth_client.get("/", params=params).text
        return {n for n in ("Tomato Soup", "Tomato Pasta", "Roast Chicken") if f'stretched-link">{n}<' in page}

    assert names(category="soups") == {"Tomato Soup"}
    assert names(category="sunday-dinner") == {"Roast Chicken"}
    assert names(q="tomato") == {"Tomato Soup", "Tomato Pasta"}
    assert names(q="tomato", category="weeknight") == {"Tomato Pasta"}
    assert names(category="not-a-category") == {"Tomato Soup", "Tomato Pasta", "Roast Chicken"}


def test_home_shows_count_and_featured_pick(auth_client):
    create_recipe(auth_client, name="Alpha")
    create_recipe(auth_client, name="Beta")
    page = auth_client.get("/").text
    assert "2</span>" in page and "recipes in the box" in page
    assert "Tonight's pick" in page
    assert "Add a new recipe" in page


def test_invalid_form_shows_errors_and_keeps_values(auth_client, settings):
    response = create_recipe(auth_client, name="", category="dessert", prep_minutes="abc",
                             servings="0", last_made="not-a-date",
                             ingredient_amount=["2 cups"], ingredient_name=[""],
                             description="Keep me")
    assert response.status_code == 422
    for message in ["Give the recipe a name", "Choose a category", "Prep time should be",
                    "Servings should be", "real date", "also needs a name"]:
        assert message in response.text
    assert "Keep me" in response.text  # entered values are preserved
    assert db(settings).execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0


def test_optional_fields_can_be_blank(auth_client, settings):
    response = create_recipe(auth_client, description="", cuisine="", prep_minutes="", cook_minutes="",
                             servings="", card_tag="", ingredient_amount=[""], ingredient_name=[""],
                             step_title=[""], step_body=[""])
    assert response.status_code == 303
    recipe_id = recipe_id_from(response)
    page = auth_client.get(f"/recipes/{recipe_id}").text
    assert "No ingredients yet" in page and "No steps written down yet" in page


def test_recipe_form_requires_csrf(auth_client, settings):
    response = auth_client.post("/recipes/new", data={**recipe_form(), "csrf_token": "forged"})
    assert response.status_code == 403
    assert db(settings).execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0


def test_unknown_recipe_and_bad_urls_return_404(auth_client):
    assert auth_client.get("/recipes/999").status_code == 404
    assert auth_client.get("/recipes/abc").status_code == 404
    assert "Not in the box" in auth_client.get("/recipes/999").text


def test_data_survives_restart(settings):
    with TestClient(create_app(settings), follow_redirects=False) as first:
        login(first)
        recipe_id = recipe_id_from(create_recipe(first, name="Lasting Lasagna"))

    # A brand-new app instance on the same data folder (like restarting the server).
    with TestClient(create_app(settings), follow_redirects=False) as second:
        login(second)
        page = second.get(f"/recipes/{recipe_id}")
        assert page.status_code == 200
        assert "Lasting Lasagna" in page.text
