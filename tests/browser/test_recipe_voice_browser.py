"""Case F: creating a recipe by talking, in Chromium with SIMULATED speech (see speech_stub.js)."""

import json

from tests.browser.conftest import sign_in, sql


def state(page):
    return page.get_attribute("[data-recipe-voice]", "data-state")


def talk(page, text):
    page.wait_for_function("() => ['listening'].includes(document.querySelector('[data-recipe-voice]').dataset.state)"
                           " && __speech.listening()")
    page.evaluate("t => __speech.say(t)", text)


def values(page, name):
    return page.eval_on_selector_all(f'[name="{name}"]', "els => els.map(e => e.value).filter(Boolean)")


def wait_value(page, name, value):
    page.wait_for_function("([n, v]) => Array.from(document.querySelectorAll(`[name='${n}']`)).some(e => e.value === v)",
                           arg=[name, value])


def wait_spoken(page, text, timeout=12_000):
    try:
        page.wait_for_function("t => __speech.texts().includes(t)", arg=text, timeout=timeout)
    except Exception:
        print("SPOKEN:", page.evaluate("__speech.texts()"))
        print("AMOUNTS:", values(page, "ingredient_amount"), values(page, "ingredient_name"))
        print("REPLY:", page.inner_text("[data-rvoice-reply]"), "| STATE:", state(page))
        raise


def recipe_count(server):
    return sql(server, "SELECT COUNT(*) FROM recipes")[0][0]


def test_create_a_recipe_by_talking(new_phone, server):
    before = recipe_count(server)
    page = new_phone()
    sign_in(page)
    page.goto("/recipes/new")
    page.click("[data-recipe-voice-start]")
    wait_spoken(page, "Tell me about the recipe")

    talk(page, "These are Grandma's chicken nuggets. They feed four.")
    wait_value(page, "name", "Grandma's Chicken Nuggets")
    wait_value(page, "servings", "4")
    wait_value(page, "creator_credit", "Grandma")
    talk(page, "I use two chicken breasts, cut them into chunks, and coat them in breadcrumbs.")
    wait_value(page, "ingredient_name", "breadcrumbs")
    assert values(page, "ingredient_amount") == ["2"]
    assert values(page, "step_body") == ["Cut the chicken breasts into chunks.", "Coat the chicken breasts in breadcrumbs."]
    talk(page, "Actually, make that three chicken breasts.")
    wait_value(page, "ingredient_amount", "3")
    assert values(page, "ingredient_name") == ["chicken breasts", "breadcrumbs"]

    # The same sentence heard again doesn't duplicate anything.
    talk(page, "I use two chicken breasts, cut them into chunks, and coat them in breadcrumbs.")
    page.wait_for_timeout(1500)
    assert values(page, "ingredient_name") == ["chicken breasts", "breadcrumbs"]
    assert len(values(page, "step_body")) == 2

    # Undo puts back the last change.
    talk(page, "I use a little salt")
    wait_value(page, "ingredient_name", "salt")
    page.click("[data-rvoice-undo]")
    page.wait_for_function("() => !Array.from(document.querySelectorAll('[name=ingredient_name]')).some(e => e.value === 'salt')")

    talk(page, "I use 2 flour and some salt")
    wait_value(page, "ingredient_name", "flour")
    talk(page, "that's it")
    wait_spoken(page, "For the flour, is that 2 cups?")
    talk(page, "yes")
    wait_value(page, "ingredient_amount", "2 cups")
    wait_spoken(page, "How much breadcrumbs do you use?")      # missing amounts, in the order they're listed
    talk(page, "a cup and a half")
    wait_value(page, "ingredient_amount", "1 1/2 cups")
    wait_spoken(page, "How much salt do you use?")
    talk(page, "to taste")
    wait_value(page, "ingredient_amount", "to taste")
    wait_spoken(page, "how long does it take")
    talk(page, "fifteen minutes prep and twenty to cook")
    wait_value(page, "cook_minutes", "20")
    wait_spoken(page, "Any note for the card")
    talk(page, "She always doubled the batch at Christmas")
    wait_value(page, "note_body", "She always doubled the batch at Christmas")
    wait_spoken(page, "That's everything I need")

    # "Sounds good" and closing never save; only the Save button does.
    talk(page, "that sounds good")
    page.wait_for_timeout(1200)
    page.click("[data-rvoice-end]")
    assert recipe_count(server) == before
    assert page.is_visible("button:has-text('Save recipe')")
    page.click("button:has-text('Save recipe')")
    page.wait_for_url("**/recipes/*?msg=saved")
    saved = sql(server, "SELECT id, name, servings, creator_credit, prep_minutes, cook_minutes FROM recipes "
                        "ORDER BY id DESC LIMIT 1")[0]
    assert saved[1:] == ("Grandma's Chicken Nuggets", 4, "Grandma", 15, 20)
    assert sql(server, "SELECT amount, name FROM ingredients WHERE recipe_id = ? ORDER BY position", (saved[0],)) == [
        ("3", "chicken breasts"), ("1 1/2 cups", "breadcrumbs"), ("2 cups", "flour"), ("to taste", "salt")]
    assert sql(server, "SELECT author, body FROM notes WHERE recipe_id = ?", (saved[0],)) == \
        [("Kyle", "She always doubled the batch at Christmas")]
    assert sql(server, "SELECT COUNT(*) FROM recipe_drafts") == [(0,)]


