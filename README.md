# Family Table

Our family's digital recipe box: one private place to save every meal we know how to cook. Sign in, browse the box, open a recipe while cooking, tick off ingredients, leave notes for each other, and update recipes as they improve.

## Features

- Split-screen login styled as an index card, with one shared family account and "Keep me signed in"
- Home page with today's date, recipe count, a daily **Tonight's pick**, category filters, and search across names, descriptions, labels, and ingredients
- Recipe cards with photos, card numbers (`NO. 01`), total time, last-made info, and an optional handwritten tag
- Recipe page with prep/cook/servings/last-made strip, a large photo, an ingredient checklist with a live "2 of 10 ready" counter, numbered steps, and a **We made it today** button
- **Notes from the kitchen** on index cards, signed by name (`~ Mom, Oct 2025`)
- Add/edit/delete recipes with photo upload, adjustable ingredient and step rows, and server-side validation
- **Import from a website**: paste a recipe link, and the Add Recipe form fills in the name, description, times, servings, ingredients, steps, and photo for you to review before saving. Imported recipes link back to the **Original recipe**.
- **Meal Planner**: plan dinner for Monday to Sunday from your recipes, drag dinners between days (mouse or touch; two dinners swap), leave days blank, and browse past or future weeks
- **Shopping List**: one tap builds the week's grocery list, combining matching ingredients ("Ground beef 3 lbs, from Taco Bowls (1 lb) and Burgers (2 lbs)") and setting Kitchen Staples aside. At the store, tap an item once and it moves to Purchased. Changes show up on the other phone within a few seconds.
- **Kitchen Staples**: the shared list of things you always have (salt, butter, olive oil...). They're left off Need to Buy, and you can add one back when you run out.
- Responsive down to 320px wide phones

## Technology

| Layer | Choice |
|---|---|
| Backend | Python 3.14, FastAPI, Uvicorn |
| Templates | Jinja2 (server-rendered HTML) |
| Frontend | HTML, CSS, vanilla JavaScript (no build step) |
| Database | SQLite via Python's built-in `sqlite3` |
| Photos | Pillow (validate, fix rotation, resize, save as WebP) |
| Recipe import | recipe-scrapers + BeautifulSoup (parsing only); SSRF-safe downloader built on the standard library |
| Auth | scrypt password hash (Python standard library), server-side sessions in SQLite, CSRF tokens |
| Tests | pytest + FastAPI `TestClient` |

## Project structure

```text
family-table/
├── app/
│   ├── main.py          # Builds the FastAPI app: middleware, error pages, startup
│   ├── config.py        # Settings from .env, file paths, recipe categories
│   ├── database.py      # SQLite schema, connections, initialization
│   ├── meals.py         # Recipe queries, saving, and form validation
│   ├── routes.py        # All page routes and form handlers
│   ├── auth.py          # Password hashing, sessions, CSRF, login throttling
│   ├── images.py        # Photo validation and processing
│   ├── recipe_import.py # Import from a website: extract recipe data, map to the form
│   ├── safe_fetch.py    # Safe downloads from user-supplied URLs (SSRF protection)
│   ├── planner.py       # Weekly dinner plans (weeks, days, moving/swapping)
│   ├── ingredients.py   # Parse, normalize, and combine ingredient lines (no database)
│   ├── shopping.py      # Shopping lists, list updates, item states, Kitchen Staples
│   ├── plan_routes.py   # Pages/actions for the planner, shopping list, and staples
│   ├── setup_login.py   # CLI: set the shared username/password
│   └── seed.py          # CLI: optional sample recipes (development only)
├── templates/           # Jinja2 pages (+ _macros.html for shared pieces)
├── static/              # styles.css, main.js (+ live.js, planner.js, shopping.js), favicon
├── tests/               # pytest suite
├── data/                # ← your family's data (git-ignored)
│   ├── family_table.db  #   created automatically on first start
│   ├── uploads/         #   processed recipe photos
│   └── import-tmp/      #   photos from imports not saved yet (auto-cleaned)
├── requirements.txt     # runtime packages
├── requirements-dev.txt # runtime + test packages
├── .env.example         # settings template
└── pytest.ini
```

