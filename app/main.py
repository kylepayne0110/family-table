# asynccontextmanager defines startup/shutdown work for the app.
from contextlib import asynccontextmanager

# logging reports startup problems and upgrades.
import logging

# FastAPI creates our web application and handles HTTP requests.
# Request represents an incoming request from the browser.
from fastapi import FastAPI, Request

# RequestValidationError is raised for malformed URLs like /recipes/abc.
from fastapi.exceptions import RequestValidationError

# RedirectResponse sends signed-out visitors to the login page; PlainTextResponse refuses huge uploads.
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

# StaticFiles allows FastAPI to serve CSS and JavaScript.
from fastapi.staticfiles import StaticFiles

# StarletteHTTPException covers 404s and other HTTP errors.
from starlette.exceptions import HTTPException as StarletteHTTPException

# Project modules.
from app import ollama, tables
from app.account_routes import router as account_router
from app.community_routes import router as community_router
from app.model_gate import ModelGate
from app.auth import LoginRequired, default_limiters
from app.clock import HouseholdClock
from app.config import BASE_DIR, Settings, load_settings
from app.database import data_folder_lock, init_db
from app.recipe_import import cleanup_temp_photos
from app.meal_routes import router as meal_router
from app.packaged_routes import router as packaged_router
from app.plan_routes import router as plan_router
from app.pwa import router as pwa_router
from app.routes import render, router


# Content Security Policy: only our own scripts, plus Google Fonts.
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
)


class BodySizeLimit:
    """Refuse request bodies bigger than a photo upload plus its form fields.

    Matters once the site is public: without it, anyone could stream a huge body at the
    sign-in form and fill the disk with temporary upload files.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            response = PlainTextResponse("That upload is too large.", status_code=413)
            return await response(scope, receive, send)

        # Bodies sent without a length are counted as they arrive.
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise StarletteHTTPException(status_code=413, detail="That upload is too large.")
            return message

        return await self.app(scope, limited_receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (tests pass their own settings and data folder)."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Only one server may use the data folder; a second one stops here with a clear message.
        with data_folder_lock(settings.data_dir):
            # Create the data folders and database tables on startup.
            settings.upload_dir.mkdir(parents=True, exist_ok=True)
            settings.import_dir.mkdir(parents=True, exist_ok=True)
            legacy = (settings.username, settings.password_hash) if settings.login_configured else None
            report = init_db(settings.database_path, legacy, settings.display_name, settings.table_name)
            if report:
                logging.getLogger("family_table").warning(
                    "Upgraded the database to accounts and Tables. Backup of the original: %s", report["backup"])
            cleanup_temp_photos(settings.import_dir)  # leftovers from abandoned imports
            yield

    app = FastAPI(title="Family Table", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.limiters = default_limiters()
    app.state.login_limiter = app.state.limiters["login"]
    # "Today" for the planner, in the household's time zone (tests freeze it).
    app.state.clock = HouseholdClock(settings.zone)
    # The Cooking Assistant's model: the local Ollama server (tests swap in a stand-in).
    app.state.ask_model = lambda messages, response_format=None: ollama.chat(settings, messages, response_format)
    # Model calls run on their own small pool, one per person, so they never hold up pages.
    app.state.model_gate = ModelGate(settings.model_concurrency)

    # Make our static files accessible to the browser.
    # For example, /static/css/styles.css loads our stylesheet.
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(account_router)  # Welcome, Tables, invites, Account
    app.include_router(community_router)  # Community, sharing a recipe, ideas from what's on hand
    app.include_router(plan_router)  # Meal Planner, Shopping List, Kitchen Staples
    app.include_router(meal_router)  # one planned dinner: servings, Add ingredients, Cooking Assistant
    app.include_router(packaged_router)  # scan a packaged meal for one dinner
    app.include_router(pwa_router)   # web app manifest and service worker (home-screen install)
    # Largest photo plus 4 MB of room for the rest of the recipe form.
    app.add_middleware(BodySizeLimit, max_bytes=(settings.max_upload_mb + 4) * 1024 * 1024)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if settings.cookie_secure:
            # Served over HTTPS (e.g. Tailscale Funnel): browsers should never fall back to http.
            # Browsers ignore this header on plain http://localhost, so local use still works.
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Pages are private: don't let the back button show them after sign-out.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(tables.NoTable)
    async def no_table(request: Request, exc: tables.NoTable):
        # Signed in but not in any Table (new, or removed from the last one): start one or join one.
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"error": "You're not a member of any Table now.", "access": "lost"}, status_code=404)
        return RedirectResponse("/welcome", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if exc.status_code in (403, 404, 409, 410, 413, 422, 429) else "Something went wrong."
        if request.headers.get("x-requested-with") == "fetch":
            # Background requests from an open page get JSON, so the page can say what happened
            # (for example "you no longer have access to this Table") instead of failing quietly.
            lost = isinstance(exc, tables.NotMember)
            return JSONResponse({"error": message, "access": "lost" if lost else ""}, status_code=exc.status_code)
        session = getattr(request.state, "session", None)
        return render(request, "error.html", {"status": exc.status_code, "message": message},
                      session, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_url(request: Request, exc: RequestValidationError):
        # e.g. /recipes/abc: treat malformed URLs as "not found".
        return render(request, "error.html", {"status": 404, "message": "We couldn't find that page."},
                      status_code=404)

    return app


# The app Uvicorn runs: python -m uvicorn app.main:app --reload
app = create_app()
