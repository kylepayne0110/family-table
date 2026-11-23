# Sign-in, sign-up, recipes, and photos.

# json passes a draft's voice notes to the page; logging records access problems (ids only).
import json
import logging

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
from urllib.parse import quote, urlencode, urlsplit

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
from app import auth, meals, tables
from app.config import BASE_DIR, CATEGORIES
from app.database import get_db
from app.images import ImageError, delete_photo, photo_path, process_photo
from app.pwa import static_url
from app.recipe_import import RecipeImportError, import_recipe


router = APIRouter()
templates = Jinja2Templates(directory=BASE_DIR / "templates")
log = logging.getLogger("family_table.access")

# Short status messages shown after redirects (only these fixed strings).
MESSAGES = {
    "saved": "Recipe saved.",
    "deleted": "Recipe removed from the box.",
    "made": "Marked as made today.",
    "note": "Note added.",
    "note-deleted": "Note removed.",
    "joined": "Welcome! You're now a member of this Table.",
    "published": "Shared with the community. Your family copy stays private and editable.",
    "community-updated": "The community recipe now matches your family copy.",
    "withdrawn": "Removed from the community. Copies other families already saved still work for them.",
    "copied": "Saved to your recipes. It's your own copy now: edit it however you like.",
    "already-copied": "That version is already in your recipes.",
}

# Pages a Table switch can land on (the same section in the other Table).
SECTIONS = {"recipes": "/", "plan": "/plan", "shopping": "/shopping", "community": "/community"}


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


def _header_context(request: Request, session: auth.Session | None) -> dict:
    """The Table name in the header and, with several Tables, the switcher's list."""
    if session is None:
        return {}
    conn = getattr(request.state, "db", None)
    if conn is None:
        # Error pages render after the request's own connection has closed.
        from app.database import connect
        own = connect(request.app.state.settings.database_path)
        try:
            request.state.db = own
            return _header_context(request, session)
        finally:
            request.state.db = None
            own.close()
    scope = getattr(request.state, "scope", None)
    mine = tables.my_tables(conn, session.user_id)
    if scope is None and mine:
        try:
            scope = tables.resolve(conn, session)
        except tables.NoTable:
            scope = None
    return {"user_name": session.display_name, "my_tables": mine,
            "active_table": {"id": scope.table_id, "name": scope.name} if scope else None,
            "table_id": scope.table_id if scope else ""}


def render(request: Request, name: str, context: dict, session: auth.Session | None = None,
           status_code: int = 200):
    """Render a template with the CSRF token and the header's Table details."""
    context = {"csrf_token": session.csrf_token if session else "", "signed_in": session is not None,
               "nav_active": "recipes", "today": request.app.state.clock.today(),
               **_header_context(request, session), **context}
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    # 303 makes the browser follow up with a GET after a form POST.
    return RedirectResponse(url, status_code=303)


def recipe_or_404(conn: sqlite3.Connection, recipe_id: int, session: auth.Session,
                  request: Request | None = None) -> dict:
    """A recipe from one of your Tables. Anything else is 'not found' (no hint that it exists)."""
    table_id = meals.recipe_table(conn, recipe_id)
    if table_id is None or not tables.is_member(conn, table_id, session.user_id):
        if table_id is not None:
            log.info("access denied: user %s recipe %s", session.user_id, recipe_id)
        raise HTTPException(status_code=404, detail="We couldn't find that recipe.")
    if request is not None:
        scope = tables.resolve(conn, session, table_id, strict=True)
        request.state.scope = scope
        if session.active_table_id != table_id:
            tables.remember(conn, session, table_id)
    return meals.get_recipe(conn, recipe_id)


def scope(request: Request, conn: sqlite3.Connection, session: auth.Session) -> tables.Scope:
    return tables.scope_for_get(request, conn, session)


# ---- Sign in, sign up, sign out ----

