# asynccontextmanager defines startup/shutdown work for the app.
from contextlib import asynccontextmanager

# FastAPI creates our web application and handles HTTP requests.
# Request represents an incoming request from the browser.
from fastapi import FastAPI, Request

# RequestValidationError is raised for malformed URLs like /recipes/abc.
from fastapi.exceptions import RequestValidationError

# RedirectResponse sends signed-out visitors to the login page.
from fastapi.responses import RedirectResponse

# StaticFiles allows FastAPI to serve CSS and JavaScript.
from fastapi.staticfiles import StaticFiles

# StarletteHTTPException covers 404s and other HTTP errors.
from starlette.exceptions import HTTPException as StarletteHTTPException

# Project modules.
from app.auth import LoginLimiter, LoginRequired
from app.config import BASE_DIR, Settings, load_settings
from app.database import init_db
from app.recipe_import import cleanup_temp_photos
from app.plan_routes import router as plan_router
from app.pwa import router as pwa_router
from app.routes import render, router


# Content Security Policy: only our own scripts, plus Google Fonts.
CSP = (
    "default-src 'self'; img-src 'self' data: blob:; script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app (tests pass their own settings and data folder)."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Create the data folders and database tables on startup.
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        settings.import_dir.mkdir(parents=True, exist_ok=True)
        init_db(settings.database_path)
        cleanup_temp_photos(settings.import_dir)  # leftovers from abandoned imports
        yield

    app = FastAPI(title="Family Table", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.login_limiter = LoginLimiter()

    # Make our static files accessible to the browser.
    # For example, /static/css/styles.css loads our stylesheet.
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(plan_router)  # Meal Planner, Shopping List, Kitchen Staples
    app.include_router(pwa_router)   # web app manifest and service worker (home-screen install)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # Pages are private: don't let the back button show them after sign-out.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if exc.status_code in (403, 404) else "Something went wrong."
        return render(request, "error.html", {"status": exc.status_code, "message": message},
                      status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_url(request: Request, exc: RequestValidationError):
        # e.g. /recipes/abc: treat malformed URLs as "not found".
        return render(request, "error.html", {"status": 404, "message": "We couldn't find that page."},
                      status_code=404)

    return app


# The app Uvicorn runs: python -m uvicorn app.main:app --reload
app = create_app()
