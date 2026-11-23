"""Recipe creation by voice: edits to the visible form, questions after the person finishes, drafts."""

# json builds snapshots and stand-in model answers.
import json

# pytest provides fixtures.
import pytest

# Project modules.
from app.recipe_voice import clean_form, clean_voice, model_edits, turn
from tests.conftest import csrf_from, make_image
from tests.plan_helpers import FETCH, query, table_id


def apply(form, ops):
    """What the page does with the edits (mirrors recipe_voice.js)."""
    for op in ops:
        if op["op"] == "set":
            form[op["field"]] = op["value"]
        elif op["op"] == "add_ingredient":
            form["ingredients"].append({"amount": op["amount"], "name": op["name"]})
        elif op["op"] == "update_ingredient":
            row = next(r for r in form["ingredients"] if r["name"] == op["match"])
            row["amount"] = op["amount"]
        elif op["op"] == "remove_ingredient":
            form["ingredients"] = [r for r in form["ingredients"] if r["name"] != op["match"]]
        elif op["op"] == "add_step":
            form["steps"].append({"title": "", "body": op["body"]})
    return form


class Talk:
    """A conversation with the helper against a form snapshot."""

    def __init__(self, form=None, ask=None):
        self.form = clean_form(form or {"category": "weeknight"})
        self.voice = clean_voice({})
        self.ask = ask
        self.said = []

    def say(self, text):
        result = turn(text, self.form, self.voice, "fill", self.ask)
        apply(self.form, result.ops)
        self.voice = result.voice
        if result.say:
            self.said.append(result.say)
        return result

    def pause(self):
        result = turn("", self.form, self.voice, "next", self.ask)
        self.voice = result.voice
        self.said.append(result.say)
        return result


def test_grandmas_chicken_nuggets():
    t = Talk()
    for sentence in ["These are Grandma's chicken nuggets.", "They feed four.",
                     "I use two chicken breasts, cut them into chunks, and coat them in breadcrumbs."]:
        assert t.say(sentence).say == ""          # never interrupts while they're describing it
    assert t.form["name"] == "Grandma's Chicken Nuggets"
    assert t.form["creator_credit"] == "Grandma"
    assert t.form["servings"] == "4"
    assert t.form["ingredients"] == [{"amount": "2", "name": "chicken breasts"}, {"amount": "", "name": "breadcrumbs"}]
    assert [s["body"] for s in t.form["steps"]] == ["Cut the chicken breasts into chunks.",
                                                    "Coat the chicken breasts in breadcrumbs."]
    t.say("Actually, make that three chicken breasts.")
    assert t.form["ingredients"][0] == {"amount": "3", "name": "chicken breasts"}
    assert len(t.form["ingredients"]) == 2


def test_repeated_or_revised_speech_enters_things_once():
    t = Talk()
    t.say("I use two chicken breasts and a cup of flour")
    t.say("I use two chicken breasts and a cup of flour")       # heard twice
    t.say("cut them into chunks")
    t.say("Cut them into chunks.")
    assert [r["name"] for r in t.form["ingredients"]] == ["chicken breasts", "flour"]
    assert len(t.form["steps"]) == 1


def test_questions_come_one_at_a_time_after_finishing():
    t = Talk()
    t.say("These are Grandma's chicken nuggets. They feed four.")
    t.say("I use two chicken breasts, a little salt, and 2 flour. Cut them into chunks and coat them in breadcrumbs.")
    first = t.say("that's it")
    assert first.say == "For the flour, is that 2 cups?"          # unclear units first
    t.say("yes")
    assert t.form["ingredients"][2]["amount"] == "2 cups"
    assert t.said[-1] == "How much salt do you use? You can say “to taste”."
    t.say("to taste")
    assert next(r for r in t.form["ingredients"] if r["name"] == "salt")["amount"] == "to taste"
    assert t.said[-1] == "How much breadcrumbs do you use?"
    t.say("about a cup and a half")
    t.say("one and a half cups")
    assert next(r for r in t.form["ingredients"] if r["name"] == "breadcrumbs")["amount"] == "1 1/2 cups"
    assert "how long" in t.said[-1]
    t.say("skip")
    assert t.said[-1].startswith("Any note for the card")              # name, servings, credit already known
    t.say("She always doubled the batch for Christmas.")
    assert t.form["note_body"] == "She always doubled the batch for Christmas."
    assert t.said[-1].startswith("That's everything I need")
    assert "Nothing is saved until you do" in t.said[-1]
    # Nothing it already knows is asked again
    asked = " ".join(t.said)
    assert "called" not in asked and "serve" not in asked and "Who created" not in asked


