"""Community recipes: opt-in publishing, independent copies, versions, withdrawal, reports, and ideas."""

# json builds stand-in model answers; re reads pages.
import json
import re

# pytest provides fixtures.
import pytest

# Project modules.
from app.database import connect
from tests.conftest import csrf_from, make_image
from tests.plan_helpers import FETCH, W, query, table_id
from tests.test_accounts_tables import browser, form_post, rename_my_name, signup


def create_with_photo(c, settings, **fields):
    page = c.get("/recipes/new").text
    data = {"csrf_token": csrf_from(page), "table_id": table_id(settings), "category": "kids-pick", "servings": "4",
            "step_title": ["", ""], "step_body": ["Cut the chicken into chunks.", "Bake at 400°F for 20 minutes."],
            "ingredient_amount": ["2", "1 cup"], "ingredient_name": ["chicken breasts", "breadcrumbs"],
            "creator_credit": "Grandma", "name": "Grandma's Chicken Nuggets", **fields}
    response = c.post("/recipes/new", data=data, files={"photo": ("p.jpg", make_image(), "image/jpeg")})
    assert response.status_code == 303, response.text[:500]
    return int(re.match(r"/recipes/(\d+)", response.headers["location"]).group(1))


@pytest.fixture
def paynes(auth_client, settings):
    rename_my_name(auth_client, "Kyle")
    form_post(auth_client, "/table/rename", {"name": "Payne's Table"}, page="/table")
    recipe = create_with_photo(auth_client, settings)
    form_post(auth_client, f"/recipes/{recipe}/notes", {"author": "Mom", "body": "Secret: a pinch of paprika."},
              page=f"/recipes/{recipe}")
    return auth_client, recipe


@pytest.fixture
def lees(auth_client):
    lee = browser(auth_client)
    signup(lee, "lee", "Lee")
    form_post(lee, "/tables/new", {"name": "Lee's Table"}, page="/welcome")
    return lee


def publish(c, recipe, **extra):
    page = c.get(f"/recipes/{recipe}/share").text
    data = {"creator_credit": "Grandma", "include_photo": "on",
            "recipe_version": re.search(r'name="recipe_version" value="(\d+)"', page).group(1), **extra}
    return c.post(f"/recipes/{recipe}/share/publish", data={**data, "csrf_token": csrf_from(page)})


def listing_id(settings, recipe):
    return query(settings, "SELECT id FROM community_listings WHERE source_recipe_id = ?", (recipe,))[0][0]


def test_recipes_are_private_until_shared(paynes, lees, settings):
    kyle, recipe = paynes
    assert "Grandma" not in lees.get("/community?q=nuggets").text.split("<main")[1].split("Clear search")[1]
    assert query(settings, "SELECT COUNT(*) FROM community_listings") == [(0,)]
    # The new-recipe form offers sharing as an unchecked opt-in.
    form = kyle.get("/recipes/new").text
    assert re.search(r'<input type="checkbox" name="share_community">', form)


def test_preview_shows_exactly_what_is_published(paynes, settings):
    kyle, recipe = paynes
    page = kyle.get(f"/recipes/{recipe}/share").text
    assert "Exactly what will be published" in page
    assert "Grandma&#39;s Chicken Nuggets" in page and "chicken breasts" in page
    assert 'value="Grandma"' in page and "shared by Kyle" in page
    assert "pinch of paprika" not in page                      # private notes are never shown or sent
    assert "Never shared" in page
    assert query(settings, "SELECT COUNT(*) FROM community_listings") == [(0,)]   # preview publishes nothing


