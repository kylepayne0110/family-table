# "Scan a packaged meal": plan a store-bought meal for one dinner from its barcode,
# typed digits, or just its name. The product lookup happens here on the server; the
# browser never talks to the product database. Nothing here touches the recipe box or
# the shopping list.

# sqlite3 types the database connection.
import sqlite3

# timedelta works out the day's date.
from datetime import date, timedelta

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# run_in_threadpool keeps the lookup and photo work off the event loop.
from fastapi.concurrency import run_in_threadpool

# JSON for the page's JavaScript.
from fastapi.responses import JSONResponse

# UploadFile is the type of a submitted photo.
from starlette.datastructures import UploadFile

# Project modules.
from app import auth, packaged, planner, tables
from app.database import get_db
from app.images import LABEL_MAX_EDGE, ImageError, delete_photo, process_photo
from app.plan_routes import _week, _week_nav, household_today
from app.routes import redirect, render


router = APIRouter()

PHOTO_FIELDS = ("label_photo_camera", "label_photo_file")   # "Take a photo" and "Choose a photo"


def _day_or_404(week: date, day: int) -> None:
    if not 0 <= day <= 6:
        raise HTTPException(status_code=404, detail="We couldn't find that day.")


def _current_dinner(conn: sqlite3.Connection, table_id: int, week: date, day: int) -> dict | None:
    row = conn.execute(
        """SELECT p.id, COALESCE(r.name, pk.name) AS name FROM planned_meals p
           LEFT JOIN recipes r ON r.id = p.recipe_id LEFT JOIN packaged_meals pk ON pk.meal_id = p.id
           WHERE p.table_id = ? AND p.week_start = ? AND p.day = ?""",
        (table_id, week.isoformat(), day),
    ).fetchone()
    return dict(row) if row else None


def _scan_page(request, conn, session, week: date, day: int, values: dict | None = None, error: str = "",
               lookup: dict | None = None, status_code: int = 200):
    today = household_today(request)
    scope = request.state.scope
    return render(request, "packaged_scan.html", {
        **_week_nav(week, today), "nav_active": "plan", "day": day, "day_name": planner.DAY_NAMES[day],
        "day_date": week + timedelta(days=day), "current": _current_dinner(conn, scope.table_id, week, day),
        "is_past": planner.is_past(week, day, today), "values": values or {}, "error": error,
        "lookup": lookup, "max_upload_mb": request.app.state.settings.max_upload_mb,
    }, session, status_code)


