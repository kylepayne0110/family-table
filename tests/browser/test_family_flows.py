"""Real-browser checks (Chromium, real clicks and form submissions) for Tables, sharing, ideas, and the
existing planner features. Cases B, C, D, G, H, and I from the release checklist."""

from tests.browser.conftest import add_recipe, next_monday, plan_through_ui, sign_in, sign_up, sql


def make_invite(page) -> str:
    page.goto("/table")
    page.click("button:has-text('Make an invite link'), button:has-text('Make another invite link')")
    return page.input_value("[data-invite-url]")


def switch_to(page, name):
    page.click("[data-table-switch] summary")
    page.click(f".table-switch__option:has-text(\"{name}\")")
    page.wait_for_load_state()


# ---- B. One Table means no picker; two Tables switch in place ----

def test_single_table_has_no_picker_and_two_tables_switch_on_groceries(new_phone, server):
    page = new_phone()
    sign_up(page, "solo", "Solo Cook")
    assert "/welcome" in page.url
    assert "A Table is where people share" in page.inner_text("main")
    page.click("button:has-text('Create Table')")             # the suggested "My Table"
    page.wait_for_url("**/plan*")
    for path in ("/plan", "/", "/shopping", "/community"):
        page.goto(path)
        assert page.locator("[data-table-switch]").count() == 0, path
        assert "My Table" in page.inner_text(".table-bar")

    # A second Table: now the header name opens a switcher with the current one checked.
    page.goto("/welcome")
    page.fill("#table-name", "Lake Cabin")
    page.click("button:has-text('Create another Table')")
    page.wait_for_url("**/plan*")
    page.goto("/shopping")
    page.fill("#add-name", "Firewood")
    page.click(".add-item__btn")
    page.wait_for_selector(".shop-item__name:has-text('Firewood')")
    switch_to(page, "My Table")
    assert page.url.endswith("/shopping")                        # stays on the grocery list
    page.fill("#add-name", "Milk")
    page.click(".add-item__btn")
    page.wait_for_selector(".shop-item__name:has-text('Milk')")
    assert "Firewood" not in page.inner_text("main")
    page.click("[data-table-switch] summary")
    assert page.locator(".table-switch__option.is-current").inner_text().find("My Table") >= 0
    page.click(".table-switch__option:has-text('Lake Cabin')")
    page.wait_for_selector(".shop-item__name:has-text('Firewood')")
    assert "Milk" not in page.inner_text("main")


def test_draft_open_while_switching_tables_saves_to_its_own_table(new_phone, server):
    page = new_phone()
    sign_up(page, "twotabs", "Two Tabs")
    page.click("button:has-text('Create Table')")
    page.goto("/welcome")
    page.fill("#table-name", "Second Home")
    page.click("button:has-text('Create another Table')")
    page.wait_for_url("**/plan*")
    page.goto("/recipes/new")                                    # drawn for Second Home
    page.fill("#name", "Drafted In Second Home")
    page.fill('[name="ingredient_name"]', "salt")
    page.fill('[name="step_body"]', "Season.")
    other = page.context.new_page()                              # another tab switches Tables
    other.goto("/")
    switch_to(other, "My Table")
    page.click("button:has-text('Save recipe')")
    page.wait_for_url("**/recipes/*")
    owner = sql(server, "SELECT t.name FROM recipes r JOIN family_tables t ON t.id = r.table_id "
                        "WHERE r.name = 'Drafted In Second Home'")
    assert owner == [("Second Home",)]


# ---- A + C. Invite, join, remove with an open page, rejoin; other households can't see in ----