def test_publish_creates_a_private_free_snapshot_once(paynes, lees, settings):
    kyle, recipe = paynes
    assert publish(kyle, recipe).headers["location"] == f"/recipes/{recipe}?msg=published"
    assert publish(kyle, recipe).status_code == 303            # a double tap or retry
    assert query(settings, "SELECT COUNT(*) FROM community_listings") == [(1,)]
    lid = listing_id(settings, recipe)
    snapshot = json.loads(query(settings, "SELECT snapshot_json FROM community_versions WHERE listing_id = ?", (lid,))[0][0])
    assert "paprika" not in json.dumps(snapshot)
    assert set(snapshot) == {"name", "description", "category", "cuisine", "prep_minutes", "cook_minutes", "servings",
                             "creator_credit", "source_url", "ingredients", "steps"}
    family_photo = query(settings, "SELECT photo_filename FROM recipes WHERE id = ?", (recipe,))[0][0]
    listing_photo = query(settings, "SELECT photo_filename FROM community_listings WHERE id = ?", (lid,))[0][0]
    assert listing_photo and listing_photo != family_photo     # its own photo file
    # Found by dish and by ingredient, with credit
    for q in ("nuggets", "breadcrumbs", "grandma"):
        results = lees.get(f"/community?q={q}").text
        assert "Grandma&#39;s Chicken Nuggets" in results, q
    assert "By Grandma" in results and "shared by Kyle" in results
    assert "Save to Lee&#39;s Table" in results
    # Lee can see the published photo but never the family's own
    assert lees.get(f"/photos/{listing_photo}").status_code == 200
    assert lees.get(f"/photos/{family_photo}").status_code == 404
    assert "pinch of paprika" not in lees.get(f"/community/{lid}").text


