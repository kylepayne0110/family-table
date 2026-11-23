"""Case E: Cook with me in a real browser (Chromium), with SIMULATED speech recognition and synthesis.

The page's real JavaScript runs against the real server; only the microphone and the voice are
stand-ins (speech_stub.js). Physical-iPhone behavior is covered by the manual test in docs.
"""

import pytest

from tests.browser.conftest import add_recipe, next_monday, plan_through_ui, sign_in, sql

STEPS = [("Prep", "Heat the oven to 400°F. Cut 2 chicken breasts into chunks."),
         ("Dredge", "Whisk 1 cup flour with salt, then toss the chicken in it."),
         ("Coat", "Dip in egg, then coat in 2 cups breadcrumbs."),
         ("Bake", "Bake for 20 minutes, flipping once.")]
INGREDIENTS = [("2", "chicken breasts"), ("1 cup", "flour"), ("2 cups", "breadcrumbs"), ("1", "egg")]


def spoken(page):
    return page.evaluate("__speech.texts()")


def wait_spoken(page, text, timeout=10_000):
    page.wait_for_function("t => __speech.texts().includes(t)", arg=text, timeout=timeout)


def wait_listening(page):
    page.wait_for_function("() => document.querySelector('[data-cook]').dataset.state === 'listening' && __speech.listening()")


def talk(page, text):
    wait_listening(page)
    page.evaluate("t => __speech.say(t)", text)


@pytest.fixture
def cooking(new_phone, server):
    recipe = add_recipe(server, "Voice Test Nuggets", INGREDIENTS, STEPS)
    page = new_phone()
    sign_in(page)
    meal = plan_through_ui(page, next_monday(), 1, "Voice Test Nuggets")
    return page, meal, recipe


def test_cook_with_me_conversation(cooking, server):
    page, meal, recipe = cooking
    page.click("[data-cook-start]")
    assert page.is_visible("[data-cook]")
    wait_spoken(page, "How can I help?")
    assert page.evaluate("__speech.spoken[0].text") == "How can I help?"

    talk(page, "walk me through it")
    wait_spoken(page, "Step 1. Prep. Heat the oven to 400 degrees")
    assert "Step 2" not in spoken(page)
    assert page.text_content("[data-cook-step-label]") == "Step 1 of 4"
    assert page.get_attribute("#step-1", "class").find("is-current") >= 0

    talk(page, "next")
    wait_spoken(page, "Step 2. Dredge")

    # Interrupt while it's speaking: the obsolete speech stops and the new question is answered once.
    page.evaluate("__speech.autoFinish = false")
    talk(page, "next")
    wait_spoken(page, "Step 3. Coat")
    page.wait_for_function("() => document.querySelector('[data-cook]').dataset.state === 'speaking' && __speech.listening()")
    page.evaluate("__speech.say('how much flour')")
    page.wait_for_function("() => __speech.spoken.some(s => s.text.includes('Step 3') && s.cancelled)")
    page.evaluate("__speech.autoFinish = true")
    wait_spoken(page, "servings: 1 cup flour")
    assert page.text_content("[data-cook-step-label]") == "Step 3 of 4"
    asked = sql(server, "SELECT COUNT(*) FROM meal_messages WHERE meal_id = ? AND body = 'how much flour'", (meal,))
    assert asked == [(1,)]

    talk(page, "repeat")
    page.wait_for_function("() => __speech.spoken.filter(s => s.text.startsWith('Step 3')).length >= 2")
    talk(page, "read me the whole recipe")
    wait_spoken(page, "Bake for 20 minutes")
    assert page.text_content("[data-cook-step-label]") == "Step 3 of 4"

    # Per-meal scaling by voice updates the on-screen selector.
    talk(page, "make this for eight tonight")
    page.wait_for_function("() => document.querySelector('#servings-input')?.value === '8'")
    assert sql(server, "SELECT servings FROM planned_meals WHERE id = ?", (meal,)) == [(8,)]

    # Permanent edit: proposed, heard in full, confirmed by an explicit yes.
    talk(page, "use three quarters of a cup of flour in the original recipe")
    wait_spoken(page, "Do you want me to save this to the recipe?")
    page.wait_for_timeout(200)
    talk(page, "yes, save it")
    wait_spoken(page, "Saved.")
    assert sql(server, "SELECT amount FROM ingredients WHERE recipe_id = ? AND name = 'flour'", (recipe,)) == [("3/4 cup",)]

    # Cancellation leaves the recipe alone.
    talk(page, "from now on use panko")
    wait_spoken(page, "Do you want me to save this to the recipe?")
    talk(page, "no, cancel")
    wait_spoken(page, "left the saved recipe")
    assert sql(server, "SELECT COUNT(*) FROM ingredients WHERE recipe_id = ? AND name = 'panko'", (recipe,)) == [(0,)]

    # Its own voice is not taken as a command.
    before = sql(server, "SELECT COUNT(*) FROM meal_messages WHERE meal_id = ? AND role = 'user'", (meal,))[0][0]
    page.evaluate("__speech.echo = true")
    talk(page, "what step am I on")
    wait_spoken(page, "You're on step 3 of 4.")
    page.wait_for_timeout(2500)
    page.evaluate("__speech.echo = false")
    after = sql(server, "SELECT COUNT(*) FROM meal_messages WHERE meal_id = ? AND role = 'user'", (meal,))[0][0]
    assert after == before + 1

    # End releases the microphone.
    page.click("[data-cook-end]")
    page.wait_for_function("() => !__speech.listening()")
    assert page.is_hidden("[data-cook]")