def test_invite_remove_rejoin_and_privacy(new_phone, server):
    recipe = add_recipe(server, "Payne Family Chili", [("1 lb", "beef")], [("", "Simmer.")])
    kyle = new_phone()
    sign_in(kyle)
    kyle.goto("/account")
    kyle.fill("#display-name", "Kyle")
    kyle.click("button:has-text('Save')")
    link = make_invite(kyle)
    nat = new_phone()
    nat.goto(link)
    assert "Kyle invited you to Payne's Table" in nat.inner_text("main")
    sign_up(nat, "nat", "Nat", invite_url=link)
    assert "Payne's Table" in nat.inner_text(".table-bar")
    assert "Payne Family Chili" in nat.inner_text("main")

    # Nat has the meal plan open; Kyle removes her.
    nat.goto(f"/plan?week={next_monday()}")
    kyle.goto("/table")
    kyle.click(".plain-list__row:has-text('Nat') summary")
    assert "lose access" in kyle.inner_text(".confirm-box[open]")
    kyle.click("button:has-text('Remove Nat')")
    kyle.wait_for_url("**/table?msg=removed")
    nat.wait_for_selector("text=You no longer have access to this Table", timeout=15_000)
    assert nat.goto(f"/recipes/{recipe}").status == 404
    nat.goto(f"/plan?week={next_monday()}")
    assert "/welcome" in nat.url and "Payne Family Chili" not in nat.content()
    # Rejoining needs a new invite.
    link2 = make_invite(kyle)
    nat.goto(link2)
    nat.click("button:has-text('Join Payne')")
    nat.wait_for_url("**/?msg=joined")
    assert "Payne Family Chili" in nat.inner_text("main")

    # Another household: direct link, guessed id, image URL, search.
    sql(server, "UPDATE recipes SET photo_filename = 'dddddddddddddddddddddddddddddddd.webp' WHERE id = ?", (recipe,))
    (server["data"] / "uploads" / ("d" * 32 + ".webp")).write_bytes(b"RIFF0000WEBP")
    lee = new_phone()
    sign_up(lee, "lee", "Lee")
    lee.click("button:has-text('Create Table')")
    lee.wait_for_url("**/plan*")
    assert lee.goto(f"/recipes/{recipe}").status == 404
    assert lee.goto("/photos/" + "d" * 32 + ".webp").status == 404
    lee.goto("/?q=chili")
    assert "Payne Family Chili" not in lee.inner_text("main")
    table = sql(server, "SELECT id FROM family_tables WHERE name = \"Payne's Table\"")[0][0]
    assert lee.goto(f"/shopping?table={table}").status == 404


# ---- D + G. Publish, save into another Table, ideas ----

def test_publish_save_and_ideas(new_phone, server):
    add_recipe(server, "Sharing Test Fried Rice", [("2 cups", "rice"), ("1 lb", "chicken"), ("1", "egg")],
               [("", "Fry the rice.")])
    kyle = new_phone()
    sign_in(kyle)
    kyle.goto("/")
    kyle.click("text=Sharing Test Fried Rice")
    kyle.click("text=Share with the community")
    assert "Exactly what will be published" in kyle.inner_text("main")
    kyle.fill("#share-credit", "Uncle Ray")
    kyle.click("button:has-text('Publish to the community')")
    kyle.wait_for_url("**/recipes/*?msg=published")

    sam = new_phone()
    sign_up(sam, "sam", "Sam")
    sam.click("button:has-text('Create Table')")
    sam.wait_for_url("**/plan*")
    sam.goto("/community")
    sam.fill("#community-q", "fried rice")
    sam.press("#community-q", "Enter")
    card = sam.locator(".community-card:has-text('Sharing Test Fried Rice')")
    assert "By Uncle Ray" in card.inner_text()
    card.locator("button:has-text('Save to My Table')").click()
    sam.wait_for_url("**/recipes/*?msg=copied")
    assert "Created by Uncle Ray" in sam.inner_text("main")
    # Ideas: from Sam's own book (now including the saved copy), planned straight onto a day.
    week = next_monday()
    sam.goto(f"/plan/{week}/day/3/choose")
    sam.click("text=Get ideas from what you have")
    sam.fill("#have", "chicken, rice")
    sam.click("button:has-text('Find ideas')")
    sam.wait_for_selector(".idea-card:has-text('Sharing Test Fried Rice')")
    sam.click(".idea-card:has-text('Sharing Test Fried Rice') button:has-text('Plan for Thursday')")
    sam.wait_for_url("**/plan?week=*")
    assert "Sharing Test Fried Rice" in sam.inner_text("#day-3")


# ---- H. Existing features through their normal controls ----