def test_i_dont_measure_the_salt_offers_to_taste():
    t = Talk()
    t.say("I use salt and pepper")
    t.say("I don't measure the salt")
    t.pause()
    assert t.said[-1] == "No problem. Should I write the salt as “to taste”?"
    t.say("yes please")
    assert t.form["ingredients"][0] == {"amount": "to taste", "name": "salt"}


def test_creator_credit_and_skip():
    t = Talk({"name": "Chili", "ingredients": [{"amount": "1 lb", "name": "beef"}],
              "steps": [{"title": "", "body": "Simmer."}], "servings": "6", "prep_minutes": "10"})
    t.pause()
    assert t.said[-1] == "Who created this recipe? You can say skip."
    t.say("it was my aunt sue")
    assert t.form["creator_credit"] == "Aunt Sue"
    t.say("skip")          # the note question
    assert t.said[-1].startswith("That's everything")


def test_chatter_and_ending_never_save_or_change_anything():
    t = Talk({"name": "Soup"})
    for text in ("that sounds good", "great", "thanks", "okay"):
        result = t.say(text)
        assert result.ops == [] and result.say == ""


def test_undo():
    result = turn("scratch that", clean_form({}), clean_voice({}), "fill")
    assert result.ops == [{"op": "undo"}]


def test_imported_fields_are_corrected_quickly():
    imported = {"name": "Best Ever Chicken Nuggets", "servings": "6",
                "ingredients": [{"amount": "1 1/2 lbs", "name": "chicken breasts"}, {"amount": "1 cup", "name": "panko"}],
                "steps": [{"title": "", "body": "Bake at 425°F."}]}
    t = Talk(imported)
    t.say("Actually make that two cups of panko")
    t.say("call it Grandma's nuggets")
    t.say("it serves four")
    assert t.form["ingredients"][1] == {"amount": "2 cups", "name": "panko"}
    assert t.form["name"] == "Grandma's Nuggets"
    assert t.form["servings"] == "4"
    assert t.form["steps"] == [{"title": "", "body": "Bake at 425°F."}]       # kept as imported


def test_typed_name_is_not_overwritten_by_a_passing_mention():
    t = Talk({"name": "Mom's Sunday Roast"})
    t.say("This is my favorite roast beef")
    assert t.form["name"] == "Mom's Sunday Roast"


def test_model_edits_are_checked_against_what_was_said():
    form = clean_form({"name": "Stew", "ingredients": [{"amount": "1 lb", "name": "beef"}]})

    def model(messages, response_format=None):
        return json.dumps({
            "ingredients": [{"amount": "3", "name": "carrots"},             # said
                            {"amount": "2 cups", "name": "red wine"},       # invented
                            {"amount": "a squeeze", "name": "lemon"}],      # said, odd amount kept as said
            "steps": ["Brown the beef in batches.", "Deglaze with brandy."],  # one said, one invented
            "servings": 8,                                                  # invented number
            "cook_minutes": None})
    edits = model_edits(model, "toss in three carrots and a squeeze of lemon, then brown the beef in batches",
                        form, clean_voice({}))
    assert {"op": "add_ingredient", "amount": "3", "name": "carrots"} in edits.ops
    assert {"op": "add_ingredient", "amount": "a squeeze", "name": "lemon"} in edits.ops
    assert {"op": "add_step", "body": "Brown the beef in batches."} in edits.ops
    flat = json.dumps(edits.ops)
    assert "red wine" not in flat and "brandy" not in flat and "servings" not in flat