def test_session_recovery_after_reload(cooking, server):
    page, meal, _ = cooking
    page.click("[data-cook-start]")
    wait_spoken(page, "How can I help?")
    talk(page, "start")
    wait_spoken(page, "Step 1.")
    talk(page, "next")
    wait_spoken(page, "Step 2.")
    page.reload()
    page.wait_for_selector("[data-cook]:not([hidden])")
    assert page.get_attribute("[data-cook]", "data-state") == "paused"
    assert "You were on step 2 of 4" in page.inner_text("[data-cook-reply]")
    assert not page.evaluate("__speech.listening()")            # never listens again without a tap
    page.click("[data-cook-orb]")
    talk(page, "repeat")
    page.wait_for_function("() => __speech.spoken.some(s => s.text.startsWith('Step 2'))")


def test_buttons_and_typing_work_without_a_microphone(new_phone, server):
    add_recipe(server, "No Mic Soup", [("1", "onion")], [("", "Chop the onion."), ("", "Simmer.")])
    page = new_phone()
    sign_in(page)
    plan_through_ui(page, next_monday(), 2, "No Mic Soup")
    page.evaluate("__speech.denyMic = true")
    page.click("[data-cook-start]")
    page.wait_for_function("() => document.querySelector('[data-cook]').dataset.state === 'blocked'")
    assert "isn't allowed" in page.inner_text("[data-cook-hint]")
    page.click("[data-cook-cue='next']")
    page.wait_for_function("() => document.querySelector('[data-cook-step-label]').textContent === 'Step 1 of 2'")
    page.fill("#cook-text", "next")
    page.press("#cook-text", "Enter")
    page.wait_for_function("() => document.querySelector('[data-cook-step-label]').textContent === 'Step 2 of 2'")


def test_lost_connection_keeps_the_place_and_retries(cooking, server):
    page, meal, _ = cooking
    page.click("[data-cook-start]")
    wait_spoken(page, "How can I help?")
    talk(page, "start")
    wait_spoken(page, "Step 1.")
    page.route("**/cook", lambda route: route.abort() if route.request.method == "POST" else route.continue_())
    talk(page, "next")
    wait_spoken(page, "lost the connection")
    assert page.is_visible("[data-cook-retry]")
    page.unroute("**/cook")
    page.click("[data-cook-retry]")
    wait_spoken(page, "Step 2.")


def test_slow_model_shows_still_thinking(cooking, server, fake_ollama):
    page, meal, _ = cooking
    fake_ollama.delay = 7
    try:
        page.click("[data-cook-start]")
        wait_spoken(page, "How can I help?")
        talk(page, "is this good with ranch")
        page.wait_for_function("() => document.querySelector('[data-cook]').dataset.state === 'thinking'")
        page.wait_for_function("() => document.querySelector('[data-cook-hint]').innerText.includes('Still thinking')",
                               timeout=9000)
        wait_spoken(page, "gentle simmer", timeout=15_000)
    finally:
        fake_ollama.delay = 0
