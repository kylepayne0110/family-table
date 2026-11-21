# secrets creates the pre-login CSRF token.
import secrets

# sqlite3 types the database connection.
import sqlite3

# datetime/date/timedelta handle dates shown on the pages.
from datetime import date, datetime, timedelta

# hmac.compare_digest compares the login CSRF token safely.
import hmac

# pass_context lets the "last made" filter read today's date from the page.
from jinja2 import pass_context

# urlencode builds filter links; urlsplit shows an import's website name.
from urllib.parse import urlencode, urlsplit

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# Response types for redirects, pages, and photos.
from fastapi.responses import FileResponse, RedirectResponse

# run_in_threadpool keeps slow image work off the event loop.
from fastapi.concurrency import run_in_threadpool

# Jinja2Templates renders the HTML templates.
from fastapi.templating import Jinja2Templates

# UploadFile is the type of a submitted file field.
from starlette.datastructures import UploadFile

# Project modules.
from app import auth, meals
from app.config import BASE_DIR, CATEGORIES
from app.database import get_db
from app.images import ImageError, adopt_photo, delete_photo, photo_path, process_photo
from app.pwa import static_url
from app.recipe_import import RecipeImportError, import_recipe


router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Short status messages shown after redirects (only these fixed strings).
MESSAGES = {
    "saved": "Recipe saved.",
    "deleted": "Recipe removed from the box.",
    "made": "Marked as made today.",
    "note": "Note added.",
    "note-deleted": "Note removed.",
}


# ---- Template helpers ----

def duration(minutes: int | None) -> str:
    """45 -> '45 min', 75 -> '1 hr 15 min'."""
    if not minutes:
        return ""
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    return f"{hours} hr" if hours else f"{mins} min"


def total_time(recipe) -> str:
    return duration((recipe["prep_minutes"] or 0) + (recipe["cook_minutes"] or 0))


@pass_context
def made_label(context, iso_date: str | None) -> str:
    """Friendly 'last made' text relative to the household's today."""
    if not iso_date:
        return ""
    made = date.fromisoformat(iso_date)
    days = (context.get("today") or date.today()) - made
    days = days.days
    if days <= 0:
        return "Made today"
    if days == 1:
        return "Made yesterday"
    if days < 14:
        return f"Made {days} days ago"
    if days < 60:
        return f"Made {days // 7} weeks ago"
    return f"Made {made.strftime('%b %Y')}"


def long_date(iso_date: str | None) -> str:
    return date.fromisoformat(iso_date).strftime("%b %-d, %Y") if iso_date else ""


def month_year(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).strftime("%b %Y")


templates.env.filters.update(
    duration=duration, total_time=total_time, made_label=made_label,
    long_date=long_date, month_year=month_year,
    card_no=lambda recipe_id: f"No. {recipe_id:02d}",
    hostname=lambda url: (urlsplit(url).hostname or "").removeprefix("www."),
)
templates.env.globals["categories"] = CATEGORIES
templates.env.globals["static_url"] = static_url   # versioned, root-relative static file links


def home_url(q: str = "", category: str = "") -> str:
    """Home page link that keeps the current search and category."""
    params = {key: value for key, value in (("q", q), ("category", category)) if value}
    return "/?" + urlencode(params) if params else "/"


templates.env.globals["home_url"] = home_url


def render(request: Request, name: str, context: dict, session: auth.Session | None = None,
           status_code: int = 200):
    """Render a template with the CSRF token available to every form."""
    context = {"csrf_token": session.csrf_token if session else "", "signed_in": session is not None,
               "nav_active": "recipes", "today": request.app.state.clock.today(), **context}
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    # 303 makes the browser follow up with a GET after a form POST.
    return RedirectResponse(url, status_code=303)


def recipe_or_404(conn: sqlite3.Connection, recipe_id: int) -> dict:
    recipe = meals.get_recipe(conn, recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="We couldn't find that recipe.")
    return recipe


# ---- Login and logout ----