## Setup (Ubuntu / WSL)

```bash
cd ~/projects/family-table

# 1. Create and activate the virtual environment (skip creation if .venv exists)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install packages (use requirements.txt alone on a server)
pip install -r requirements-dev.txt

# 3. Create your settings file (skip if .env already exists)
cp .env.example .env

# 4. Set the shared family login (stores only a secure hash in .env)
python -m app.setup_login
```

Run `python -m app.setup_login` again any time to change the username or password. Restart the server afterward.

## Running

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** in your Windows browser.

The database and `data/uploads/` are created automatically on first start. Restarting never erases data. Tables are only created if missing.

Optional: to try the app with three sample recipes on an **empty** database, run `python -m app.seed`. It never runs on its own and refuses to touch a box that already has recipes.

## Tests

```bash
source .venv/bin/activate
python -m pytest
```

Every test uses its own temporary database and upload folder, so tests never touch `data/`.

## Importing recipes from websites

On **Add recipe**, paste a link into **Import from a website** and click **Import recipe**. Family Table downloads the page, reads its recipe data, and opens the normal Add Recipe form pre-filled. Nothing is saved until you click **Save recipe**, so you can fix anything first.

How it works:

1. **Download** (`app/safe_fetch.py`): one request, with redirects followed manually (at most 5). Pages are capped at 5 MB and photos at `MAX_UPLOAD_MB`. A request gets 5 s to connect, 10 s of silence, and 20 s in total.
2. **Extract** (`app/recipe_import.py`): `recipe-scrapers` reads site-specific and schema.org recipe data from the downloaded HTML. It never downloads anything itself. A small built-in JSON-LD reader fills any gaps and keeps section/step titles (`@graph`, `HowToSection`, `HowToStep`). Extractors are a simple list, so another one (such as an optional LLM fallback) could be added later.
3. **Map to the form**: ingredient lines are split into amount and name (`2 cups (240g)` / `flour`), times and servings are filled only when the page states them, and notes explain anything left blank. Nothing is invented.
4. **Photo**: the recipe's own image (from its structured data) is downloaded, validated, and resized by the same Pillow pipeline as uploads. It waits in `data/import-tmp/` and moves into `data/uploads/` only when you save. Unsaved import photos are deleted after 6 hours.

Security: only `http`/`https` on ports 80 and 443. Every resolved address must be public: loopback, private, link-local (including cloud metadata `169.254.169.254`), CGNAT, multicast and reserved ranges are refused, as are IPv4-mapped IPv6 forms. Each redirect hop is checked again, and connections go to the exact IP that was validated, which defeats DNS rebinding. HTTPS certificates are still verified against the real hostname. No JavaScript from the page is run. The import endpoint requires sign-in and a CSRF token.

Limitations:

- Sites that block automated requests (for example with a 403 error or a CAPTCHA), require a login, or are paywalled can't be imported, and Family Table doesn't try to get around that. Some sites also rate-limit repeated requests.
- Pages that build the recipe with JavaScript and have no structured data in the HTML won't be found.
- The category isn't guessed (except obvious soups), so pick one before saving.

Meal planner and shopping list limitations:

- Ingredient matching is deliberately cautious, so near-matches with different wording ("Jalapeño" and "jalapeño peppers") can appear as two lines. That's better than wrongly merging different things.
- Mixed volume totals are written out exactly ("1 1/2 cups + 2 tsp") rather than rounded.
- Items aren't grouped by store aisle.
- Updates reach the other phone within about 5 seconds, not instantly.
- Ingredient splitting covers common formats. Unusual lines keep the whole text in the ingredient name.

## Meal Planner and Shopping List

1. **Meal Planner**: tap **Add dinner** on a day and pick a recipe. Drag the ⋮⋮ handle to move a dinner; dropping it on another dinner swaps them. Tapping the handle (or **Move**) opens a simple Move menu instead. Blank days add nothing to the list.
2. **Generate Shopping List** gathers every planned recipe's ingredients. If you change the plan later, the planner and list say so and the button becomes **Update Shopping List**. Only moving dinners between days doesn't count as a change, because the groceries are the same.
3. **Shopping List** has three states: **Need to Buy**, **Already have**, and **Purchased**. Kitchen Staples sit in their own collapsed section. Use **Add an item...** for anything else (paper towels, bananas). Every list is saved per week, and old weeks stay available.

