# How Family Table works

Detailed notes on the main features. The [README](../README.md) has the overview and setup.

- [Recipes](#recipes)
- [Importing recipes from websites](#importing-recipes-from-websites)
- [Meal Planner and Shopping List](#meal-planner-and-shopping-list)
- [Planned dinners: servings and the Cooking Assistant](#planned-dinners-servings-and-the-cooking-assistant)
- [Sharing a list between phones](#sharing-a-list-between-phones)
- [iPhone app (Progressive Web App)](#iphone-app-progressive-web-app)
- [Security](#security)
- [Settings](#settings-env)
- [Data and backups](#data-and-backups)
- [Known limitations](#known-limitations)

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
- **Permanent edits** ("From now on, use panko instead of breadcrumbs"): the app shows the exact changes to the saved recipe, including affected steps, with **Confirm** and **Cancel**. Only wording changes; amounts and the serving count stay as saved, so tonight's 8 servings never changes a 4-serving recipe. A proposal goes stale (and saves nothing) if the recipe was edited meanwhile. Without "from now on" or similar, the assistant asks one short question: for good, or just tonight?

Everything else goes to the local Ollama model (`OLLAMA_MODEL`, default `qwen3:1.7b`) with the recipe name, yields, tonight's scaled ingredients, and the scaled steps in the prompt. There is no RAG, embeddings, or vector database. The model replies in JSON and may only *propose* an action (change servings, propose an edit, ask a question); the app validates it before doing anything. If Ollama is offline, the assistant says so and the recipe, serving selector, and checklist keep working.

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

- **Passwords**: stored only as a salted scrypt hash (Python standard library). `python -m app.setup_login` writes the hash to `.env`; the plain password is never stored.
- **Sessions**: random tokens, stored hashed in SQLite, so **Sign out** really ends a session. Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` when served over HTTPS.
- **CSRF**: every form and JSON request that changes data carries a CSRF token.
- **Login throttling**: 5 failed logins block an address for 15 minutes. Behind a proxy, only the known proxy address is trusted to report the visitor's real IP.
- **Headers**: Content-Security-Policy (no inline scripts), `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, and HSTS when served over HTTPS. Production mode hides debug pages and the server header.
- **Data access**: all SQL is parameterized, Jinja2 escapes all user content, photos are served only to signed-in users, and the `data/` folder is never served directly.
- **Uploads**: content-checked, size-limited, re-encoded, and saved under random names.
- **Recipe import**: SSRF-safe downloader (see above).
- **Container**: non-root user, read-only filesystem, all Linux capabilities dropped, `no-new-privileges`, and the port published only on `127.0.0.1`. The image contains no data or secrets.
- **Single server**: a file lock stops two servers from using the same data folder at once.

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `FAMILY_USERNAME` | — | Shared login name (set by `setup_login`) |
| `FAMILY_PASSWORD_HASH` | — | scrypt hash of the password (set by `setup_login`) |
| `FAMILY_TABLE_DATA_DIR` | `data` | Folder holding the database and photos |
| `COOKIE_SECURE` | `false` | `true` = HTTPS-only cookies. Docker and `scripts/serve.sh` set it to `true` themselves |
| `MAX_UPLOAD_MB` | `12` | Largest accepted photo upload |
| `SESSION_HOURS` | `12` | Normal sign-in length |
| `REMEMBER_DAYS` | `30` | Sign-in length with "Keep me signed in" |
| `HOUSEHOLD_TIMEZONE` | `America/Chicago` | The household's time zone: when a planned dinner's day ends |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama server for the Cooking Assistant (only the server connects to it) |
| `OLLAMA_MODEL` | `qwen3:1.7b` | Model the assistant uses |
| `OLLAMA_TIMEOUT_SECONDS` | `90` | How long to wait for a model answer |

## Data and backups

All private data lives in two places, and neither is committed to Git or copied into the Docker image:

1. **`data/`**: `family_table.db` (recipes, notes, plans, lists, staples) and `uploads/` (photos).
2. **`.env`**: the login hash and settings.

To back up or move to another computer, copy both while the server is stopped, or use `sqlite3 data/family_table.db ".backup backup.db"` for a safe copy while it runs. Schema changes (new columns) are applied automatically at startup without removing data.

## Known limitations

- Sites that block automated requests, need a login, or are paywalled can't be imported. Pages that build the recipe with JavaScript and have no structured data won't be found.
- Ingredient matching is deliberately cautious, so near-matches with different wording can appear as two lines rather than being wrongly merged.
- Mixed volume totals are written exactly ("1 1/2 cups + 2 tsp") rather than rounded. Items aren't grouped by store aisle.
- List changes reach the other phone within about 5 seconds, not instantly.
- The Cooking Assistant's permanent edits are word substitutions ("use X instead of Y"). Other recipe changes go through **Edit recipe**.
- A small local model can misread open-ended questions; amounts, conversions, and serving changes come from the app, not the model.
- Voice input uses the browser's speech recognition (Safari sends audio to Apple for transcription). Browsers without it show only the text box.
- Login throttling is in memory and resets when the server restarts. The app is designed for one worker process.