def _login_page(request: Request, error: str = "", username: str = "", status_code: int = 200):
    """Render the login page, issuing a pre-login CSRF cookie if needed."""
    token = request.cookies.get(auth.LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(32)
    settings = request.app.state.settings
    response = render(request, "login.html", {
        "csrf_token": token, "error": error, "username": username,
        "configured": settings.login_configured,
    }, status_code=status_code)
    response.set_cookie(auth.LOGIN_CSRF_COOKIE, token, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/login")
    return response


@router.get("/login")
def login_form(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    if auth.load_session(conn, request.cookies.get(auth.SESSION_COOKIE)):
        return redirect("/")
    return _login_page(request)


@router.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username") or "")[:100]
    password = str(form.get("password") or "")[:200]

    # Double-submit check: the hidden field must match the cookie.
    cookie_token = request.cookies.get(auth.LOGIN_CSRF_COOKIE, "")
    form_token = str(form.get("csrf_token") or "")
    if not cookie_token or not hmac.compare_digest(cookie_token, form_token):
        return _login_page(request, "That form expired. Please try again.", username, 400)

    settings = request.app.state.settings
    limiter: auth.LoginLimiter = request.app.state.login_limiter
    client = request.client.host if request.client else "unknown"

    if limiter.is_blocked(client):
        return _login_page(request, "Too many tries. Please wait 15 minutes and try again.", username, 429)

    if not await run_in_threadpool(auth.check_credentials, settings, username, password):
        limiter.record_failure(client)
        return _login_page(request, "That username and password don't match. Try again.", username, 401)

    limiter.reset(client)
    remember = form.get("remember") == "on"
    lifetime = timedelta(days=settings.remember_days) if remember else timedelta(hours=settings.session_hours)
    token = auth.create_session(conn, lifetime)

    response = redirect("/")
    auth.set_session_cookie(response, token, settings, remember)
    # Deleted with the same flags it was set with, so browsers accept the removal over HTTPS.
    response.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/login", secure=settings.cookie_secure,
                           httponly=True, samesite="lax")
    return response


@router.post("/logout")
async def logout(request: Request, session: auth.Session = Depends(auth.require_session),
                 conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    auth.delete_session(conn, session)
    response = redirect("/login")
    response.delete_cookie(auth.SESSION_COOKIE, path="/", secure=request.app.state.settings.cookie_secure,
                           httponly=True, samesite="lax")
    return response


# ---- Home ----

@router.get("/")
def home(request: Request, q: str = "", category: str = "", msg: str = "",
         session: auth.Session = Depends(auth.require_session),
         conn: sqlite3.Connection = Depends(get_db)):
    q = q.strip()[:100]
    category = category if category in CATEGORIES else ""
    filtering = bool(q or category)

    today = request.app.state.clock.today()
    featured = None if filtering else meals.featured_recipe(conn, today)
    return render(request, "meals.html", {
        "today": today,
        "total": meals.count_recipes(conn),
        "recipes": meals.list_recipes(conn, q, category),
        "featured": featured,
        "featured_note": meals.latest_note(conn, featured["id"]) if featured else None,
        "q": q, "active_category": category, "filtering": filtering,
        "message": MESSAGES.get(msg, ""),
    }, session)


# ---- Recipe create / edit ----

EMPTY_VALUES = {
    "name": "", "description": "", "category": "weeknight", "cuisine": "",
    "prep_minutes": "", "cook_minutes": "", "servings": "", "card_tag": "",
    "last_made": "", "note_author": "", "note_body": "",
    "source_url": "", "imported_photo": "",
    "ingredients": [], "steps": [],
}


def _form_page(request, session, values, errors=None, recipe=None, status_code=200, **extra):
    return render(request, "meal_form.html", {
        "values": values, "errors": errors or {}, "recipe": recipe, **extra,
    }, session, status_code)


async def _save_from_form(request: Request, session: auth.Session, conn: sqlite3.Connection,
                          recipe: dict | None = None):
    """Shared create/edit handler: validate, store photo, save, redirect."""
    settings = request.app.state.settings
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    values, data, errors = meals.parse_recipe_form(form)

    old_photo = recipe["photo_filename"] if recipe else None
    new_photo = None
    upload = form.get("photo")
    has_upload = isinstance(upload, UploadFile) and bool(upload.filename)

    if errors:
        if has_upload:
            errors["photo"] = "Please choose the photo again after fixing the errors above."
        return _form_page(request, session, values, errors, recipe, 422)

    imported = values["imported_photo"]
    if has_upload:
        max_bytes = settings.max_upload_mb * 1024 * 1024
        raw = await upload.read(max_bytes + 1)  # read one extra byte to detect oversize
        try:
            new_photo = await run_in_threadpool(process_photo, raw, settings.upload_dir, max_bytes)
        except ImageError as exc:
            return _form_page(request, session, values, {"photo": str(exc)}, recipe, 422)
        delete_photo(settings.import_dir, imported)  # a chosen upload replaces an imported photo
    elif imported and form.get("discard_imported_photo") != "on":
        # Photo from "Import from a website": move it from the temp folder into uploads.
        new_photo = adopt_photo(settings.import_dir, settings.upload_dir, imported)

    # Decide which photo the saved recipe should point at.
    if new_photo:
        photo = new_photo
    elif form.get("remove_photo") == "on":
        photo = None
    else:
        photo = old_photo

    try:
        recipe_id = meals.save_recipe(conn, data, photo, recipe["id"] if recipe else None)
    except sqlite3.Error:
        # Don't leave an orphaned upload behind if the save failed.
        delete_photo(settings.upload_dir, new_photo)
        raise

    # Only now, after the database commit, remove the replaced photo.
    if old_photo and old_photo != photo:
        delete_photo(settings.upload_dir, old_photo)

    return redirect(f"/recipes/{recipe_id}?msg=saved")


@router.get("/recipes/new")
def new_recipe_form(request: Request, session: auth.Session = Depends(auth.require_session)):
    return _form_page(request, session, dict(EMPTY_VALUES))


@router.post("/recipes/new")
async def create_recipe(request: Request, session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    return await _save_from_form(request, session, conn)


@router.post("/recipes/import")
async def import_from_url(request: Request, session: auth.Session = Depends(auth.require_session)):
    """Fetch a recipe page and show the Add Recipe form pre-filled for review (nothing is saved)."""
    settings = request.app.state.settings
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    url = str(form.get("url") or "").strip()[:2000]

    if not url:
        return _form_page(request, session, dict(EMPTY_VALUES), status_code=422,
                          import_error="Paste a link to a recipe page first.", import_url=url)
    try:
        values, notes = await run_in_threadpool(
            import_recipe, url, settings.import_dir, settings.max_upload_mb * 1024 * 1024)
    except RecipeImportError as exc:
        return _form_page(request, session, dict(EMPTY_VALUES), status_code=422,
                          import_error=str(exc), import_url=url)

    return _form_page(request, session, values, import_notes=notes,
                      imported_from=urlsplit(values["source_url"]).hostname or "")


@router.get("/recipes/{recipe_id}")
def recipe_detail(request: Request, recipe_id: int, msg: str = "",
                  session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    recipe = recipe_or_404(conn, recipe_id)
    return render(request, "meal_detail.html", {
        "recipe": recipe, "message": MESSAGES.get(msg, ""),
        "made_today": recipe["last_made"] == request.app.state.clock.today().isoformat(),
    }, session)


@router.get("/recipes/{recipe_id}/edit")
def edit_recipe_form(request: Request, recipe_id: int,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    recipe = recipe_or_404(conn, recipe_id)
    return _form_page(request, session, meals.recipe_to_values(recipe), recipe=recipe)


@router.post("/recipes/{recipe_id}/edit")
async def update_recipe(request: Request, recipe_id: int,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    recipe = recipe_or_404(conn, recipe_id)
    return await _save_from_form(request, session, conn, recipe)


@router.post("/recipes/{recipe_id}/delete")
async def remove_recipe(request: Request, recipe_id: int,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    recipe = recipe_or_404(conn, recipe_id)
    meals.delete_recipe(conn, recipe_id)
    delete_photo(request.app.state.settings.upload_dir, recipe["photo_filename"])
    return redirect("/?msg=deleted")


# ---- Small recipe actions ----

@router.post("/recipes/{recipe_id}/made")
async def made_today(request: Request, recipe_id: int,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    recipe_or_404(conn, recipe_id)
    meals.mark_made(conn, recipe_id, request.app.state.clock.today())
    return redirect(f"/recipes/{recipe_id}?msg=made")


@router.post("/recipes/{recipe_id}/notes")
async def create_note(request: Request, recipe_id: int,
                      session: auth.Session = Depends(auth.require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    recipe = recipe_or_404(conn, recipe_id)
    author = str(form.get("author") or "").strip()
    body = str(form.get("body") or "").strip()

    error = ""
    if not author or not body:
        error = "Add both the note and who it's from."
    elif len(author) > meals.LIMITS["note_author"] or len(body) > meals.LIMITS["note_body"]:
        error = "That note is a little too long."
    if error:
        return render(request, "meal_detail.html", {
            "recipe": recipe, "note_error": error,
            "note_values": {"author": author, "body": body},
            "made_today": recipe["last_made"] == request.app.state.clock.today().isoformat(),
        }, session, 422)

    meals.add_note(conn, recipe_id, author, body)
    return redirect(f"/recipes/{recipe_id}?msg=note#notes")


@router.post("/recipes/{recipe_id}/notes/{note_id}/delete")
async def remove_note(request: Request, recipe_id: int, note_id: int,
                      session: auth.Session = Depends(auth.require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    meals.delete_note(conn, recipe_id, note_id)
    return redirect(f"/recipes/{recipe_id}?msg=note-deleted#notes")


# ---- Photos (served only to signed-in visitors) ----

@router.get("/photos/import/{filename}")
def imported_photo(request: Request, filename: str, session: auth.Session = Depends(auth.require_session)):
    """Preview of a photo found by URL import that hasn't been saved yet."""
    path = photo_path(request.app.state.settings.import_dir, filename)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store"})


@router.get("/photos/{filename}")
def photo(request: Request, filename: str, session: auth.Session = Depends(auth.require_session)):
    path = photo_path(request.app.state.settings.upload_dir, filename)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Photo not found.")
    # Filenames are random and never reused, so browsers may cache them.
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=604800"})
