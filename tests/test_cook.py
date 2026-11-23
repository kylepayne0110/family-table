"""Cook with me: spoken turns for one planned dinner (the conversation logic behind the voice panel)."""

# json builds stand-in model replies; uuid makes request keys.
import json
import uuid

# pytest provides fixtures.
import pytest

# Project modules.
from app.cook import speakable
from tests.plan_helpers import FETCH, add_ingredients, meal_id, plan, query, token
from tests.test_meal_features import save

STEPS = [
    ("Prep", "Heat the oven to 400°F. Cut 2 chicken breasts into chunks."),
    ("Dredge", "Whisk 1 cup flour with salt. Toss the chicken in the flour."),
    ("Coat", "Dip in egg, then coat in 2 cups breadcrumbs."),
    ("Bake", "Put the tray in the oven and bake for 20 minutes, flipping once."),
]
INGREDIENTS = [("2", "chicken breasts"), ("1 cup", "flour"), ("2 cups", "breadcrumbs"), ("1", "egg"),
               ("", "salt to taste")]


@pytest.fixture
def dinner(auth_client, settings):
    recipe = save(settings, "Grandma's Chicken Nuggets", INGREDIENTS, STEPS, servings=4)
    plan(auth_client, 2, recipe)
    return auth_client, meal_id(settings, 2), recipe


def say(client, mid, text, proposal_id=None, key=None):
    data = {"csrf_token": token(client), "text": text, "request_key": key or uuid.uuid4().hex}
    if proposal_id:
        data["proposal_id"] = str(proposal_id)
    response = client.post(f"/plan/meals/{mid}/cook", data=data, headers=FETCH)
    assert response.status_code == 200, response.text
    return response.json()


def test_opens_with_how_can_i_help_and_guides_one_step_at_a_time(dinner):
    client, mid, _ = dinner
    state = client.get(f"/plan/meals/{mid}/cook", headers=FETCH).json()
    assert state["greeting"] == "How can I help?" and state["step"] == 0 and state["total"] == 4
    page = client.get(f"/plan/meals/{mid}").text
    assert "data-cook-start" in page and "Cook with me" in page

    r = say(client, mid, "Walk me through it")
    assert r["step"] == 1 and r["kind"] == "step"
    assert r["say"].startswith("Step 1. Prep. Heat the oven to 400 degrees")
    assert "Step 2" not in r["say"]                       # one step at a time
    assert say(client, mid, "next")["step"] == 2
    r = say(client, mid, "Okay, next step please")
    assert r["step"] == 3 and "breadcrumbs" in r["say"]
    assert say(client, mid, "repeat that")["step"] == 3
    assert say(client, mid, "go back")["step"] == 2
    assert say(client, mid, "what step am I on?")["say"] == "You're on step 2 of 4."
    assert say(client, mid, "skip to step four")["step"] == 4
    assert "last step" in say(client, mid, "next")["say"]


def test_interrupting_with_a_question_keeps_the_place(dinner, settings):
    client, mid, _ = dinner
    say(client, mid, "start from the beginning")
    say(client, mid, "next")
    r = say(client, mid, "How much flour?")
    assert "1 cup flour" in r["show"] and r["step"] == 2
    assert "still on step 2" in r["say"]
    assert say(client, mid, "next")["step"] == 3                  # resumes where they were


def test_whole_recipe_only_when_asked(dinner):
    client, mid, _ = dinner
    say(client, mid, "start")
    r = say(client, mid, "read me the whole recipe")
    for _, body in STEPS:
        assert body.split(".")[0] in r["show"]
    assert "Ingredients for 4 servings" in r["show"] and r["step"] == 1    # reading doesn't move the place


def test_already_halfway_through(dinner):
    client, mid, _ = dinner
    r = say(client, mid, "I'm already halfway through")
    assert r["kind"] == "clarify" and "Which step" in r["say"]
    r = say(client, mid, "I just dipped them in egg and breadcrumbs")
    assert r["step"] == 4 and "done step 3" in r["say"]
    r = say(client, mid, "I'm on step 2")
    assert r["step"] == 2


def test_make_it_for_eight_tonight_updates_the_meal_not_the_groceries(dinner, settings):
    client, mid, recipe = dinner
    add_ingredients(client, settings, 2)
    before = query(settings, "SELECT name, quantity FROM shopping_items ORDER BY id")
    say(client, mid, "start")
    r = say(client, mid, "Make this for eight tonight")
    assert r["servings"] == 8 and 'value="8"' in r["recipe"]       # the on-screen selector follows
    assert query(settings, "SELECT servings FROM planned_meals WHERE id = ?", (mid,)) == [(8,)]
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (recipe,)) == [(4,)]
    assert query(settings, "SELECT name, quantity FROM shopping_items ORDER BY id") == before
    assert "Add ingredients" in r["show"]                           # explains the explicit update flow
    r = say(client, mid, "how much flour do I need?")
    assert "2 cups flour" in r["show"]
    r = say(client, mid, "repeat")
    assert "400 degrees" in r["say"]                                # oven temperature is never scaled
    assert "4 chicken breasts" in r["say"]                          # quantities in the step follow tonight's count


