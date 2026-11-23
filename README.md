# Family Table

A self-hosted recipe box, weekly meal planner, and shared grocery list for families. It runs on a home computer, installs on iPhones as an app, and keeps every recipe, plan, and photo in one SQLite file the family owns. Everyone has their own sign-in; a family shares a **Table** (its recipe book, meal plan, and grocery list), and can talk to a local cooking assistant hands-free.

![Python](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![Tests](https://img.shields.io/badge/tests-677%20passing-2ea44f)

## The problem

Our recipes were spread across bookmarks, screenshots, and cookbooks. Planning the week's dinners and writing a grocery list took an evening, and two people shopping from two copies of a list meant duplicate or missed items.

Family Table brings it together:

1. **Save recipes** by typing them in or pasting a link from a recipe website.
2. **Plan the week** by dragging dinners onto days.
3. **Add each dinner's ingredients** to one shared list, choosing what you actually need, with pantry staples left unchecked.
4. **Cook with help**: scale tonight's dinner for more people, and tap **Cook with me** for a spoken, step-by-step conversation while your hands are busy.
5. **Shop together**: items ticked off on one phone disappear from the other within seconds.
6. **Share if you want**: publish a family recipe to the community, or save someone else's into your Table.

## Features

| Area | Highlights |
|---|---|
| **Accounts and Tables** | Individual sign-ins; a Table is a shared recipe book, plan, and grocery list. Invite links ("Kyle invited you to Payne's Table"), equal members, safe removal and leaving, a switcher only when you're in several Tables |
| **Recipes** | Photos, categories, search, ingredient checklist, cooking steps, "Created by" credit, and signed notes from family members |
| **Recipe import** | Paste a URL; the form is pre-filled from the page's schema.org data for review before saving. Downloads go through an SSRF-safe fetcher |
| **Meal planning** | Monday–Sunday board with drag-and-drop (mouse and touch), swapping, and past/future weeks. A dinner leaves the board at midnight in the household's time zone and stays in the week's history |
| **Packaged meals** | **Scan a packaged meal** on a day: read the UPC with the phone camera (or a photo of the barcode, or typed digits, or just the name), look it up on Open Food Facts, correct it, and add it. Cooking directions are shown only as printed (copied, typed, or a tap-to-enlarge photo of the box). Never added to the recipe box or the shopping list |
| **Serving sizes** | Each planned dinner can be cooked for a different number of people; amounts (and quantities repeated in the steps) scale from the original with readable fractions |
| **Shopping list** | **Add ingredients** per dinner with a checklist (staples start unchecked); amounts combined with exact fraction math and unit conversion; each item lists the recipes that need it. Double taps never add twice |
| **Cooking Assistant** | A chat panel per planned dinner using a local Ollama model (`qwen3:1.7b`). The app does the math (conversions, scaling) and only saves a recipe change after you confirm |
| **Cook with me** | One tap starts a two-way spoken conversation: one step at a time, "next / repeat / go back", interrupt with questions, scale tonight's dinner, confirm permanent edits by voice, pick up where you left off |
| **Tell me the recipe** | Talk a recipe through on the Add/Edit form: it fills the visible form as you speak, handles corrections, asks about anything missing one question at a time, and never saves without your tap. Drafts survive reloads |
| **Community** | Opt-in sharing with a preview, separate "Update community recipe" step, withdrawal and reports; full-text search; saving makes an independent copy with credit |
| **Ideas** | "I have chicken, rice, and broccoli": meals you've made, community recipes, or a new AI idea that becomes an editable draft |
| **Shared sync** | Lightweight polling with version numbers and optimistic concurrency, so phones stay in step without WebSockets or a message broker |
| **Kitchen Staples** | A household list of always-on-hand items that stay off "Need to Buy" until someone runs out |
| **Progressive Web App** | Installable on iPhone home screens, full screen, with an offline notice. The service worker never caches private data |
| **Security** | scrypt passwords, hashed sessions with device list, CSRF, membership checks on every route, rate limits, hashed expiring invites, one-time reset links, CSP, redacted logs, hardened container |

Detailed notes on each feature are in [`docs/features.md`](docs/features.md).

## Technology

| Layer | Choice |
|---|---|
| Backend | Python 3.14, FastAPI, Uvicorn |
| Views | Server-rendered Jinja2 templates |
| Frontend | HTML, CSS, vanilla JavaScript (no framework, no build step) |
| Database | SQLite (WAL mode) through Python's built-in `sqlite3`, parameterized SQL, no ORM |
| Images | Pillow: content validation, orientation fix, resize, WebP re-encode |
| Recipe import | `recipe-scrapers` and BeautifulSoup for parsing; standard-library HTTP client for downloading |
| Auth | Standard-library scrypt, sessions stored in SQLite, CSRF tokens |
| Deployment | Docker and Docker Compose (single container), HTTPS through a reverse proxy |
| Voice | The browser's own speech recognition and speech synthesis (no audio reaches the server) |
| Testing | pytest, FastAPI `TestClient`, and Playwright (Chromium) for real-browser tests |

## Architecture

```mermaid
flowchart LR
    subgraph Clients
        Phone["iPhone (installed PWA)"]
        Desktop["Desktop browser"]
    end

    Proxy["HTTPS reverse proxy<br/>(Tailscale Funnel)"]

    subgraph Container["Docker container (non-root, read-only)"]
        Uvicorn["Uvicorn"]
        subgraph App["FastAPI application"]
            MW["Middleware<br/>security headers, sessions, CSRF"]
            Routes["Routes<br/>recipes · planner · shopping · staples"]
            Domain["Domain modules<br/>meals · planner · ingredients · shopping"]
            Import["Recipe import<br/>safe_fetch + recipe_import"]
            Images["Image pipeline<br/>Pillow"]
        end
    end

    subgraph Data["Mounted data volume"]
        DB[("SQLite<br/>family_table.db")]
        Uploads["uploads/<br/>WebP photos"]
    end

    Web["Recipe websites"]

    Phone -- HTTPS --> Proxy
    Desktop --> Proxy
    Proxy -- "127.0.0.1:8000" --> Uvicorn
    Uvicorn --> MW --> Routes --> Domain --> DB
    Routes --> Import -- "public IPs only" --> Web
    Routes --> Images --> Uploads
```

**Design choices**

- **One process, one file.** A single Uvicorn worker and one SQLite database keep the app light enough for a small home computer with no background services. A file lock stops two servers from sharing a data folder.
- **Pure domain logic.** Ingredient parsing and combining (`app/ingredients.py`) has no database access, which makes it easy to test thoroughly.
- **Server-rendered first.** Pages work as plain HTML forms. JavaScript adds drag-and-drop, live list updates, and offline handling on top.
- **Data stays outside the code.** The image contains only code. The database, photos, and `.env` are mounted at runtime and are never committed.

### Project layout

```text
app/
  main.py            App factory: middleware, error pages, startup, data-folder lock
  config.py          Settings loaded from .env
  database.py        Schema, connections, migrations
  migrations.py      Versioned upgrades with backup and verification
  auth.py            Passwords, sessions, CSRF, rate limits
  tables.py          Tables: membership checks, invites, removal, switching
  accounts.py        CLI: one-time password reset links
  routes.py          Sign in/up and recipe pages, photo access
  account_routes.py  Welcome, Table settings, invites, Account
  community.py       Community listings, versions, saved copies, reports
  community_routes.py Community pages, sharing, ideas
  suggest.py         Ideas from ingredients on hand
  cook.py            Cook with me: steps, cues, spoken confirmation
  recipe_voice.py    Recipe form voice helper: understanding speech, questions
  drafts.py          Saved recipe drafts
  draft_routes.py    Draft and voice endpoints
  model_gate.py      Keeps slow model calls off the page threads
  plan_routes.py     Planner, shopping list, and staples pages and JSON endpoints
  meal_routes.py     One planned dinner: servings, Add ingredients, Cooking Assistant
  packaged.py        Packaged meals: barcode checks, Open Food Facts lookup, directions
  packaged_routes.py Scan a packaged meal pages
  meals.py           Recipe queries and validation
  planner.py         Weekly plans: days, moving, swapping, servings, past days
  clock.py           "Today" in the household's time zone
  scaling.py         Serving math, readable fractions, unit conversion
  assistant.py       Cooking Assistant: requests the app answers itself, and the model prompt
  meal_chat.py       Saved conversations and confirmed recipe edits
  ollama.py          Client for the local Ollama server
  ingredients.py     Ingredient parsing, normalization, and combining
  shopping.py        Per-dinner additions, item states, staples
  recipe_import.py   Recipe extraction from downloaded HTML
  safe_fetch.py      SSRF-safe downloader
  images.py          Photo validation and processing
  pwa.py             Web app manifest, service worker, versioned static URLs
  setup_login.py     CLI: create an account or set a password from the server
  seed.py            CLI: optional sample recipes
templates/           Jinja2 pages and partials
static/              CSS, JavaScript, icons, service worker, offline page
tests/               pytest suite (tests/browser: Playwright, real clicks)
scripts/             Production start scripts (with and without Docker)
tools/               Icon generator, performance measurement
docs/                Feature notes, upgrading, testing, Docker guide, iPhone checklist
Dockerfile, compose.yaml
```

## Getting started

### Requirements

- Python 3.14 (for local development and tests)
- Docker with Docker Compose (optional, for the container)
- Linux, macOS, or Windows with WSL2

### 1. Clone and configure

```bash
git clone https://github.com/kylepayne0110/family-table.git
cd family-table

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
```

Then start the app and use **Create an account**. (Upgrading an existing install? See [`docs/upgrading.md`](docs/upgrading.md): your login and everything in it carry over.)

### 2a. Run for development

```bash
python -m uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and create an account. The database and `data/uploads/` are created on first start.

To try the app with three sample recipes, run `python -m app.seed` on the empty database.

### 2b. Run with Docker Compose

The container refuses to start without an existing database, to guard against starting on an empty folder by mistake. The first time, allow it to create a new one:

```bash
FAMILY_TABLE_NEW_DATABASE=yes docker compose up --build
```

After that, use `docker compose up --build`. Stop with **Ctrl+C**. Open http://127.0.0.1:8000.

Notes:

- The app is published on `127.0.0.1` only. Expose it to other devices through an HTTPS reverse proxy (this project uses [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)).
- The container sets `COOKIE_SECURE=true`. Browsers accept secure cookies on `127.0.0.1`/`localhost`; elsewhere it must be served over HTTPS.
- If your user id isn't 1000 (`id -u`), build with `docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)`.

See [`docs/docker.md`](docs/docker.md) for logs, data location, rebuilding, and running against a copy of the data.

### 2c. Run in production without Docker

```bash
./scripts/serve.sh
```

This runs Uvicorn on `127.0.0.1` with production settings: no reload, one worker, secure cookies, and proxy headers trusted only from localhost.

## Configuration

Settings are read from `.env`. See [`.env.example`](.env.example).

| Setting | Default | Purpose |
|---|---|---|
| `FAMILY_USERNAME` / `FAMILY_PASSWORD_HASH` | — | The shared login from before accounts; becomes the first account when upgrading |
| `FAMILY_DISPLAY_NAME` / `FAMILY_TABLE_NAME` | — | Upgrade only: the first account's and Table's names |
| `FAMILY_TABLE_DATA_DIR` | `data` | Folder for the database and photos |
| `COOKIE_SECURE` | `false` | HTTPS-only cookies (forced on in Docker and `serve.sh`) |
| `MAX_UPLOAD_MB` | `12` | Largest photo upload |
| `SESSION_HOURS` / `REMEMBER_DAYS` | `12` / `30` | Session lengths |
| `HOUSEHOLD_TIMEZONE` | `America/Chicago` | When a day (and its planned dinner) ends |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama server for the Cooking Assistant |
| `OLLAMA_MODEL` / `OLLAMA_TIMEOUT_SECONDS` | `qwen3:1.7b` / `90` | Model name and how long to wait for an answer |
| `OLLAMA_MAX_CONCURRENT` / `OLLAMA_KEEP_ALIVE` | `2` / `30m` | Model requests at once (extras get "busy"), and how long the model stays loaded |

The Cooking Assistant needs [Ollama](https://ollama.com) running with the model pulled (`ollama pull qwen3:1.7b`). Without it, everything else still works and the assistant says it can't reach the model. In Docker, the container reaches Ollama on the host at `http://host.docker.internal:11434` (set `OLLAMA_URL_DOCKER` to change it); Ollama must accept connections from Docker (on Windows, Docker Desktop forwards to it; elsewhere set `OLLAMA_HOST=0.0.0.0`). Under WSL without mirrored networking, `127.0.0.1` doesn't reach Ollama on Windows: point `OLLAMA_URL` at the Windows host's address.

## Testing

```bash
source .venv/bin/activate
python -m pytest --ignore=tests/browser    # 660 unit and integration tests
python -m pytest tests/browser             # 17 real-browser tests (Chromium via Playwright)
ruff check app tests && pip-audit -r requirements.txt
```

Each test gets its own temporary database, upload folder, and test accounts, so tests never touch real data; the recipe-import tests use a local test server instead of the internet, and the model is a stand-in. Browser tests use real clicks and form submissions; their speech is simulated (headless browsers have no microphone), so voice on a real iPhone is checked with [`docs/iphone-testing.md`](docs/iphone-testing.md). Details, setup, and what each suite covers: [`docs/testing.md`](docs/testing.md).

## Data and privacy

Private data never enters the repository or the Docker image:

- `data/` (database and photos) and `.env` are listed in `.gitignore`.
- `.dockerignore` is an allow-list: only `app/`, `templates/`, `static/`, `requirements.txt`, and the start script are copied into the image.
- To back up, copy `data/` and `.env` while the server is stopped. Database upgrades also write a copy to `data/backups/` before changing anything.

## Further reading

- [`docs/features.md`](docs/features.md): accounts and Tables, community, voice (and where it's processed), importing, ingredient combining, list sync, the PWA, and security
- [`docs/upgrading.md`](docs/upgrading.md): upgrading an existing install to accounts, and restoring
- [`docs/testing.md`](docs/testing.md): running the tests, browser tests, and measurements
- [`docs/docker.md`](docs/docker.md): running and maintaining the container
- [`docs/iphone-testing.md`](docs/iphone-testing.md): installing on iPhone and a test checklist
