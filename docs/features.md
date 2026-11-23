# How Family Table works

Detailed notes on the main features. The [README](../README.md) has the overview and setup.

- [Accounts and Tables](#accounts-and-tables)
- [Recipes](#recipes)
- [Importing recipes from websites](#importing-recipes-from-websites)
- [Meal Planner and Shopping List](#meal-planner-and-shopping-list)
- [Planned dinners: servings and the Cooking Assistant](#planned-dinners-servings-and-the-cooking-assistant)
- [Cook with me (voice)](#cook-with-me-voice)
- [Tell me the recipe (voice on the recipe form)](#tell-me-the-recipe-voice-on-the-recipe-form)
- [Packaged meals](#packaged-meals)
- [Community recipes](#community-recipes)
- [Ideas from what you have](#ideas-from-what-you-have)
- [Where voice is processed](#where-voice-is-processed)
- [Sharing a list between phones](#sharing-a-list-between-phones)
- [iPhone app (Progressive Web App)](#iphone-app-progressive-web-app)
- [Security](#security)
- [Settings](#settings-env)
- [Data and backups](#data-and-backups)
- [Known limitations](#known-limitations)

## Accounts and Tables

Everyone signs in with their own account. A **Table** is where people share recipes, dinners, and groceries: each Table has its own recipe book, meal plan, grocery list, Kitchen Staples, and history. Most families use one Table; an account can be in several (a lake house, a parent's kitchen).

- **Starting**: **Create an account**, then name a Table (suggested: "My Table") and start using it right away. Inviting people is optional.
- **Inviting**: tap the Table name in the header → **Make an invite link** and send it to one person. They see "Kyle invited you to Payne's Table"; the link survives creating an account or signing in, and joins them straight away. Links work once, expire after 7 days, can be cancelled, and are stored only as a hash. Opening a used, expired, or cancelled link explains what happened.
- **Everyone is equal**: every member can rename the Table, invite people, and remove members.
- **Removing someone** (with an explained confirmation) ends their access everywhere at once: recipes, photos, the plan, the list, cooking conversations, and pages they already have open. Their account, their other Tables, and the recipes they added here stay.
- **Leaving**: the last member can't leave, so a Table is never left with nobody to look after it.
- **The header** shows the active Table. With one Table it opens straight into the app (the name links to Table settings); with several, tapping the name opens a switcher with the current one checked, and switching keeps you in the same section. The app remembers your last Table and falls back to another one if your access changed. Pages always send the Table they were drawn for, so a tab left open on one Table can never change another.
- **Recipes** record who added them separately from who created them ("Created by Grandma · Added by Kyle").
- **Account**: change your name or password, see signed-in devices, and sign out one or all of the others. A new password signs out the other devices.

## Recipes

- Home page with today's date, recipe count, a daily **Tonight's pick**, category filters, and search across names, descriptions, labels, and ingredients.
- Recipe cards with photos, card numbers (`NO. 01`), total time, last-made date, and an optional handwritten-style tag.
- Recipe page with a prep/cook/servings strip, a large photo, an ingredient checklist with a live "2 of 10 ready" counter, numbered steps, and a **We made it today** button.
- **Notes from the kitchen**: short notes on each recipe, signed by name.
- Add, edit, and delete recipes with photo upload, adjustable ingredient and step rows, and server-side validation.
- Photos are checked by their real content (not the file extension), rotated upright, resized, and re-encoded as WebP by Pillow. Re-encoding strips EXIF/GPS metadata.

## Importing recipes from websites

On **Add recipe**, paste a link into **Import from a website**. Family Table downloads the page, reads its recipe data, and opens the normal Add Recipe form pre-filled. Nothing is saved until you click **Save recipe**.

1. **Download** (`app/safe_fetch.py`): one request, with redirects followed manually (at most 5). Pages are capped at 5 MB and photos at `MAX_UPLOAD_MB`. A request gets 5 s to connect, 10 s of silence, and 20 s in total.
2. **Extract** (`app/recipe_import.py`): `recipe-scrapers` reads site-specific and schema.org recipe data from the downloaded HTML. It never downloads anything itself. A small JSON-LD reader fills gaps and keeps section and step titles (`@graph`, `HowToSection`, `HowToStep`). Extractors are a plain list, so another one could be added later.
3. **Map to the form**: ingredient lines are split into amount and name (`2 cups (240g)` / `flour`). Times and servings are filled only when the page states them, and notes explain anything left blank. Nothing is invented.
4. **Photo**: the recipe's own image is downloaded and processed by the same pipeline as uploads. It waits in `data/import-tmp/` and moves to `data/uploads/` only when the recipe is saved. Unsaved import photos are deleted after 6 hours.

**SSRF protection.** Only `http`/`https` on ports 80 and 443 are allowed. Every resolved address must be public: loopback, private, link-local (including cloud metadata `169.254.169.254`), CGNAT, multicast, and reserved ranges are refused, as are IPv4-mapped, 6to4, and NAT64 IPv6 forms. Each redirect hop is checked again, and the connection goes to the exact IP that was validated, which defeats DNS rebinding. HTTPS certificates are still verified against the real hostname. No JavaScript from the page runs. The endpoint requires sign-in and a CSRF token.

## Meal Planner and Shopping List

1. **Meal Planner**: tap **Add dinner** on a day and pick a recipe. Drag the ⋮⋮ handle to move a dinner (mouse or touch); dropping it on another dinner swaps them. A **Move** menu offers the same thing without dragging. Past and future weeks are available.
2. **Past dinners**: a dinner stays on its day through 11:59 p.m. in the household's time zone (`HOUSEHOLD_TIMEZONE`, default America/Chicago). After midnight its slot is clear and it's listed under **Earlier this week**. The planned dinner, the recipe, and anything already on the shopping list are all kept. A planner left open overnight refreshes itself when the phone wakes up. Days that are over can't be changed.
3. **Add ingredients** on a planned dinner opens a short checklist with that dinner's ingredients at its current serving count. Regular ingredients start checked; Kitchen Staples and anything that dinner already added start unchecked. The button shows the count ("Add 3 items") and is disabled with nothing checked. Only the checked ingredients of that one dinner are added; everything already on the list stays. Each checklist carries a one-time request key, so a double tap or a retried request adds nothing the second time, and adding the same dinner again replaces that dinner's earlier amounts instead of stacking them.
4. **Shopping List** has three states: **Need to Buy**, **Already have**, and **Purchased**. Kitchen Staples sit in their own collapsed section. **Add an item...** covers anything else (paper towels, bananas). Each week keeps its own list.

### How ingredients are combined

`app/ingredients.py` is plain, deterministic Python with no database access and no AI.

- **Names** are normalized conservatively: case, plurals, accents, notes in parentheses, and preparation words ("finely chopped", "melted") are ignored, and a few synonyms are mapped ("kosher salt" counts as salt, "garlic cloves" as garlic). Words that change the product are kept, so garlic and garlic powder, dried and fresh herbs, and crushed and plain tomatoes stay separate.
- **Quantities** are added with exact fractions. Units convert only within a family (3 tsp = 1 tbsp, 16 oz = 1 lb). Volume and weight are never mixed. Ranges ("2-3 cups") and anything unclear are kept exactly as written.
- **Sources**: every item remembers which recipes need it and how much, for example "Ground beef 3 lbs, from Taco Bowls (1 lb) and Burgers (2 lbs)".

### Kitchen Staples

A shared list of things the household always has (salt, butter, olive oil). In a dinner's **Add ingredients** checklist, staples start unchecked, so they stay off the list unless you tick them. **We always have this** on a list item makes it a staple and moves it to the Kitchen Staples section, and one tap adds a staple back when it runs out.

### When the plan changes

The list only changes when someone adds to it. Removing or moving a dinner leaves its groceries on the list. If another dinner needs more of something that was already purchased (or cleared with **Clear purchased**), the item goes back to **Need to Buy** with a note ("Need more: was 3 lbs, now 4 lbs"). The old whole-week **Generate / Update Shopping List** button has been replaced by per-dinner **Add ingredients**.

**Clear purchased** hides everything already bought. Cleared items are remembered for that week, so adding the same dinner again doesn't bring them back unless more is now needed.

## Planned dinners: servings and the Cooking Assistant

Tap a planned dinner's name to open it at that dinner's serving count.

### Servings

- **Cooking for** starts at the recipe's own yield. −/+ or typing a number updates the amounts immediately, and the choice is remembered for that planned dinner only; **Reset** goes back. The saved recipe never changes.
- Amounts are always calculated from the recipe's original text and the ratio (wanted ÷ recipe yield), so changing back and forth never drifts. Numbers are shown as kitchen fractions (`1 1/8 lbs`, `3/8 tsp`); spoon amounts are re-expressed when that reads better (8 × 1 tbsp = `1/2 cup`). Descriptions like "salt to taste" stay as written.
- In the steps, a quantity changes only when it equals an ingredient's amount, uses the same unit, and is followed by that ingredient's name ("Toss in 2 cups breadcrumbs" → "4 cups breadcrumbs"). Oven temperatures, times, pan sizes, and anything unclear stay as written.
- Older recipes without a serving count ask once how many servings they make before scaling.
- The **Add ingredients** checklist and the assistant use the same scaled amounts. Changing servings after adding groceries leaves the list alone; the checklist offers **Update list to N servings** for that dinner if you want it.

### Cooking Assistant

A chat panel for the open dinner, full screen on phones (**Back to recipe** returns to the same place) and beside the recipe on wide screens. Each planned dinner has its own saved conversation, so it resumes after the phone locks, and the same recipe planned twice has two separate conversations. Type, or tap the microphone where the browser supports speech recognition; if microphone access is denied, typing still works.

The app owns the calculations and every database write. These requests are understood by the app itself, so they work even when the model is offline:

- **Unit conversions** ("How many cups are in one US quart?"). Weight ↔ volume asks which ingredient it is, and answers only for ingredients with a dependable figure (flour, sugar, butter, milk…, never salt).
- **Serving changes** ("Make this for eight tonight") use the same function as the serving selector and update the visible recipe at once, for this dinner only.
- **Ingredient amounts** ("How much chicken do I need tonight?") from this dinner's scaled list.
- **Permanent edits** ("From now on, use panko instead of breadcrumbs", "use 3/4 cup of flour in the original recipe"): the app shows the exact changes to the saved recipe, including affected steps, with **Confirm** and **Cancel**. Amount changes are measured against the saved recipe's yield, and the serving count stays as saved, so tonight's 8 servings never changes a 4-serving recipe. A proposal goes stale (and saves nothing) if the recipe was edited meanwhile. Without "from now on" or similar, the assistant asks one short question: for good, or just tonight?

Everything else goes to the local Ollama model (`OLLAMA_MODEL`, default `qwen3:1.7b`) with the recipe name, yields, tonight's scaled ingredients, and the scaled steps in the prompt. There is no RAG, embeddings, or vector database. The model replies in JSON and may only *propose* an action (change servings, propose an edit, ask a question); the app validates it before doing anything. If Ollama is offline, the assistant says so and the recipe, serving selector, and checklist keep working.

## Cook with me (voice)

**Cook with me** is on the recipe you open from a planned dinner (and on packaged meals), next to **Cooking Assistant**. One tap starts a two-way spoken conversation; it opens with "How can I help?".

- **Guiding**: "walk me through it" starts at step one and reads **one step at a time**, then waits: "next", "repeat that", "go back", "I'm on step 4", "what step am I on?". "I'm already halfway" asks which step (or understands "I just put it in the oven"). The whole recipe is read only when asked ("read me the whole recipe").
- **Questions keep your place**: "How much flour?" is answered at tonight's serving count and the step stays where it was.
- **Tonight's servings**: "make this for eight tonight" changes this dinner and the selector on screen right away. Groceries already added change only through **Add ingredients → Update list**. Oven temperatures and times are never scaled.
- **Permanent edits**: "from now on, use panko" or "use 3/4 cup of flour in the original recipe" shows the exact changes (and affected steps), reads them out, and asks. Amounts are measured against the saved recipe's own yield: if tonight is scaled, it asks whether the amount is for the original or for tonight and converts. Nothing is saved without an explicit yes or **Confirm**.
- **Safe spoken confirmation**: a spoken yes counts only if it's a short, clear "yes, save it" given right after the phone finished reading that exact change, while it's still pending and the recipe hasn't changed. An interrupted read-out, a repeated transcript, or a late or stale yes never saves.
- **Hands busy**: replies are short; tap the circle (or just talk) to interrupt; **Back / Repeat / Next** buttons and a text box work at any time; **Mute** turns the microphone off and **End** releases it.
- **Saved with the dinner**: the conversation (it's the same one as the typed Cooking Assistant) and your step are saved, so reopening the dinner picks up where you were. The same recipe planned twice has two sessions. After a reload, lock screen, or app switch, a tap resumes (phones require it).
- **Packaged meals**: it reads only the directions the app has as text, and points to the box photo when that's all there is.

## Tell me the recipe (voice on the recipe form)

**Tell me the recipe** is at the top of Add a recipe and Edit recipe. It works on a blank form and on one already filled by **Import from a website**, an AI idea, or typing, and keeps everything already there, including a chosen photo.

- **The form is the source of truth.** As you talk, the visible form fills in: name, who created it, servings, ingredients with amounts, steps in the order you describe them, times, notes. Typing and talking can be mixed. **Undo** puts back the last change.
- **Corrections** apply at once: "Actually, make that three chicken breasts" changes the same ingredient, including ones that came from an import. Something heard twice (or revised by the speech recognizer) is entered once.
- **No interruptions**: it never asks a question while you're mid-thought. After you say "that's it" (or pause for a few seconds) it clarifies anything uncertain ("For the flour, is that 2 cups?") and then asks about missing things one at a time: amounts (offering "to taste"), steps, servings, times, who created it, and a note. Say "skip" for anything optional; it won't ask again about anything already filled in.
- **Inventing together**: "help me come up with something with chicken and rice" offers three ideas; pick one and it fills a starting point, clearly marked as an AI suggestion.
- **Nothing is saved by talking.** Closing the helper, losing the connection, or saying "sounds good" leaves an unsaved draft. The recipe joins the Table's book only when you press **Save**.
- **Drafts**: Add/Edit recipe saves a draft as you work (including a chosen photo, visible only to you) and offers it again if you come back. If the same draft is open in two places, the second one is saved separately instead of overwriting.
- Clear speech is understood by the app itself. Anything else may go to the local model, whose suggestions are accepted only for real form fields and only when they match what you said; if the model is off, the patterns still work and you can type.

## Where voice is processed

- **Speech to text** happens in the phone's browser, using its built-in speech recognition: on iPhone that's Apple's dictation service (on the phone or Apple's servers, depending on the device and settings); in Chrome it's Google's. Family Table receives only the recognized **text**, never audio, and stores no audio.
- **Spoken replies** use the phone's built-in voice.
- **Understanding and answers** come from the app itself and the local model (Ollama on your computer). No new outside or paid service is involved.
- Browsers without speech recognition still get spoken replies where possible, plus typing and the step buttons. Problems like a blocked microphone are reported to the server log as short codes (never content).

## Packaged meals

For a store-bought dinner (a frozen meal from Walmart, say). Open a day in the Meal Planner and tap **Scan a packaged meal** (also on the recipe picker, and **Scan instead** on a planned day).

1. **Find the product.** **Scan the barcode** uses the phone camera; the browser's built-in barcode reader is used where it exists (Chrome on Android) and the vendored ZXing library elsewhere (iPhone Safari). **Take a photo of the barcode** reads it from a still photo. You can also type the digits under the barcode, or skip it and type the meal's name. Digits are checked (UPC-A, EAN-13, EAN-8, and UPC-E, which is expanded to UPC-A).
2. **Lookup.** The server (never the browser) asks [Open Food Facts](https://world.openfoodfacts.org) for that barcode: free, no key or account. The name, brand, and package size are shown so a wrong match is easy to spot, and every field can be corrected. Unknown barcodes and lookup outages still let you add the meal by name. Successful lookups are cached in memory for a day.
3. **Directions.** Only the package's own words are used. If Open Food Facts has preparation text, it's copied word for word and split into **Oven** and **Microwave** only where the text has those headings, and labelled as coming from Open Food Facts. Edited or typed directions are labelled as typed by the family. Nothing (including AI) fills in temperatures, times, or steps. You can attach a photo of the box's cooking panel (taken now or chosen from the photo library), kept large enough to read and shown tap-to-enlarge, and add or fix directions later from the planned meal.
4. **Add to the day.** It becomes that day's planned dinner, visible on both phones, and stays in **Earlier this week** after the day passes, like any dinner. If the day already has a dinner, the page says so and replaces it only when you tick **Replace …**; a confirmation made before another phone changed the day doesn't count. The shopping list and recipe box are never touched. Removing or replacing the meal deletes its label photo.

**What Open Food Facts actually has (checked November 2026).** For U.S. frozen meals it reliably returns the product name and brand and usually the package size (sometimes in grams, sometimes missing). Of 100 U.S. "frozen meals" records surveyed, none had preparation text, so in practice the cooking directions come from a label photo or typing. Its U.S. coverage of frozen meals is incomplete (246 products in that category), and community-entered data can be wrong (the textbook example barcode 012345678905 is listed as a jar of cactus), which is why the review step comes before saving.

**Camera access** needs a secure page: the HTTPS (Funnel) address, or `localhost`. On plain `http://` over the home network, live scanning is hidden and the barcode photo, typed digits, and name still work.

## Community recipes

Recipes are private to their Table. **Share with the community** (on a recipe, or the opt-in box when adding one) shows a preview of exactly what will be published: the recipe, its "Created by" credit, your display name as the one who shared it, and optionally a photo (a separate copy; the family's photo stays private). Notes, last made, the handwritten tag, plans, grocery lists, conversations, and account details are never published.

- Publishing makes a **snapshot**. Editing the family recipe changes only the family copy; **Update community recipe** is a separate step that publishes the next version (any member of the Table can do it). **Withdraw** takes it down and removes its photos.
- **Community** (in the navigation) is shared by everyone: search by dish or ingredient (full-text index, 20 per page), see the creator credit, and **Save to [your Table]**. Saving copies the exact version you were looking at into your Table, with its own photo copy and the original credit. Tapping twice gives one copy. Saved copies are yours to edit and keep working if the original is changed or withdrawn; the recipe page says when a newer version exists.
- **Report** a recipe that's spam, offensive, or unsafe. After three different people report it, it's hidden until whoever runs the server reviews it: `python -m app.community reports | restore ID | remove ID`.

## Ideas from what you have

On a day's **Choose dinner** page, **Get ideas from what you have**: type what's in the kitchen ("chicken, rice, broccoli"), then pick where to look:

- **Meals we've made**: your Table's own recipes that use those, labelled "You've made this" (marked with **We made it today**) or "Planned before" (only scheduled), with any extra ingredients you'd need (staples left out). **Plan for Tuesday** puts it straight on the plan.
- **Community recipes** that use them, with **Save and plan**.
- **Invent something new**: three ideas from the local model, clearly labelled as AI ideas. **Start a draft** opens an editable recipe marked as AI-generated; nothing is saved until you press Save.

Only your Table's recipes and published community recipes are searched (indexed queries, a handful of results). The model sees only the words you typed, never anyone's recipes.

## Sharing a list between phones

Each open list asks the server "anything new?" every 5 seconds (8 on the planner) and pauses while the tab is hidden. The check is one small query, and the list is re-sent only when its version number has changed.

Every change carries the item's revision number (optimistic concurrency), so a phone showing an old list can't overwrite a newer change; it receives the latest list instead. Taps are queued in order and retried if the connection drops. There is no Redis, WebSocket server, or background worker.

## iPhone app (Progressive Web App)

Family Table can be added to an iPhone home screen, where it opens full screen with its own icon. It is the same website and database as the desktop, so nothing comes from the App Store.

- `/manifest.webmanifest`: name, icons, `standalone` display, theme colors, and shortcuts to the Shopping List and Meal Planner.
- Apple tags in `base.html`: `apple-touch-icon`, status bar style, and `viewport-fit=cover` with safe-area padding for the notch and home indicator.
- `/sw.js`: a deliberately small service worker. Pages, photos, and list updates always come from the server. Its only job is to show a **Can't reach Family Table / Try again** screen when the server is unreachable. It caches four public files and never caches recipes, lists, or signed-in pages.
- Static files are linked with a content hash (`styles.css?v=…`), so an installed app picks up new CSS and JavaScript straight away.
- Touch details: 16px text inputs (stops iOS zooming in), a sticky section bar, and large tap targets.

A PWA needs HTTPS (or `localhost`). Install steps and a test checklist are in [`iphone-testing.md`](iphone-testing.md). To redraw the icons: `python tools/make_icons.py`.

## Security

- **Passwords**: salted scrypt hashes (Python standard library), at least 10 characters. Signing in takes the same time whether or not the username exists, and wrong usernames and passwords get the same message.
- **Sessions**: random tokens stored hashed, with device labels; sign out one device or all others; a new password signs out the others. Cookies are `HttpOnly`, `SameSite=Lax`, and over HTTPS `Secure` with the `__Host-` prefix. Sign-ins expire (`SESSION_HOURS` / `REMEMBER_DAYS`).
- **Recovery**: one-time reset links made by whoever runs the server (no email), valid for an hour.
- **CSRF**: every form and background request that changes data carries a token (sign-in and sign-up use a double-submit cookie).
- **Access**: every read and write checks Table membership on the server, including photos, nested items, search, polls, the assistant, voice, drafts, and sharing. A Table id sent by the browser is only a request; someone else's records are simply "not found". Confirmations (removal, publishing, recipe edits) are checked again when they happen.
- **Model and imported content are untrusted**: recipe text, imported pages, labels, community recipes, and model replies can't authorize anything. Model-suggested actions are accepted only when they match what the person said, and permanent changes always need confirmation.
- **Rate limits** (per account and per address, plus site-wide backstops): sign-in, sign-up, reset links, invites, invite pages, imports, uploads, community search and sharing, reports, and assistant/voice turns. Model calls run on their own small pool, one per person, so a slow reply never holds up anyone else.
- **Headers**: Content-Security-Policy (no inline scripts), `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, and HSTS over HTTPS.
- **Logs**: one-time links are redacted from the access log; app diagnostics record ids and fixed codes, never passwords, tokens, audio, or what families say.
- **Uploads, import, container, single server**: as before (content-checked photos with random names, SSRF-safe downloader, hardened container, one server per data folder).

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `FAMILY_USERNAME` / `FAMILY_PASSWORD_HASH` | — | The shared login from before accounts: becomes the first account during the upgrade (see [upgrading.md](upgrading.md)) |
| `FAMILY_DISPLAY_NAME` / `FAMILY_TABLE_NAME` | — | Upgrade only: the first account's name and the first Table's name (default "<Username>'s Table") |
| `FAMILY_TABLE_DATA_DIR` | `data` | Folder holding the database and photos |
| `COOKIE_SECURE` | `false` | `true` = HTTPS-only cookies. Docker and `scripts/serve.sh` set it to `true` themselves |
| `MAX_UPLOAD_MB` | `12` | Largest accepted photo upload |
| `SESSION_HOURS` | `12` | Normal sign-in length |
| `REMEMBER_DAYS` | `30` | Sign-in length with "Keep me signed in" |
| `HOUSEHOLD_TIMEZONE` | `America/Chicago` | The household's time zone: when a planned dinner's day ends |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama server for the Cooking Assistant (only the server connects to it) |
| `OLLAMA_MODEL` | `qwen3:1.7b` | Model the assistant uses |
| `OLLAMA_TIMEOUT_SECONDS` | `90` | How long to wait for a model answer |
| `OLLAMA_MAX_CONCURRENT` | `2` | Model requests at once; more get "busy" right away |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded between turns |
| `FAMILY_TABLE_LOG_LEVEL` | `INFO` | Detail of Family Table's own diagnostics on the server's terminal |

## Data and backups

All private data lives in two places, and neither is committed to Git or copied into the Docker image:

1. **`data/`**: `family_table.db` (recipes, notes, plans, lists, staples) and `uploads/` (photos).
2. **`.env`**: the login hash and settings.

To back up or move to another computer, copy both while the server is stopped, or use `sqlite3 data/family_table.db ".backup backup.db"` for a safe copy while it runs. Database upgrades run automatically at startup, back up the database to `data/backups/` first, and either finish completely or change nothing (see [upgrading.md](upgrading.md) for restoring).

## Known limitations

- Sites that block automated requests, need a login, or are paywalled can't be imported. Pages that build the recipe with JavaScript and have no structured data won't be found.
- Ingredient matching is deliberately cautious, so near-matches with different wording can appear as two lines rather than being wrongly merged.
- Mixed volume totals are written exactly ("1 1/2 cups + 2 tsp") rather than rounded. Items aren't grouped by store aisle.
- List changes reach the other phone within about 5 seconds, not instantly.
- Packaged-meal directions depend on the box: Open Food Facts rarely has them for U.S. frozen meals. Label photos are shown as photos; the text in them isn't read or searchable.
- The Cooking Assistant's permanent edits are word substitutions and ingredient amounts. Other recipe changes go through **Edit recipe** (or Tell me the recipe there).
- A small local model can misread open-ended questions; amounts, conversions, and serving changes come from the app, not the model.
- Voice uses the browser's speech recognition (Safari uses Apple's dictation service). iPhone ends a listening session after a pause, so Family Table restarts it; after a lock screen or app switch, a tap is needed to resume. Whether you can talk over the phone while it speaks depends on the iPhone: if not, tap the circle to interrupt. Recognition can mishear; field edits and permanent changes are always visible, undoable, or need confirmation.
- The recipe voice helper understands common ways of describing a recipe; unusual phrasing may need typing, especially with the model off.
- Signing up has to say when a username is taken (usernames are unique); sign-up is rate-limited so names can't be checked in bulk.
- Login throttling is in memory and resets when the server restarts. The app is designed for one worker process.