def _pre_login_token(request: Request) -> str:
    return request.cookies.get(auth.LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(32)


def _public_page(request: Request, name: str, context: dict, status_code: int = 200):
    """A signed-out page with a double-submit CSRF cookie for its form."""
    token = _pre_login_token(request)
    settings = request.app.state.settings
    response = render(request, name, {"csrf_token": token, **context}, status_code=status_code)
    response.set_cookie(auth.LOGIN_CSRF_COOKIE, token, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")
    return response


def _pre_login_csrf_ok(request: Request, form) -> bool:
    cookie_token = request.cookies.get(auth.LOGIN_CSRF_COOKIE, "")
    form_token = str(form.get("csrf_token") or "")
    return bool(cookie_token) and hmac.compare_digest(cookie_token, form_token)


def _invite_token(value) -> str:
    token = str(value or "").strip()
    return token if 10 <= len(token) <= 100 and token.replace("-", "").replace("_", "").isalnum() else ""


def _invite_context(conn: sqlite3.Connection, token: str) -> dict:
    if not token:
        return {}
    view = tables.view_invite(conn, token)
    return {"invite": token, "invite_view": view}


def _login_page(request: Request, conn: sqlite3.Connection, error: str = "", username: str = "",
                invite: str = "", status_code: int = 200):
    return _public_page(request, "login.html", {
        "error": error, "username": username, **_invite_context(conn, invite),
    }, status_code)


def _finish_sign_in(request: Request, conn: sqlite3.Connection, user, remember: bool, invite: str):
    """Start a session; with an invite, join that Table right away."""
    settings = request.app.state.settings
    destination, joined_table = "/", None
    if invite:
        try:
            view = tables.accept_invite(conn, invite, user["id"])
        except tables.TableError:
            view = None
        if view and view.status in ("joined", "member"):
            joined_table = view.table_id
            destination = "/?msg=joined" if view.status == "joined" else "/"
        elif view:
            destination = f"/invite/{quote(invite)}"
    lifetime = timedelta(days=settings.remember_days) if remember else timedelta(hours=settings.session_hours)
    active = joined_table
    if active is None:
        last = user["last_table_id"] if "last_table_id" in user.keys() else None
        active = last if last and tables.is_member(conn, last, user["id"]) else None
    token = auth.create_session(conn, user["id"], lifetime, auth.device_label(request.headers.get("user-agent", "")),
                                active)
    if active is None and not tables.my_tables(conn, user["id"]) and destination == "/":
        destination = "/welcome"
    response = redirect(destination)
    auth.set_session_cookie(response, token, settings, remember)
    response.delete_cookie(auth.LOGIN_CSRF_COOKIE, path="/", secure=settings.cookie_secure,
                           httponly=True, samesite="lax")
    return response


@router.get("/login")
def login_form(request: Request, invite: str = "", conn: sqlite3.Connection = Depends(get_db)):
    invite = _invite_token(invite)
    if auth.session_from_request(request, conn):
        return redirect(f"/invite/{quote(invite)}" if invite else "/")
    return _login_page(request, conn, invite=invite)


@router.post("/login")
async def login(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username") or "")[:100]
    password = str(form.get("password") or "")[:auth.MAX_PASSWORD]
    invite = _invite_token(form.get("invite"))

    if not _pre_login_csrf_ok(request, form):
        return _login_page(request, conn, "That form expired. Please try again.", username, invite, 400)

    limiter: auth.RateLimiter = request.app.state.login_limiter
    client = auth.client_key(request)
    account_key = "user:" + username.strip().lower()
    if limiter.is_blocked(client) or limiter.is_blocked(account_key):
        return _login_page(request, conn, "Too many tries. Please wait 15 minutes and try again.", username, invite, 429)

    user = await run_in_threadpool(auth.authenticate, conn, username, password)
    if user is None:
        limiter.record_failure(client)
        limiter.record_failure(account_key)
        return _login_page(request, conn, "That username and password don't match. Try again.", username, invite, 401)

    limiter.reset(client)
    limiter.reset(account_key)
    return _finish_sign_in(request, conn, user, form.get("remember") == "on", invite)


@router.get("/signup")
def signup_form(request: Request, invite: str = "", conn: sqlite3.Connection = Depends(get_db)):
    invite = _invite_token(invite)
    if auth.session_from_request(request, conn):
        return redirect(f"/invite/{quote(invite)}" if invite else "/")
    return _public_page(request, "signup.html", {"values": {}, "errors": {}, **_invite_context(conn, invite)})


@router.post("/signup")
async def signup(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form = await request.form()
    invite = _invite_token(form.get("invite"))
    values = {
        "display_name": " ".join(str(form.get("display_name") or "").split())[:60],
        "username": str(form.get("username") or "").strip()[:40],
    }
    password = str(form.get("password") or "")[:auth.MAX_PASSWORD + 1]
    errors: dict[str, str] = {}

    def again(status: int):
        return _public_page(request, "signup.html", {"values": values, "errors": errors,
                                                     **_invite_context(conn, invite)}, status)

    if not _pre_login_csrf_ok(request, form):
        errors["form"] = "That form expired. Please try again."
        return again(400)
    if not values["display_name"]:
        errors["display_name"] = "Tell us your name (it's what your family will see)."
    problem = auth.username_problem(values["username"])
    if problem:
        errors["username"] = problem
    problem = auth.password_problem(password, values["username"], values["display_name"])
    if problem:
        errors["password"] = problem
    if errors:
        return again(422)
    if auth.too_many(request, "signup", auth.client_key(request)):
        errors["form"] = "Too many new accounts from here recently. Please try again in an hour."
        return again(429)

    password_hash = await run_in_threadpool(auth.hash_password, password)
    try:
        with conn:
            user_id = conn.execute("INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                                   (values["username"], values["display_name"], password_hash)).lastrowid
    except sqlite3.IntegrityError:
        # Usernames are public handles (they have to be unique), so this can't be hidden entirely;
        # the sign-up limit above keeps anyone from checking names in bulk.
        errors["username"] = "That username is taken. Try another."
        return again(422)
    user = conn.execute("SELECT id, username, display_name, last_table_id FROM users WHERE id = ?", (user_id,)).fetchone()
    return _finish_sign_in(request, conn, user, form.get("remember") == "on", invite)


@router.post("/logout")
async def logout(request: Request, session: auth.Session = Depends(auth.require_session),
                 conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    auth.delete_session(conn, session)
    response = redirect("/login")
    auth.clear_session_cookie(response, request.app.state.settings)
    return response


@router.get("/forgot")
def forgot(request: Request):
    """Recovery without email: the person who runs this Family Table makes a one-time reset link."""
    return render(request, "forgot.html", {})


@router.get("/reset/{token}")
def reset_form(request: Request, token: str, conn: sqlite3.Connection = Depends(get_db)):
    from app import accounts
    valid = accounts.reset_token_user(conn, token) is not None
    return _public_page(request, "reset.html", {"token": token if valid else "", "valid": valid, "error": ""},
                        200 if valid else 404)


@router.post("/reset/{token}")
async def reset_submit(request: Request, token: str, conn: sqlite3.Connection = Depends(get_db)):
    from app import accounts
    form = await request.form()
    password = str(form.get("password") or "")[:auth.MAX_PASSWORD + 1]

    def again(error: str, status: int, valid: bool = True):
        return _public_page(request, "reset.html", {"token": token if valid else "", "valid": valid,
                                                    "error": error}, status)

    if not _pre_login_csrf_ok(request, form):
        return again("That form expired. Please try again.", 400)
    if auth.too_many(request, "reset", auth.client_key(request)):
        return again("Too many tries. Please wait 15 minutes and try again.", 429)
    user = accounts.reset_token_user(conn, token)
    if user is None:
        return again("This reset link has expired or was already used. Ask for a new one.", 404, valid=False)
    problem = auth.password_problem(password, user["username"], user["display_name"])
    if problem:
        return again(problem, 422)
    password_hash = await run_in_threadpool(auth.hash_password, password)
    if not accounts.use_reset_token(conn, token, password_hash):
        return again("This reset link has expired or was already used. Ask for a new one.", 404, valid=False)
    return redirect("/login?reset=1")


# ---- Recipes (the active Table's recipe book) ----

@router.get("/")
def home(request: Request, q: str = "", category: str = "", msg: str = "",
         session: auth.Session = Depends(auth.require_session),
         conn: sqlite3.Connection = Depends(get_db)):
    current = scope(request, conn, session)
    q = q.strip()[:100]
    category = category if category in CATEGORIES else ""
    filtering = bool(q or category)

    today = request.app.state.clock.today()
    featured = None if filtering else meals.featured_recipe(conn, current.table_id, today)
    return render(request, "meals.html", {
        "today": today,
        "total": meals.count_recipes(conn, current.table_id),
        "recipes": meals.list_recipes(conn, current.table_id, q, category),
        "featured": featured,
        "featured_note": meals.latest_note(conn, featured["id"]) if featured else None,
        "q": q, "active_category": category, "filtering": filtering,
        "message": MESSAGES.get(msg, ""),
    }, session)


EMPTY_VALUES = {
    "name": "", "description": "", "category": "weeknight", "cuisine": "",
    "prep_minutes": "", "cook_minutes": "", "servings": "", "card_tag": "",
    "last_made": "", "note_author": "", "note_body": "",
    "source_url": "", "imported_photo": "", "creator_credit": "", "version": "",
    "ingredients": [], "steps": [],
}


def _form_page(request, session, values, errors=None, recipe=None, status_code=200, **extra):
    return render(request, "meal_form.html", {
        "values": values, "errors": errors or {}, "recipe": recipe, **extra,
    }, session, status_code)


def own_temp_photo(conn: sqlite3.Connection, filename: str, user_id: int, table_id: int | None = None) -> bool:
    """True if this unsaved photo (Import or a draft) was added by this person (in this Table)."""
    if not filename:
        return False
    sql = "SELECT 1 FROM temp_photos WHERE filename = ? AND user_id = ?"
    params: list = [filename, user_id]
    if table_id is not None:
        sql += " AND table_id = ?"
        params.append(table_id)
    return conn.execute(sql, params).fetchone() is not None


def register_temp_photo(conn: sqlite3.Connection, filename: str, user_id: int, table_id: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO temp_photos (filename, user_id, table_id) VALUES (?, ?, ?)",
                     (filename, user_id, table_id))


def adopt_temp_photo(conn: sqlite3.Connection, settings, filename: str, user_id: int, table_id: int) -> str | None:
    """Move your own unsaved photo into the recipe photos. Someone else's filename is ignored."""
    from app.images import adopt_photo
    if not own_temp_photo(conn, filename, user_id, table_id):
        return None
    adopted = adopt_photo(settings.import_dir, settings.upload_dir, filename)
    with conn:
        conn.execute("DELETE FROM temp_photos WHERE filename = ?", (filename,))
    return adopted


async def _save_from_form(request: Request, session: auth.Session, conn: sqlite3.Connection,
                          recipe: dict | None = None):
    """Shared create/edit handler: validate, store photo, save, redirect."""
    settings = request.app.state.settings
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    if recipe is None:
        current = tables.scope_for_post(request, conn, session, form)
    else:
        current = tables.resolve(conn, session, recipe["table_id"], strict=True)
        request.state.scope = current
    values, data, errors = meals.parse_recipe_form(form)
    share = form.get("share_community") == "on"

    old_photo = recipe["photo_filename"] if recipe else None
    new_photo = None
    upload = form.get("photo")
    has_upload = isinstance(upload, UploadFile) and bool(upload.filename)

    if errors:
        if has_upload:
            errors["photo"] = "Please choose the photo again after fixing the errors above."
        return _form_page(request, session, values, errors, recipe, 422, share_community=share)

    imported = values["imported_photo"]
    if imported and not own_temp_photo(conn, imported, session.user_id, current.table_id):
        imported = values["imported_photo"] = ""   # not yours: ignored, never moved or shown
    if has_upload:
        if auth.too_many(request, "upload", f"user:{session.user_id}"):
            return _form_page(request, session, values, {"photo": "Too many photo uploads. Try again later."},
                              recipe, 429)
        max_bytes = settings.max_upload_mb * 1024 * 1024
        raw = await upload.read(max_bytes + 1)  # read one extra byte to detect oversize
        try:
            new_photo = await run_in_threadpool(process_photo, raw, settings.upload_dir, max_bytes)
        except ImageError as exc:
            return _form_page(request, session, values, {"photo": str(exc)}, recipe, 422)
        if imported:
            delete_photo(settings.import_dir, imported)  # a chosen upload replaces an imported photo
    elif imported and form.get("discard_imported_photo") != "on":
        # Photo from Import or a saved draft: move it from the temp folder into uploads.
        new_photo = adopt_temp_photo(conn, settings, imported, session.user_id, current.table_id)

    # Decide which photo the saved recipe should point at.
    if new_photo:
        photo = new_photo
    elif form.get("remove_photo") == "on":
        photo = None
    else:
        photo = old_photo

    expected = int(values["version"]) if recipe and values["version"] else None
    try:
        recipe_id = meals.save_recipe(conn, data, photo, recipe["id"] if recipe else None,
                                      table_id=current.table_id, user_id=session.user_id,
                                      expected_version=expected)
    except meals.StaleEdit as exc:
        if new_photo and new_photo != old_photo:
            delete_photo(settings.upload_dir, new_photo)
        latest = meals.get_recipe(conn, recipe["id"]) if recipe else None
        if latest is None:
            raise HTTPException(status_code=404, detail="That recipe was deleted while you were editing it.") from None
        log.info("draft-save conflict: user %s recipe %s", session.user_id, recipe["id"])
        values["version"] = str(latest["version"])   # saving again now replaces their version, on purpose
        return _form_page(request, session, values, {"conflict": (
            f"{exc} Your changes are below and haven't been saved. Open the recipe in another tab to compare, "
            "or press Save again to replace their version with yours.")}, latest, 409)
    except sqlite3.Error:
        # Don't leave an orphaned upload behind if the save failed.
        delete_photo(settings.upload_dir, new_photo)
        raise

    # Only now, after the database commit, remove the replaced photo.
    if old_photo and old_photo != photo:
        delete_photo(settings.upload_dir, old_photo)

    draft_id = str(form.get("draft_id") or "")
    if draft_id.isdigit():
        from app import drafts
        drafts.discard(conn, int(draft_id), session.user_id)
    if share:
        return redirect(f"/recipes/{recipe_id}/share?from=new")
    return redirect(f"/recipes/{recipe_id}?msg=saved")


def draft_values(draft: dict) -> dict:
    """A saved draft's fields as form values (missing keys get their empty defaults)."""
    fields = {**EMPTY_VALUES, **{k: v for k, v in draft["fields"].items() if k in EMPTY_VALUES}}
    fields["version"] = ""
    return fields


@router.get("/recipes/new")
def new_recipe_form(request: Request, draft: int | None = None,
                    session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    """A blank form, or a draft (from an AI idea, or recovered after a reload or lost connection)."""
    from app import drafts
    current = scope(request, conn, session)
    if draft is not None:
        found = drafts.get(conn, draft, session.user_id, current.table_id)
        if found is None:
            raise HTTPException(status_code=404, detail="That draft isn't available. It may have been saved already.")
        return _form_page(request, session, draft_values(found), draft_id=found["id"], draft_rev=found["rev"],
                          draft_voice=json.dumps(found["voice"]),
                          ai_generated=found["fields"].get("ai_generated", False),
                          ai_offline=found["fields"].get("offline", False))
    waiting = drafts.latest(conn, session.user_id, current.table_id)
    return _form_page(request, session, dict(EMPTY_VALUES), recoverable=waiting)


@router.post("/recipes/new")
async def create_recipe(request: Request, session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    return await _save_from_form(request, session, conn)


@router.post("/recipes/import")
async def import_from_url(request: Request, session: auth.Session = Depends(auth.require_session),
                          conn: sqlite3.Connection = Depends(get_db)):
    """Fetch a recipe page and show the Add Recipe form pre-filled for review (nothing is saved)."""
    settings = request.app.state.settings
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    current = tables.scope_for_post(request, conn, session, form)
    url = str(form.get("url") or "").strip()[:2000]

    if not url:
        return _form_page(request, session, dict(EMPTY_VALUES), status_code=422,
                          import_error="Paste a link to a recipe page first.", import_url=url)
    if auth.too_many(request, "import", f"user:{session.user_id}"):
        return _form_page(request, session, dict(EMPTY_VALUES), status_code=429,
                          import_error="That's a lot of imports in a short time. Try again in a little while.",
                          import_url=url)
    try:
        values, notes = await run_in_threadpool(
            import_recipe, url, settings.import_dir, settings.max_upload_mb * 1024 * 1024)
    except RecipeImportError as exc:
        return _form_page(request, session, dict(EMPTY_VALUES), status_code=422,
                          import_error=str(exc), import_url=url)
    values = {**EMPTY_VALUES, **values}
    if values.get("imported_photo"):
        register_temp_photo(conn, values["imported_photo"], session.user_id, current.table_id)
    return _form_page(request, session, values, import_notes=notes,
                      imported_from=urlsplit(values["source_url"]).hostname or "")


@router.get("/recipes/{recipe_id}")
def recipe_detail(request: Request, recipe_id: int, msg: str = "",
                  session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    recipe = recipe_or_404(conn, recipe_id, session, request)
    return render(request, "meal_detail.html", {
        "recipe": recipe, "message": MESSAGES.get(msg, ""), **_recipe_extras(conn, recipe),
        "made_today": recipe["last_made"] == request.app.state.clock.today().isoformat(),
    }, session)


def _recipe_extras(conn: sqlite3.Connection, recipe: dict) -> dict:
    from app import community
    added = conn.execute("SELECT display_name FROM users WHERE id = ?", (recipe["added_by"],)).fetchone() \
        if recipe.get("added_by") else None
    source = None
    if recipe.get("community_listing_id"):
        row = conn.execute("SELECT id, status, version FROM community_listings WHERE id = ?",
                           (recipe["community_listing_id"],)).fetchone()
        source = dict(row) if row else None
    return {"added_by_name": added["display_name"] if added else "",
            "listing": community.listing_for_recipe(conn, recipe["id"]), "community_source": source}


@router.get("/recipes/{recipe_id}/edit")
def edit_recipe_form(request: Request, recipe_id: int, draft: int | None = None,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    from app import drafts
    recipe = recipe_or_404(conn, recipe_id, session, request)
    if draft is not None:
        found = drafts.get(conn, draft, session.user_id, recipe["table_id"])
        if found is None or found["recipe_id"] != recipe_id:
            raise HTTPException(status_code=404, detail="That draft isn't available. It may have been saved already.")
        values = draft_values(found)
        values["version"] = str(recipe["version"])   # saving still checks nobody else changed the recipe since
        return _form_page(request, session, values, recipe=recipe, draft_id=found["id"], draft_rev=found["rev"],
                          draft_voice=json.dumps(found["voice"]))
    waiting = drafts.latest(conn, session.user_id, recipe["table_id"], recipe_id)
    return _form_page(request, session, meals.recipe_to_values(recipe), recipe=recipe, recoverable=waiting)


@router.post("/recipes/{recipe_id}/edit")
async def update_recipe(request: Request, recipe_id: int,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    recipe = recipe_or_404(conn, recipe_id, session, request)
    return await _save_from_form(request, session, conn, recipe)


@router.post("/recipes/{recipe_id}/delete")
async def remove_recipe(request: Request, recipe_id: int,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    recipe = recipe_or_404(conn, recipe_id, session, request)
    meals.delete_recipe(conn, recipe_id)
    delete_photo(request.app.state.settings.upload_dir, recipe["photo_filename"])
    return redirect("/?msg=deleted")


# ---- Small recipe actions ----

@router.post("/recipes/{recipe_id}/made")
async def made_today(request: Request, recipe_id: int,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    recipe_or_404(conn, recipe_id, session, request)
    meals.mark_made(conn, recipe_id, request.app.state.clock.today())
    return redirect(f"/recipes/{recipe_id}?msg=made")


@router.post("/recipes/{recipe_id}/notes")
async def create_note(request: Request, recipe_id: int,
                      session: auth.Session = Depends(auth.require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    recipe = recipe_or_404(conn, recipe_id, session, request)
    author = str(form.get("author") or "").strip()
    body = str(form.get("body") or "").strip()

    error = ""
    if not author or not body:
        error = "Add both the note and who it's from."
    elif len(author) > meals.LIMITS["note_author"] or len(body) > meals.LIMITS["note_body"]:
        error = "That note is a little too long."
    if error:
        return render(request, "meal_detail.html", {
            "recipe": recipe, "note_error": error, **_recipe_extras(conn, recipe),
            "note_values": {"author": author, "body": body},
            "made_today": recipe["last_made"] == request.app.state.clock.today().isoformat(),
        }, session, 422)

    meals.add_note(conn, recipe_id, author, body, session.user_id)
    return redirect(f"/recipes/{recipe_id}?msg=note#notes")


@router.post("/recipes/{recipe_id}/notes/{note_id}/delete")
async def remove_note(request: Request, recipe_id: int, note_id: int,
                      session: auth.Session = Depends(auth.require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    recipe_or_404(conn, recipe_id, session, request)
    meals.delete_note(conn, recipe_id, note_id)
    return redirect(f"/recipes/{recipe_id}?msg=note-deleted#notes")


# ---- Photos (only to people allowed to see them) ----

def photo_allowed(conn: sqlite3.Connection, filename: str, user_id: int) -> bool:
    """A recipe or label photo in one of your Tables, or a photo on a published community recipe."""
    return conn.execute(
        """SELECT 1 FROM recipes r JOIN memberships m ON m.table_id = r.table_id
               WHERE r.photo_filename = :f AND m.user_id = :u
           UNION ALL
           SELECT 1 FROM packaged_meals pk JOIN planned_meals p ON p.id = pk.meal_id
               JOIN memberships m ON m.table_id = p.table_id
               WHERE pk.label_photo = :f AND m.user_id = :u
           UNION ALL
           SELECT 1 FROM community_versions v JOIN community_listings l ON l.id = v.listing_id
               WHERE v.photo_filename = :f AND l.status = 'published'
           LIMIT 1""", {"f": filename, "u": user_id}).fetchone() is not None


@router.get("/photos/import/{filename}")
def imported_photo(request: Request, filename: str, session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    """Preview of your own unsaved photo (from Import or a draft)."""
    path = photo_path(request.app.state.settings.import_dir, filename)
    if path is None or not path.is_file() or not own_temp_photo(conn, filename, session.user_id):
        raise HTTPException(status_code=404, detail="Photo not found.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store"})


@router.get("/photos/{filename}")
def photo(request: Request, filename: str, session: auth.Session = Depends(auth.require_session),
          conn: sqlite3.Connection = Depends(get_db)):
    path = photo_path(request.app.state.settings.upload_dir, filename)
    if path is None or not path.is_file() or not photo_allowed(conn, filename, session.user_id):
        if path is not None and path.is_file():
            log.info("access denied: user %s photo", session.user_id)
        raise HTTPException(status_code=404, detail="Photo not found.")
    # "no-cache": the phone may keep a copy but must ask again each time, so removing someone
    # from a Table also stops their photos. Unchanged photos come back as a quick 304.
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-cache"})