@pytest.mark.parametrize("raw", ["not json", "[]", '{"ingredients": "no"}', '{"ingredients": [1, null, {"x": 1}]}',
                                 "<think>hmm</think>{\"steps\": [null, 5]}", '{"servings": "lots"}'])
def test_malformed_model_output_changes_nothing(raw):
    edits = model_edits(lambda m, f=None: raw, "something long that the patterns do not cover at all",
                        clean_form({}), clean_voice({}))
    assert edits.ops == []


def test_note_on_the_edit_form_points_to_the_recipe_page():
    result = turn("add a note: double the garlic", clean_form({"editing": True}), clean_voice({}), "fill")
    assert result.ops == [] and "recipe page" in result.say


# ---- Routes ----

def voice(client, settings, text="", mode="fill", form=None, state=None):
    page = client.get("/recipes/new").text
    response = client.post("/recipes/voice", data={
        "csrf_token": csrf_from(page), "table_id": table_id(settings), "mode": mode, "text": text,
        "form": json.dumps(form or {}), "voice": json.dumps(state or {})}, headers=FETCH)
    assert response.status_code == 200, response.text
    return response.json()


def test_voice_route_returns_edits_and_never_saves(auth_client, settings):
    data = voice(auth_client, settings, "These are Grandma's chicken nuggets. They feed four.")
    assert {"op": "set", "field": "servings", "value": "4"} in data["ops"]
    data = voice(auth_client, settings, "that sounds good")
    assert data["ops"] == []
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(0,)]


def test_voice_route_needs_csrf_and_bounds_input(auth_client, settings):
    assert auth_client.post("/recipes/voice", data={"csrf_token": "x", "text": "hi"}).status_code == 403
    page = auth_client.get("/recipes/new").text
    too_big = auth_client.post("/recipes/voice", data={"csrf_token": csrf_from(page), "form": "x" * 200_000})
    assert too_big.status_code == 413


def test_drafts_save_restore_and_refuse_stale_writes(auth_client, settings):
    page = auth_client.get("/recipes/new").text
    token = csrf_from(page)
    fields = {"name": "Half-told Lasagna", "ingredients": [{"amount": "1 lb", "name": "noodles"}], "steps": []}
    created = auth_client.post("/recipes/drafts", data={"csrf_token": token, "table_id": table_id(settings),
                                                        "fields": json.dumps(fields), "voice": "{}"}).json()
    did = created["id"]
    saved = auth_client.post(f"/recipes/drafts/{did}", data={"csrf_token": token, "rev": 1, "voice": "{}",
                                                             "fields": json.dumps({**fields, "servings": "6"})}).json()
    assert saved["rev"] == 2
    stale = auth_client.post(f"/recipes/drafts/{did}", data={"csrf_token": token, "rev": 1, "fields": "{}"})
    assert stale.status_code == 409 and stale.json()["conflict"]
    # Coming back to Add a recipe offers the unsaved draft, and opening it restores the form.
    assert "Pick up where you left off" in auth_client.get("/recipes/new").text
    restored = auth_client.get(f"/recipes/new?draft={did}").text
    assert 'value="Half-told Lasagna"' in restored and 'value="noodles"' in restored and 'value="6"' in restored
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(0,)]       # a draft is never a recipe
    # Saving the recipe removes the draft.
    auth_client.post("/recipes/new", data={"csrf_token": token, "table_id": table_id(settings), "draft_id": did,
                                           "name": "Lasagna", "category": "weeknight",
                                           "ingredient_amount": ["1 lb"], "ingredient_name": ["noodles"],
                                           "step_title": [""], "step_body": ["Bake."]})
    assert query(settings, "SELECT COUNT(*) FROM recipe_drafts") == [(0,)]
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(1,)]