def test_saving_makes_an_independent_copy_and_repeats_are_safe(paynes, lees, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    lid = listing_id(settings, recipe)
    lee_table = table_id(settings, "Lee's Table")
    page = lees.get(f"/community/{lid}").text
    response = lees.post(f"/community/{lid}/save", data={"csrf_token": csrf_from(page), "table_id": lee_table,
                                                         "version": 1})
    copy = int(re.match(r"/recipes/(\d+)", response.headers["location"]).group(1))
    assert "msg=copied" in response.headers["location"]
    again = lees.post(f"/community/{lid}/save", data={"csrf_token": csrf_from(page), "table_id": lee_table, "version": 1},
                      headers=FETCH)
    assert again.json() == {"recipe_id": copy, "created": False, "table": "Lee's Table"}
    assert query(settings, "SELECT COUNT(*) FROM recipes WHERE table_id = ?", (lee_table,)) == [(1,)]
    detail = lees.get(f"/recipes/{copy}").text
    assert "Created by <strong>Grandma</strong>" in detail and "shared by Kyle" in detail
    assert "version 1" in detail
    copy_photo = query(settings, "SELECT photo_filename FROM recipes WHERE id = ?", (copy,))[0][0]
    listing_photo = query(settings, "SELECT photo_filename FROM community_listings WHERE id = ?", (lid,))[0][0]
    assert copy_photo not in (listing_photo, None)
    # Lee's edits stay in Lee's Table
    edit = lees.get(f"/recipes/{copy}/edit").text
    lees.post(f"/recipes/{copy}/edit", data={
        "csrf_token": csrf_from(edit), "table_id": lee_table, "version": re.search(r'name="version" value="(\d+)"', edit).group(1),
        "name": "Lee's Nuggets", "category": "kids-pick", "ingredient_amount": ["3"], "ingredient_name": ["chicken breasts"],
        "step_title": [""], "step_body": ["Bake."], "creator_credit": "Grandma"})
    assert query(settings, "SELECT name FROM recipes WHERE id = ?", (recipe,)) == [("Grandma's Chicken Nuggets",)]
    assert "Grandma&#39;s Chicken Nuggets" in lees.get(f"/community/{lid}").text


def test_family_edits_stay_private_until_update_community_recipe(paynes, lees, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    lid = listing_id(settings, recipe)
    lee_table = table_id(settings, "Lee's Table")
    page = lees.get(f"/community/{lid}").text
    v1_copy = int(re.match(r"/recipes/(\d+)", lees.post(f"/community/{lid}/save", data={
        "csrf_token": csrf_from(page), "table_id": lee_table, "version": 1}).headers["location"]).group(1))

    # Kyle edits the family recipe: the family copy changes at once, the community page doesn't.
    edit = kyle.get(f"/recipes/{recipe}/edit").text
    kyle.post(f"/recipes/{recipe}/edit", data={
        "csrf_token": csrf_from(edit), "table_id": table_id(settings, "Payne's Table"),
        "version": re.search(r'name="version" value="(\d+)"', edit).group(1), "name": "Grandma's Chicken Nuggets",
        "category": "kids-pick", "servings": "4", "creator_credit": "Grandma",
        "ingredient_amount": ["2", "1 cup"], "ingredient_name": ["chicken breasts", "panko"],
        "step_title": ["", ""], "step_body": ["Cut the chicken.", "Bake."]})
    assert "panko" in kyle.get(f"/recipes/{recipe}").text
    assert "panko" not in lees.get(f"/community/{lid}").text
    assert "changes that aren't in the community version" in kyle.get(f"/recipes/{recipe}").text

    # Explicit update: version 2. Lee's saved copy keeps version 1; v1 is still viewable.
    page = kyle.get(f"/recipes/{recipe}/share").text
    assert "Update community recipe" in page
    kyle.post(f"/recipes/{recipe}/share/update", data={"csrf_token": csrf_from(page), "creator_credit": "Grandma",
                                                       "include_photo": "on", "listing_version": 1})
    assert "panko" in lees.get(f"/community/{lid}").text
    assert "panko" not in lees.get(f"/community/{lid}?v=1").text
    assert query(settings, "SELECT name FROM ingredients WHERE recipe_id = ? ORDER BY position", (v1_copy,)) == \
        [("chicken breasts",), ("breadcrumbs",)]
    assert "The original has been updated since" in lees.get(f"/recipes/{v1_copy}").text
    # Saving version 2 as well gives Lee a second, separate copy.
    page = lees.get(f"/community/{lid}").text
    lees.post(f"/community/{lid}/save", data={"csrf_token": csrf_from(page), "table_id": lee_table, "version": 2})
    assert query(settings, "SELECT COUNT(*) FROM recipes WHERE table_id = ?", (lee_table,)) == [(2,)]


def test_stale_update_is_refused(paynes, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    page = kyle.get(f"/recipes/{recipe}/share").text
    kyle.post(f"/recipes/{recipe}/share/update", data={"csrf_token": csrf_from(page), "listing_version": 1})
    again = kyle.post(f"/recipes/{recipe}/share/update", data={"csrf_token": csrf_from(page), "listing_version": 1})
    assert again.status_code == 409 and "just updated" in again.text
    assert query(settings, "SELECT version FROM community_listings") == [(2,)]


def test_withdrawing_keeps_saved_copies_working(paynes, lees, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    lid = listing_id(settings, recipe)
    listing_photo = query(settings, "SELECT photo_filename FROM community_listings WHERE id = ?", (lid,))[0][0]
    page = lees.get(f"/community/{lid}").text
    copy = int(re.match(r"/recipes/(\d+)", lees.post(f"/community/{lid}/save", data={
        "csrf_token": csrf_from(page), "table_id": table_id(settings, "Lee's Table"), "version": 1}).headers["location"]).group(1))
    copy_photo = query(settings, "SELECT photo_filename FROM recipes WHERE id = ?", (copy,))[0][0]

    page = kyle.get(f"/recipes/{recipe}/share").text
    assert kyle.post(f"/recipes/{recipe}/share/withdraw", data={"csrf_token": csrf_from(page)}).status_code == 409
    kyle.post(f"/recipes/{recipe}/share/withdraw", data={"csrf_token": csrf_from(page), "confirm": "yes"})
    assert lees.get(f"/community/{lid}").status_code == 404
    assert "Grandma" not in lees.get("/community?q=nuggets").text.split("Clear search")[1]
    assert not (settings.upload_dir / listing_photo).exists()
    assert lees.get(f"/photos/{listing_photo}").status_code == 404
    # Lee's copy and its photo still work; Kyle's family copy too.
    assert lees.get(f"/recipes/{copy}").status_code == 200
    assert lees.get(f"/photos/{copy_photo}").status_code == 200
    assert "no longer shared" in lees.get(f"/recipes/{copy}").text
    assert kyle.get(f"/recipes/{recipe}").status_code == 200
    # Sharing again brings the same page back as a new version
    publish(kyle, recipe)
    assert query(settings, "SELECT status, version FROM community_listings") == [("published", 2)]


def test_only_the_tables_members_manage_its_listing(paynes, lees, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    token = csrf_from(lees.get("/community").text)
    for action in ("publish", "update", "withdraw"):
        response = lees.post(f"/recipes/{recipe}/share/{action}", data={"csrf_token": token, "confirm": "yes"})
        assert response.status_code == 404, action
    assert query(settings, "SELECT status, version FROM community_listings") == [("published", 1)]


def test_reports_hide_a_listing_until_reviewed(paynes, auth_client, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    lid = listing_id(settings, recipe)
    for n in range(3):
        person = browser(auth_client)
        signup(person, f"reporter{n}", f"Reporter {n}")
        page = person.get(f"/community/{lid}").text
        person.post(f"/community/{lid}/report", data={"csrf_token": csrf_from(page), "reason": "spam"})
        if n == 0:   # the same person reporting twice counts once
            person.post(f"/community/{lid}/report", data={"csrf_token": csrf_from(page), "reason": "spam"})
            assert query(settings, "SELECT report_count FROM community_listings") == [(1,)]
    assert query(settings, "SELECT status FROM community_listings") == [("hidden",)]
    assert person.get(f"/community/{lid}").status_code == 404
    assert "hidden while reports are reviewed" in kyle.get(f"/recipes/{recipe}").text


def test_publishing_needs_ingredients_and_csrf(paynes, settings):
    kyle, recipe = paynes
    assert kyle.post(f"/recipes/{recipe}/share/publish", data={"csrf_token": "x"}).status_code == 403
    assert query(settings, "SELECT COUNT(*) FROM community_listings") == [(0,)]


def test_community_search_pages_and_escapes_queries(paynes, lees, settings):
    kyle, _ = paynes
    for n in range(23):
        r = create_with_photo(kyle, settings, name=f"Chicken dish {n:02d}")
        publish(kyle, r)
    first = lees.get("/community?q=chicken").text
    assert first.count('class="community-card"') == 20 and "More recipes" in first
    second = lees.get("/community?q=chicken&page=2").text
    assert second.count('class="community-card"') == 3 and "Previous" in second
    for odd in ('"', "chicken OR", "NEAR(", "*", "a'b", "-x"):
        assert lees.get("/community", params={"q": odd}).status_code == 200


# ---- Ideas from what's on hand ----

def test_ideas_from_meals_made_and_scheduled(auth_client, settings):
    from tests.plan_helpers import add_recipe
    made = add_recipe(settings, "Chicken Fried Rice", [("2 cups", "rice"), ("1 lb", "chicken"), ("1", "egg")])
    scheduled = add_recipe(settings, "Broccoli Chicken Bake", [("1 lb", "chicken"), ("2 cups", "broccoli"),
                                                               ("1 cup", "cheddar")])
    add_recipe(settings, "Pancakes", [("2 cups", "flour"), ("1 cup", "milk")])
    conn = connect(settings.database_path)
    with conn:
        conn.execute("UPDATE recipes SET last_made = '2026-11-01' WHERE id = ?", (made,))
    conn.close()
    conn = connect(settings.database_path)
    with conn:   # planned last Monday (history), never marked as made
        conn.execute("INSERT INTO week_plans (table_id, week_start) VALUES (?, '2026-11-02')", (table_id(settings),))
        conn.execute("INSERT INTO planned_meals (table_id, week_start, day, recipe_id) VALUES (?, '2026-11-02', 0, ?)",
                     (table_id(settings), scheduled))
    conn.close()
    page = auth_client.get("/ideas", params={"have": "I have chicken, rice, and broccoli", "source": "familiar",
                                             "week": W, "day": 2}).text
    assert "Chicken Fried Rice" in page and "Broccoli Chicken Bake" in page and "Pancakes" not in page
    assert "You&#39;ve made this" in page and "Planned before (not marked as made)" in page
    assert "Also needs: egg" in page and "Also needs: cheddar" in page
    # Choosing one plans it for Wednesday
    token = csrf_from(page)
    auth_client.post(f"/plan/{W}/day/2", data={"csrf_token": token, "recipe_id": made, "table_id": table_id(settings)})
    assert query(settings, "SELECT recipe_id FROM planned_meals WHERE week_start = ? AND day = 2", (W,)) == [(made,)]


def test_ideas_never_show_another_tables_recipes(auth_client, lees, settings):
    lee_table = table_id(settings, "Lee's Table")
    from tests.plan_helpers import add_recipe
    add_recipe(settings, "Lee's Private Chicken", [("1 lb", "chicken")], table=lee_table)
    page = auth_client.get("/ideas", params={"have": "chicken", "source": "familiar"}).text
    assert "Lee&#39;s Private Chicken" not in page
    page = auth_client.get("/ideas", params={"have": "chicken", "source": "community"}).text
    assert "Lee&#39;s Private Chicken" not in page


def test_community_ideas_save_and_plan(paynes, lees, settings):
    kyle, recipe = paynes
    publish(kyle, recipe)
    page = lees.get("/ideas", params={"have": "chicken, breadcrumbs", "source": "community", "week": W, "day": 4}).text
    assert "Grandma&#39;s Chicken Nuggets" in page and "Save and plan for Friday" in page
    lid = listing_id(settings, recipe)
    lee_table = table_id(settings, "Lee's Table")
    response = lees.post(f"/community/{lid}/save", data={"csrf_token": csrf_from(page), "table_id": lee_table,
                                                         "version": 1, "plan_week": W, "plan_day": 4})
    assert response.headers["location"] == f"/plan?week={W}#day-4"
    planned = query(settings, "SELECT r.table_id, r.name FROM planned_meals p JOIN recipes r ON r.id = p.recipe_id "
                              "WHERE p.table_id = ?", (lee_table,))
    assert planned == [(lee_table, "Grandma's Chicken Nuggets")]


def test_new_ideas_come_from_the_model_and_lead_to_a_draft(auth_client, settings):
    prompts = []

    def model(messages, response_format=None):
        prompts.append(messages[-1]["content"])
        if "Suggest exactly 3" in messages[-1]["content"]:
            return json.dumps({"ideas": [{"name": "Chicken Broccoli Rice Bowl", "summary": "A quick bowl.",
                                          "also_needs": ["soy sauce"]},
                                         {"name": "Cheesy Chicken Rice Bake", "summary": "Baked.", "also_needs": []}]})
        return json.dumps({"name": "Chicken Broccoli Rice Bowl", "servings": 4, "prep_minutes": 10, "cook_minutes": 20,
                           "ingredients": [{"amount": "1 lb", "name": "chicken"}, {"amount": "2 cups", "name": "rice"}],
                           "steps": ["Cook the rice.", "Stir-fry the chicken and broccoli."]})
    auth_client.app.state.ask_model = model
    page = auth_client.get("/ideas", params={"have": "chicken, rice, broccoli"}).text
    response = auth_client.post("/ideas/new", data={"csrf_token": csrf_from(page), "have": "chicken, rice, broccoli",
                                                    "table_id": table_id(settings)})
    assert "AI idea" in response.text and "not an existing recipe" in response.text
    assert "Chicken Broccoli Rice Bowl" in response.text
    # The model saw only what was typed
    assert "chicken, rice, broccoli" in prompts[0]
    response = auth_client.post("/ideas/draft", data={"csrf_token": csrf_from(page), "have": "chicken, rice, broccoli",
                                                      "idea": "Chicken Broccoli Rice Bowl", "table_id": table_id(settings)})
    draft = response.headers["location"]
    assert draft.startswith("/recipes/new?draft=")
    form = auth_client.get(draft).text
    assert "AI-generated idea" in form and "Stir-fry the chicken" in form
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(0,)]       # nothing saved yet


def test_new_ideas_offline_say_so(auth_client, settings):
    page = auth_client.get("/ideas", params={"have": "chicken"}).text
    response = auth_client.post("/ideas/new", data={"csrf_token": csrf_from(page), "have": "chicken",
                                                    "table_id": table_id(settings)})
    assert "isn&#39;t answering" in response.text
