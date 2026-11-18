# How Family Table works

Detailed notes on the main features. The [README](../README.md) has the overview and setup.

- [Recipes](#recipes)
- [Importing recipes from websites](#importing-recipes-from-websites)
- [Meal Planner and Shopping List](#meal-planner-and-shopping-list)
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

1. **Meal Planner**: tap **Add dinner** on a day and pick a recipe. Drag the ⋮⋮ handle to move a dinner (mouse or touch); dropping it on another dinner swaps them. A **Move** menu offers the same thing without dragging. Blank days add nothing to the list. Past and future weeks are available.
2. **Generate Shopping List** gathers every planned recipe's ingredients. If the plan changes later, the button becomes **Update Shopping List**. Moving dinners between days doesn't count as a change, because the groceries are the same.
3. **Shopping List** has three states: **Need to Buy**, **Already have**, and **Purchased**. Kitchen Staples sit in their own collapsed section. **Add an item...** covers anything else (paper towels, bananas). Each week keeps its own list.

### How ingredients are combined

`app/ingredients.py` is plain, deterministic Python with no database access and no AI.

- **Names** are normalized conservatively: case, plurals, accents, notes in parentheses, and preparation words ("finely chopped", "melted") are ignored, and a few synonyms are mapped ("kosher salt" counts as salt, "garlic cloves" as garlic). Words that change the product are kept, so garlic and garlic powder, dried and fresh herbs, and crushed and plain tomatoes stay separate.
- **Quantities** are added with exact fractions. Units convert only within a family (3 tsp = 1 tbsp, 16 oz = 1 lb). Volume and weight are never mixed. Ranges ("2-3 cups") and anything unclear are kept exactly as written.
- **Sources**: every item remembers which recipes need it and how much, for example "Ground beef 3 lbs, from Taco Bowls (1 lb) and Burgers (2 lbs)".

### Kitchen Staples

A shared list of things the household always has (salt, butter, olive oil). Staples are left off **Need to Buy** automatically and shown in their own section, and one tap adds a staple back when it runs out.

### Updating a list

**Update Shopping List** (only when pressed) makes the list match the current plan:

- Ingredients no planned dinner needs any more are removed. Shared ones keep the new amount (remove Burgers, and ground beef drops from 3 lbs to 1 lb).
- Items keep their id and their Purchased or Already have state. If a purchased item now needs more than was bought, it moves back to Need to Buy with a note ("Need more: was 3 lbs, now 4 lbs").
- Hand-added items always stay. Running Update on an unchanged plan changes nothing.

**Clear purchased** hides everything already bought. Cleared items are remembered for that week, so an Update doesn't bring them back unless the plan now needs more.

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
- Login throttling is in memory and resets when the server restarts. The app is designed for one worker process.