def test_permanent_substitution_needs_a_heard_explicit_yes(dinner, settings):
    client, mid, recipe = dinner
    r = say(client, mid, "From now on, use panko")
    assert r["kind"] == "proposal" and r["proposal_id"]
    assert "Do you want me to save this to the recipe?" in r["say"]
    assert "breadcrumbs becomes panko" in r["say"].lower()
    pid = r["proposal_id"]
    # A yes without having heard the change (interrupted, or a stale page) never saves.
    refused = say(client, mid, "yes")
    assert "won't change the saved recipe" in refused["show"]
    assert query(settings, "SELECT name FROM ingredients WHERE recipe_id = ? AND name LIKE '%crumb%'", (recipe,)) == \
        [("breadcrumbs",)]
    # Something unrelated in between makes the earlier proposal stale for voice.
    say(client, mid, "how much flour?")
    stale = say(client, mid, "yes, save it", proposal_id=pid)
    assert "won't change" in stale["show"]
    # Read it again, then a clear yes right after hearing it.
    again = say(client, mid, "read the change again")
    assert again["kind"] == "proposal"
    r = say(client, mid, "yes, save it", proposal_id=pid)
    assert r["show"].startswith("Saved")
    assert query(settings, "SELECT name FROM ingredients WHERE recipe_id = ? AND name LIKE '%panko%'", (recipe,)) == \
        [("panko",)]
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (recipe,)) == [(4,)]
    # A repeated "yes" (echo, duplicate transcript) can't apply anything twice.
    assert "nothing waiting" in say(client, mid, "yes", proposal_id=pid)["show"]


def test_permanent_edit_can_be_cancelled(dinner, settings):
    client, mid, recipe = dinner
    r = say(client, mid, "From now on, use panko instead of breadcrumbs")
    r = say(client, mid, "no, cancel")
    assert "left the saved recipe" in r["show"]
    assert query(settings, "SELECT status FROM recipe_edit_proposals") == [("cancelled",)]
    assert query(settings, "SELECT COUNT(*) FROM ingredients WHERE recipe_id = ? AND name = 'panko'", (recipe,)) == [(0,)]


def test_quantity_edit_is_measured_against_the_original_yield(dinner, settings):
    client, mid, recipe = dinner
    say(client, mid, "make this for 8 tonight")
    r = say(client, mid, "Use ¾ cup of flour in the original recipe")
    assert r["kind"] == "proposal"
    assert "1 cup becomes 3/4 cup" in r["show"] and "Tonight's 8 servings will show 1 1/2 cups" in r["show"]
    say(client, mid, "yes", proposal_id=r["proposal_id"])
    assert query(settings, "SELECT amount FROM ingredients WHERE recipe_id = ? AND name = 'flour'", (recipe,)) == \
        [("3/4 cup",)]
    assert "Whisk 3/4 cup flour" in query(settings, "SELECT body FROM steps WHERE recipe_id = ? AND position = 1",
                                          (recipe,))[0][0]
    assert query(settings, "SELECT servings FROM recipes WHERE id = ?", (recipe,)) == [(4,)]
    assert query(settings, "SELECT servings FROM planned_meals WHERE id = ?", (mid,)) == [(8,)]


def test_scaled_amount_for_tonight_is_converted_back(dinner, settings):
    client, mid, recipe = dinner
    say(client, mid, "make this for eight")
    r = say(client, mid, "From now on use 3 cups of breadcrumbs")
    assert r["kind"] == "clarify" and "original recipe (4 servings)" in r["show"]
    r = say(client, mid, "that's for tonight's eight")
    assert r["kind"] == "proposal" and "becomes 1 1/2 cups" in r["show"]
    say(client, mid, "save it", proposal_id=r["proposal_id"])
    assert query(settings, "SELECT amount FROM ingredients WHERE recipe_id = ? AND name = 'breadcrumbs'", (recipe,)) == \
        [("1 1/2 cups",)]


def test_tonight_only_amount_changes_nothing(dinner, settings):
    client, mid, recipe = dinner
    r = say(client, mid, "use 3/4 cup flour")
    assert r["kind"] == "clarify"
    r = say(client, mid, "just tonight")
    assert "saved recipe stays the same" in r["show"]
    assert query(settings, "SELECT amount FROM ingredients WHERE recipe_id = ? AND name = 'flour'", (recipe,)) == [("1 cup",)]


def test_stale_confirmation_after_someone_edits_the_recipe(dinner, settings):
    client, mid, recipe = dinner
    from app.database import connect
    r = say(client, mid, "From now on use panko instead of breadcrumbs")
    conn = connect(settings.database_path)
    with conn:
        conn.execute("UPDATE ingredients SET name = 'fresh breadcrumbs' WHERE recipe_id = ? AND name = 'breadcrumbs'",
                     (recipe,))
    conn.close()
    r = say(client, mid, "yes", proposal_id=r["proposal_id"])
    assert "didn't change anything" in r["show"]
    assert query(settings, "SELECT status FROM recipe_edit_proposals") == [("stale",)]