def test_someone_elses_draft_is_not_available(auth_client, settings):
    from tests.test_accounts_tables import browser, form_post, signup
    page = auth_client.get("/recipes/new").text
    did = auth_client.post("/recipes/drafts", data={"csrf_token": csrf_from(page), "table_id": table_id(settings),
                                                    "fields": json.dumps({"name": "Private"}), "voice": "{}"}).json()["id"]
    other = browser(auth_client)
    signup(other, "lee", "Lee")
    form_post(other, "/tables/new", {"name": "Lee's Table"}, page="/welcome")
    assert other.get(f"/recipes/new?draft={did}").status_code == 404
    token = csrf_from(other.get("/recipes/new").text)
    assert other.post(f"/recipes/drafts/{did}", data={"csrf_token": token, "fields": "{}"}).status_code == 404


def test_draft_photo_is_private_and_kept_with_the_recipe(auth_client, settings):
    from tests.test_accounts_tables import browser, signup
    page = auth_client.get("/recipes/new").text
    token = csrf_from(page)
    uploaded = auth_client.post("/recipes/photo", data={"csrf_token": token, "table_id": table_id(settings)},
                                files={"photo": ("p.jpg", make_image(), "image/jpeg")}).json()
    filename = uploaded["filename"]
    assert auth_client.get(uploaded["url"]).status_code == 200
    other = browser(auth_client)
    signup(other, "lee", "Lee")
    assert other.get(uploaded["url"]).status_code == 404
    response = auth_client.post("/recipes/new", data={
        "csrf_token": token, "table_id": table_id(settings), "name": "Photo Pie", "category": "weeknight",
        "imported_photo": filename, "ingredient_amount": [""], "ingredient_name": ["pie"],
        "step_title": [""], "step_body": ["Bake."]})
    assert response.status_code == 303
    assert query(settings, "SELECT photo_filename FROM recipes WHERE name = 'Photo Pie'") == [(filename,)]
    # Someone else's temp photo name can't be attached to a recipe.
    other_token = csrf_from(other.get("/welcome").text)
    from tests.test_accounts_tables import form_post
    form_post(other, "/tables/new", {"name": "Lee's Table"}, page="/welcome")
    second = auth_client.post("/recipes/photo", data={"csrf_token": token, "table_id": table_id(settings)},
                              files={"photo": ("p.jpg", make_image(), "image/jpeg")}).json()["filename"]
    other_token = csrf_from(other.get("/recipes/new").text)
    other.post("/recipes/new", data={"csrf_token": other_token, "name": "Stolen", "category": "weeknight",
                                     "imported_photo": second, "ingredient_amount": [""], "ingredient_name": ["x"],
                                     "step_title": [""], "step_body": ["x"]})
    assert query(settings, "SELECT photo_filename FROM recipes WHERE name = 'Stolen'") == [(None,)]


def test_ai_idea_in_voice_needs_a_choice_before_filling(auth_client, settings):
    def model(messages, response_format=None):
        content = messages[-1]["content"]
        if "Suggest exactly 3" in content:
            return json.dumps({"ideas": [{"name": "Lemon Chicken Rice", "summary": "Bright.", "also_needs": []},
                                         {"name": "Chicken Fried Rice", "summary": "Quick.", "also_needs": ["egg"]}]})
        return json.dumps({"name": "Chicken Fried Rice", "servings": 4, "ingredients": [{"amount": "2 cups", "name": "rice"}],
                           "steps": ["Fry the rice."]})
    auth_client.app.state.ask_model = model
    data = voice(auth_client, settings, "Help me come up with something with chicken and rice")
    assert data["ops"] == [] and "Which one sounds good?" in data["say"]
    assert "Lemon Chicken Rice" in data["say"]
    data = voice(auth_client, settings, "the second one", state=data["voice"],
                 form={"name": "", "ingredients": [], "steps": []})
    assert {"op": "mark_ai"} in data["ops"]
    assert {"op": "set", "field": "name", "value": "Chicken Fried Rice"} in data["ops"]
    assert "AI suggestion" in data["say"]
    assert query(settings, "SELECT COUNT(*) FROM recipes") == [(0,)]