@router.get("/plan/{week}/day/{day}/packaged")
async def scan_page(request: Request, week: str, day: int, barcode: str = "",
                    session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    """The scan page. Without JavaScript, ?barcode=… looks the product up here and pre-fills the form."""
    tables.scope_for_get(request, conn, session)
    wk = _week(week, household_today(request))
    _day_or_404(wk, day)
    if planner.is_past(wk, day, household_today(request)):
        raise HTTPException(status_code=404, detail="That day is over, so its dinner stays in the history.")
    code = packaged.normalize_barcode(barcode[:30])
    if not code:
        return _scan_page(request, conn, session, wk, day)
    problem = packaged.barcode_problem(code)
    if problem:
        return _scan_page(request, conn, session, wk, day, {"barcode": code}, error=problem, status_code=422)
    product = await run_in_threadpool(packaged.lookup, code)
    values = {"barcode": code, "name": product.name, "brand": product.brand, "package_size": product.package_size,
              "oven": product.oven, "microwave": product.microwave, "other": product.other}
    return _scan_page(request, conn, session, wk, day, values, lookup=product.as_dict())


@router.get("/packaged/lookup")
async def lookup(request: Request, barcode: str = "", session: auth.Session = Depends(auth.require_session)):
    """JSON lookup for the scan page's JavaScript."""
    if auth.too_many(request, "search", f"user:{session.user_id}"):
        return JSONResponse({"barcode": "", "problem": "Too many lookups. Wait a minute and try again."}, status_code=429)
    code = packaged.normalize_barcode(barcode[:30])
    problem = packaged.barcode_problem(code)
    if problem:
        return JSONResponse({"barcode": code, "problem": problem}, status_code=422)
    product = await run_in_threadpool(packaged.lookup, code)
    return JSONResponse(product.as_dict())


async def _label_photo(request: Request, form) -> str | None:
    """Process a chosen label photo (camera or photo library). Raises ImageError for a bad file."""
    settings = request.app.state.settings
    for field in PHOTO_FIELDS:
        upload = form.get(field)
        if isinstance(upload, UploadFile) and upload.filename:
            max_bytes = settings.max_upload_mb * 1024 * 1024
            raw = await upload.read(max_bytes + 1)
            return await run_in_threadpool(process_photo, raw, settings.upload_dir, max_bytes, LABEL_MAX_EDGE, 88)
    return None


@router.post("/plan/{week}/day/{day}/packaged")
async def add_to_day(request: Request, week: str, day: int,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    """Add to Friday: plan the reviewed meal. Never replaces a dinner without the family's say-so."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    scope = tables.scope_for_post(request, conn, session, form)
    today = household_today(request)
    wk = _week(week, today)
    _day_or_404(wk, day)
    values = {k: str(form.get(k) or "") for k in ("barcode", "name", "brand", "package_size", "oven", "microwave", "other")}
    replace = form.get("replace_meal_id")
    replace_id = int(replace) if str(replace or "").isdigit() and form.get("confirm_replace") == "on" else None
    settings = request.app.state.settings
    try:
        details = packaged.details_from_form(form)
        photo = await _label_photo(request, form)
    except (packaged.PackagedError, ImageError) as exc:
        return _scan_page(request, conn, session, wk, day, values, error=str(exc), status_code=422)
    try:
        meal_id, unused = packaged.add_packaged_meal(conn, scope.table_id, wk, day, details, photo, replace_id, today)
    except packaged.DayTaken as exc:
        delete_photo(settings.upload_dir, photo)
        error = str(exc) + (" A photo you chose needs to be added again." if photo else "")
        return _scan_page(request, conn, session, wk, day, values, error=error, status_code=409)
    except planner.PlanError as exc:
        delete_photo(settings.upload_dir, photo)
        return _scan_page(request, conn, session, wk, day, values, error=str(exc), status_code=422)
    except sqlite3.Error:
        delete_photo(settings.upload_dir, photo)
        raise
    for old in unused:
        delete_photo(settings.upload_dir, old)
    return redirect(f"/plan/meals/{meal_id}?msg=planned")


# ---- A planned packaged meal ----

def packaged_page(request: Request, conn: sqlite3.Connection, meal: dict, session: auth.Session,
                  message: str = "", error: str = "", status_code: int = 200):
    """What either phone sees when opening a planned packaged meal."""
    today = household_today(request)
    return render(request, "packaged_meal.html", {
        "meal": meal, "item": packaged.get_packaged(conn, meal["id"]), "nav_active": "plan",
        "week_iso": meal["week_start"], "is_past": meal["date"] < today, "is_tonight": meal["date"] == today,
        "message": message, "error": error, "max_upload_mb": request.app.state.settings.max_upload_mb,
        "source_url": packaged.OFF_PAGE_URL, "table_id": meal["table_id"],
    }, session, status_code)


@router.post("/plan/meals/{meal_id}/packaged")
async def update_directions(request: Request, meal_id: int,
                            session: auth.Session = Depends(auth.require_session),
                            conn: sqlite3.Connection = Depends(get_db)):
    """Attach or fix directions and the label photo later."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    meal = planner.get_meal(conn, meal_id)
    if meal is None or meal["kind"] != "packaged" or not tables.is_member(conn, meal["table_id"], session.user_id):
        raise HTTPException(status_code=404, detail="That dinner isn't on the plan anymore.")
    request.state.scope = tables.resolve(conn, session, meal["table_id"], strict=True)
    settings = request.app.state.settings
    try:
        photo = await _label_photo(request, form)
    except ImageError as exc:
        return packaged_page(request, conn, meal, session, error=str(exc), status_code=422)
    try:
        unused = packaged.update_directions(conn, meal_id, form, photo, form.get("remove_photo") == "on")
    except packaged.PackagedError as exc:
        delete_photo(settings.upload_dir, photo)
        return packaged_page(request, conn, meal, session, error=str(exc), status_code=422)
    for old in unused:
        delete_photo(settings.upload_dir, old)
    return redirect(f"/plan/meals/{meal_id}?msg=directions")