def test_add_ingredients_from_an_open_planned_meal_and_groceries(new_phone, server):
    add_recipe(server, "Click Test Tacos", [("1 lb", "ground beef"), ("8", "corn tortillas"), ("1 tbsp", "butter")],
               [("", "Brown the beef."), ("", "Warm the tortillas.")])
    page = new_phone()
    sign_in(page)
    plan_through_ui(page, next_monday(), 4, "Click Test Tacos")
    # Serving stepper
    page.click("button[aria-label='One more serving']")
    page.wait_for_function("() => document.querySelector('#servings-input').value === '5'")
    assert "1 1/4 lbs" in page.inner_text("[data-planned-recipe]")
    # "Add ingredients" from the open planned meal (once broken: it posted to the page instead of the list)
    page.click("[data-checklist-open]")
    page.wait_for_selector("[data-checklist-dialog][open] [data-checklist-submit]")
    assert page.is_checked("input[name=ingredient][value='0']")
    assert not page.is_checked("input[name=ingredient][value='2']")      # butter is a staple
    page.click("[data-checklist-dialog][open] [data-checklist-submit]")
    page.wait_for_selector("[data-checklist-status]:has-text('Added')")
    page.goto(f"/shopping?week={next_monday()}")
    assert "Ground beef" in page.inner_text("main") and "1 1/4 lbs" in page.inner_text("main")
    # Mark purchased
    page.click(".shop-item:has-text('Ground beef') button.check")
    page.wait_for_function("() => document.body.innerText.includes('Purchased')")


def test_packaged_meal_typed_name_fallback(new_phone, server):
    page = new_phone()
    sign_in(page)
    page.goto(f"/plan/{next_monday()}/day/5/packaged")
    page.fill("#pk-name", "Frozen Pepperoni Pizza")
    page.fill("#pk-oven", "Bake at 425°F for 12 minutes.")
    page.click("button:has-text('Add to Saturday')")
    page.wait_for_url("**/plan/meals/*?msg=planned")
    assert "Frozen Pepperoni Pizza" in page.inner_text("main")
    page.click("[data-cook-start]")
    page.wait_for_function("() => __speech.texts().includes('How can I help?')")
    page.wait_for_function("() => __speech.listening()")
    page.evaluate("__speech.say('walk me through it')")
    page.wait_for_function("() => __speech.texts().includes('425 degrees')")


# ---- I. Ollama offline, repeated submissions ----

def test_offline_model_and_double_submit(new_phone, server, fake_ollama):
    add_recipe(server, "Offline Test Pasta", [("1 lb", "pasta")], [("", "Boil."), ("", "Drain.")])
    page = new_phone()
    sign_in(page)
    plan_through_ui(page, next_monday(), 6, "Offline Test Pasta")
    fake_ollama.down = True
    try:
        page.click("[data-assistant-open]")
        page.fill("#assistant-input", "Is this good with pesto?")
        page.press("#assistant-input", "Enter")
        page.wait_for_selector(".chat-msg--offline")
        assert "can't reach the local cooking model" in page.inner_text(".chat-msg--offline")
        page.fill("#assistant-input", "How much pasta?")          # the app still answers this itself
        page.press("#assistant-input", "Enter")
        page.wait_for_selector(".chat-msg--assistant:has-text('1 lb pasta')")
    finally:
        fake_ollama.down = False
    # The recipe editor works and a double tap on Save makes one recipe.
    before = sql(server, "SELECT COUNT(*) FROM recipes")[0][0]
    page.goto("/recipes/new")
    page.fill("#name", "Double Tap Soup")
    page.fill('[name="ingredient_name"]', "broth")
    page.fill('[name="step_body"]', "Heat.")
    page.evaluate("""() => { const b = document.querySelector('.form-actions .btn-primary'); b.click(); b.click(); }""")
    page.wait_for_url("**/recipes/*?msg=saved")
    assert sql(server, "SELECT COUNT(*) FROM recipes WHERE name = 'Double Tap Soup'") == [(1,)]
    assert sql(server, "SELECT COUNT(*) FROM recipes")[0][0] == before + 1