def test_repeated_transcript_is_processed_once(dinner, settings):
    client, mid, _ = dinner
    key = uuid.uuid4().hex
    say(client, mid, "start", key=key)
    assert say(client, mid, "next", key=key)["kind"] == "duplicate"   # same turn arriving again
    assert client.get(f"/plan/meals/{mid}/cook", headers=FETCH).json()["step"] == 1
    assert query(settings, "SELECT COUNT(*) FROM meal_messages WHERE role = 'user'") == [(1,)]


def test_each_planned_dinner_has_its_own_session_and_it_survives_reopening(dinner, settings):
    client, mid, recipe = dinner
    plan(client, 4, recipe)
    other = meal_id(settings, 4)
    say(client, mid, "start")
    say(client, mid, "next")
    assert client.get(f"/plan/meals/{other}/cook", headers=FETCH).json()["step"] == 0
    state = client.get(f"/plan/meals/{mid}/cook", headers=FETCH).json()
    assert state["step"] == 2 and state["greeting"] == "How can I help? You were on step 2 of 4."
    page = client.get(f"/plan/meals/{mid}").text
    assert 'data-step="2"' in page and 'data-total="4"' in page
    assert "You were on step 2 of 4" in page


def test_voice_turns_join_the_typed_conversation(dinner):
    client, mid, _ = dinner
    say(client, mid, "start")
    log = client.get(f"/plan/meals/{mid}/chat", headers=FETCH).json()["log"]
    assert "start" in log and "Step 1 of 4" in log


def test_open_questions_use_the_model_with_the_current_step(dinner):
    client, mid, _ = dinner
    seen = []

    def model(messages, response_format=None):
        seen.append(messages[0]["content"])
        return json.dumps({"reply": "Golden brown and 165°F inside.", "action": "none"})
    client.app.state.ask_model = model
    say(client, mid, "start")
    say(client, mid, "next")
    r = say(client, mid, "How do I know when it's done?")
    assert r["show"] == "Golden brown and 165°F inside."
    assert "165 degrees" in r["say"]
    assert "They are on step 2 of 4" in seen[0] and "one or two short sentences" in seen[0]


def test_offline_and_malformed_model_answers_still_leave_cooking_usable(dinner):
    client, mid, _ = dinner
    r = say(client, mid, "Is this good with rice?")
    assert r["kind"] == "offline" and "can't reach" in r["show"]
    assert say(client, mid, "next")["step"] == 1                        # cues work without the model
    client.app.state.ask_model = lambda messages, response_format=None: "{not json at all"
    r = say(client, mid, "Can I use thighs?")
    assert r["show"]                                                    # a plain answer, never a crash
    client.app.state.ask_model = lambda messages, response_format=None: json.dumps(
        {"reply": "ok", "action": "propose_edit", "replacements": [{"old": "unicorn", "new": "dragon"}]})
    r = say(client, mid, "Can I change something?")
    assert r["kind"] != "proposal"                                      # nonsense edits never become proposals


def test_packaged_meal_uses_only_saved_directions(auth_client, settings):
    from app.database import connect
    from app.packaged import add_packaged_meal
    from tests.plan_helpers import WEEK, table_id
    conn = connect(settings.database_path)
    details = {"barcode": "", "name": "Frozen Lasagna", "brand": "", "package_size": "", "product_source": "manual",
               "oven_directions": "Bake at 375°F for 60 minutes.", "oven_source": "typed", "microwave_directions": "",
               "microwave_source": "", "other_directions": "", "other_source": ""}
    mid, _ = add_packaged_meal(conn, table_id(settings), WEEK, 3, details, None, None, None)
    photo_only = {**details, "name": "Mystery Pie", "oven_directions": "", "oven_source": ""}
    mid2, _ = add_packaged_meal(conn, table_id(settings), WEEK, 5, photo_only, "a" * 32 + ".webp", None, None)
    conn.close()
    r = say(auth_client, mid, "walk me through it")
    assert r["step"] == 1 and "375 degrees" in r["say"]
    r = say(auth_client, mid, "how long should it rest?")
    assert "only read out the directions saved" in r["show"]
    r = say(auth_client, mid2, "how long do I bake it?")
    assert "only in the photo of the box" in r["show"]


def test_cook_turns_need_csrf_and_membership(dinner, settings):
    client, mid, _ = dinner
    response = client.post(f"/plan/meals/{mid}/cook", data={"csrf_token": "x", "text": "next"}, headers=FETCH)
    assert response.status_code == 403


def test_speakable_reads_naturally():
    assert speakable("Add 1 1/2 tbsp oil and bake at 400°F for 20 min (until golden).") == \
        "Add 1 and a half tablespoons oil and bake at 400 degrees for 20 minutes."
    assert speakable("3/4 cup flour, 1/2 tsp salt") == "three quarters cup flour, a half teaspoons salt"