def test_partly_filled_draft_keeps_typed_changes_and_cancel_saves_nothing(new_phone, server):
    page = new_phone()
    sign_in(page)
    table = sql(server, "SELECT id FROM family_tables WHERE name = \"Payne's Table\"")[0][0]
    user = sql(server, "SELECT id FROM users WHERE username = 'kyle'")[0][0]
    imported = {"name": "Best Chicken Nuggets", "category": "weeknight", "servings": "6", "source_url": "https://example.com/n",
                "ingredients": [{"amount": "1 1/2 lbs", "name": "chicken breasts"}, {"amount": "1 cup", "name": "panko"}],
                "steps": [{"title": "", "body": "Bake at 425°F for 15 minutes."}]}
    draft = sql(server, "INSERT INTO recipe_drafts (table_id, user_id, fields_json) VALUES (?, ?, ?) RETURNING id",
                (table, user, json.dumps(imported)))[0][0]
    before = recipe_count(server)
    page.goto(f"/recipes/new?draft={draft}")
    # A manual edit first, then voice: both are kept.
    page.fill("#description", "Crispy and kid-approved.")
    page.click("[data-recipe-voice-start]")
    wait_spoken(page, "already in the form")
    talk(page, "Actually make that two cups of panko")
    wait_value(page, "ingredient_amount", "2 cups")
    talk(page, "it serves four")
    wait_value(page, "servings", "4")
    assert page.input_value("#description") == "Crispy and kid-approved."
    assert values(page, "step_body") == ["Bake at 425°F for 15 minutes."]
    assert page.input_value("#source_url") == "https://example.com/n"
    # Close and leave without saving: no recipe is created, and the draft is kept for later.
    page.click("[data-rvoice-end]")
    page.wait_for_timeout(1800)          # autosave
    page.goto("/")
    assert recipe_count(server) == before
    fields = json.loads(sql(server, "SELECT fields_json FROM recipe_drafts WHERE id = ?", (draft,))[0][0])
    assert fields["servings"] == "4" and fields["description"] == "Crispy and kid-approved."
    page.goto("/recipes/new")
    assert page.is_visible("text=Pick up where you left off")


def test_voice_unavailable_still_leaves_the_form_usable(new_phone, server):
    page = new_phone(speech=False)       # a browser with no speech support at all
    page.add_init_script("delete window.SpeechRecognition; delete window.webkitSpeechRecognition;")
    sign_in(page)
    page.goto("/recipes/new")
    page.fill("#name", "Plain Typed Soup")
    page.fill('[name="ingredient_name"]', "onion")
    page.fill('[name="step_body"]', "Simmer.")
    page.click("button:has-text('Save recipe')")
    page.wait_for_url("**/recipes/*?msg=saved")