How combining works (`app/ingredients.py`, plain Python, no AI):

- Names are normalized conservatively: case, plurals, accents, notes in parentheses, and preparation words ("finely chopped", "melted", "lean") are ignored, and a few synonyms are mapped ("kosher salt" counts as salt, "garlic cloves" as garlic). Words that change the product are kept, so garlic and garlic powder, dried and fresh herbs, and crushed and plain tomatoes stay separate. Plain "butter" merges into "unsalted butter" only when that's the only kind on the list.
- Quantities are added with exact fractions. Units convert only within a family (3 tsp = 1 tbsp, 4 tbsp = 1/4 cup, 16 oz = 1 lb). Volume and weight are never mixed. Ranges ("2-3 cups") and anything unclear are kept exactly as written, and nothing is guessed.
- Each item keeps its sources: which recipes need it and how much, linked to the recipe pages.

Updating a list never resets it. Items keep their id and their Purchased or Already have state. Hand-added items always stay. If a purchased item now needs more, it moves back to Need to Buy with a note ("Need more: was 3 lbs, now 4 lbs"). Ingredients that are no longer planned are listed for you to **Remove** or **Keep**. Running Update on an unchanged plan changes nothing.

Sharing between phones: each open list asks the server "anything new?" every 5 seconds (8 on the planner), and stops while the tab is hidden. That check is a single tiny query, and the list is only re-sent when its version number has changed. Every change carries the item's revision number, so a phone showing an old list can't overwrite a newer change. It gets the latest list instead. Taps are saved in order and retried automatically if the connection drops. There's no Redis, WebSocket server, or background worker.

## Settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `FAMILY_USERNAME` | — | Shared login name (set by `setup_login`) |
| `FAMILY_PASSWORD_HASH` | — | scrypt hash of the password (set by `setup_login`) |
| `FAMILY_TABLE_DATA_DIR` | `data` | Folder holding the database and photos |
| `COOKIE_SECURE` | `false` | Set `true` once the site is served over HTTPS |
| `MAX_UPLOAD_MB` | `12` | Largest accepted photo upload |
| `SESSION_HOURS` | `12` | Normal sign-in length |
| `REMEMBER_DAYS` | `30` | Sign-in length with "Keep me signed in" |

## Data and backups

Everything personal lives in two places:

1. **`data/`**: `family_table.db` (recipes, notes, meal plans, shopping lists, staples) and `uploads/` (photos)
2. **`.env`**: your login hash and settings

To back up or move to another computer, copy both. Stop the server first, or use `sqlite3 data/family_table.db ".backup backup.db"` for a safe copy while it runs. Neither is committed to Git.

## Security notes

- The password is stored only as a salted scrypt hash. Sessions are random tokens stored (hashed) in SQLite, so **Sign out** truly ends the session.
- Session cookies are `HttpOnly` and `SameSite=Lax`. Every form that changes data carries a CSRF token.
- After 5 failed logins, an address is blocked for 15 minutes (in memory; resets when the server restarts).
- Photos are checked by their actual content, re-encoded (which strips EXIF/GPS metadata), given random names, and served only to signed-in users. The `data/` folder is never publicly served.
- All SQL is parameterized. Jinja2 escapes all user content. A Content-Security-Policy blocks inline scripts.
- Fonts load from Google Fonts. Everything else is self-hosted.

## Future deployment notes

- The app is light enough for the Z83-W mini PC (2 GB RAM): no background workers, and a single SQLite file.
- On the server, install with `pip install -r requirements.txt` and run Uvicorn without `--reload` (for example under a systemd service), bound to `127.0.0.1` behind a reverse proxy that provides HTTPS.
- Once served over HTTPS, set `COOKIE_SECURE=true`.
- Behind a reverse proxy, configure Uvicorn's `--proxy-headers`/`--forwarded-allow-ips` so login throttling sees real client addresses. Also set an upload size limit at the proxy (e.g. `client_max_body_size 15m` in Nginx).
- Move `data/` and `.env` across to bring every recipe and photo along.
