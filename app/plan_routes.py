# Pages and actions for the Meal Planner, Shopping List, and Kitchen Staples.

# sqlite3 types the database connection.
import sqlite3

# date/timedelta handle week navigation.
from datetime import date, timedelta

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# JSON for the page's JavaScript; Response for "nothing new" (204).
from fastapi.responses import JSONResponse, Response

# Project modules.
from app import auth, planner, shopping
from app.database import get_db
from app.routes import redirect, render, templates


router = APIRouter()

# Fixed messages shown after a redirect (never echo user text from the URL).
STAPLE_MESSAGES = {
    "added": "Staple added.",
    "renamed": "Staple renamed.",
    "removed": "Staple removed.",
    "listed": "Added to this week's shopping list. It's still one of your staples.",
}


# ---- Shared helpers ----

def _week(text: str | None) -> date:
    """A week from the URL (its Monday), or 404 for anything invalid."""
    week = planner.parse_week(text)
    if week is None:
        raise HTTPException(status_code=404, detail="We couldn't find that week.")
    return week


def _wants_json(request: Request) -> bool:
    # Our JavaScript sends this header; plain form posts (no JS) get redirects instead.
    return request.headers.get("x-requested-with") == "fetch"


def _int(value, default: int | None = None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _week_nav(week: date) -> dict:
    this_week = planner.monday_of(date.today())
    return {
        "week": week, "week_iso": week.isoformat(), "week_label": planner.week_label(week),
        "prev_week": (week - timedelta(days=7)).isoformat(),
        "next_week": (week + timedelta(days=7)).isoformat(),
        "this_week": this_week.isoformat(), "is_this_week": week == this_week,
    }


def _fragment(name: str, context: dict) -> str:
    """Render a partial template to a string for the page's JavaScript to swap in."""
    return templates.env.get_template(name).render(context)


# ---- Meal Planner ----

def _board_context(conn: sqlite3.Connection, week: date, session: auth.Session) -> dict:
    days = planner.get_plan(conn, week)
    return {
        **_week_nav(week), "days": days, "csrf_token": session.csrf_token,
        "planned_count": sum(1 for d in days if d["recipe"]),
        "list_status": shopping.list_status(conn, week),
        "plan_version": planner.versions(conn, week)[0],
    }


def _plan_done(request: Request, conn: sqlite3.Connection, week: date, session: auth.Session,
               anchor: str = "", error: str = "", status_code: int = 200):
    """After a plan change: fresh board HTML for JavaScript, or a redirect for plain forms."""
    if _wants_json(request):
        context = _board_context(conn, week, session)
        return JSONResponse({"version": context["plan_version"], "error": error,
                             "html": _fragment("_plan_board.html", context)}, status_code=status_code)
    if error:
        return render(request, "planner.html", {**_board_context(conn, week, session), "error": error,
                                                "nav_active": "plan"}, session, status_code)
    return redirect(f"/plan?week={week.isoformat()}{anchor}")


@router.get("/plan")
def plan_page(request: Request, week: str = "", msg: str = "",
              session: auth.Session = Depends(auth.require_session),
              conn: sqlite3.Connection = Depends(get_db)):
    wk = _week(week)
    return render(request, "planner.html", {
        **_board_context(conn, wk, session), "nav_active": "plan",
        "recipe_total": conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0],
        "message": "Nothing to shop for yet: plan a dinner first." if msg == "empty" else "",
    }, session)


@router.get("/plan/{week}/live")
def plan_live(week: str, since: int = -1, session: auth.Session = Depends(auth.require_session),
              conn: sqlite3.Connection = Depends(get_db)):
    """Polling endpoint: 204 if the plan hasn't changed, otherwise the new board."""
    wk = _week(week)
    if planner.versions(conn, wk)[0] == since:
        return Response(status_code=204)
    context = _board_context(conn, wk, session)
    return JSONResponse({"version": context["plan_version"], "html": _fragment("_plan_board.html", context)})


@router.get("/plan/{week}/day/{day}/choose")
def choose_recipe(request: Request, week: str, day: int, q: str = "",
                  session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    wk = _week(week)
    if not 0 <= day <= 6:
        raise HTTPException(status_code=404, detail="We couldn't find that day.")
    q = q.strip()[:100]
    current = conn.execute(
        "SELECT recipe_id FROM planned_meals WHERE week_start = ? AND day = ?", (wk.isoformat(), day)
    ).fetchone()
    return render(request, "plan_pick.html", {
        **_week_nav(wk), "nav_active": "plan", "day": day, "q": q,
        "day_name": planner.DAY_NAMES[day], "day_date": wk + timedelta(days=day),
        "recipes": planner.recipe_choices(conn, q),
        "current_id": current["recipe_id"] if current else None,
    }, session)


@router.post("/plan/{week}/day/{day}")
async def plan_set(request: Request, week: str, day: int,
                   session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    recipe_id = _int(form.get("recipe_id"))
    try:
        if recipe_id is None:
            raise planner.PlanError("Choose a recipe first.")
        planner.set_meal(conn, wk, day, recipe_id)
    except planner.PlanError as exc:
        return _plan_done(request, conn, wk, session, error=str(exc), status_code=422)
    return _plan_done(request, conn, wk, session, anchor=f"#day-{day}")


@router.post("/plan/{week}/day/{day}/clear")
async def plan_clear(request: Request, week: str, day: int,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    try:
        planner.clear_meal(conn, wk, day)
    except planner.PlanError as exc:
        return _plan_done(request, conn, wk, session, error=str(exc), status_code=422)
    return _plan_done(request, conn, wk, session, anchor=f"#day-{day}")


@router.post("/plan/{week}/move")
async def plan_move(request: Request, week: str,
                    session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    """Drag-and-drop and the Move menu both land here. Two dinners swap days."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    from_day, to_day = _int(form.get("from_day"), -1), _int(form.get("to_day"), -1)
    try:
        planner.move_meal(conn, wk, from_day, to_day)
    except planner.PlanError as exc:
        return _plan_done(request, conn, wk, session, error=str(exc), status_code=422)
    return _plan_done(request, conn, wk, session, anchor=f"#day-{to_day}")


@router.post("/plan/{week}/shopping-list")
async def plan_generate(request: Request, week: str,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    """Generate Shopping List / Update Shopping List."""
    await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    if not conn.execute("SELECT 1 FROM planned_meals WHERE week_start = ?", (wk.isoformat(),)).fetchone() \
            and shopping.list_status(conn, wk) == "none":
        return redirect(f"/plan?week={wk.isoformat()}&msg=empty")
    summary = shopping.generate_list(conn, wk)
    counts = "&".join(f"{k}={summary[k]}" for k in ("added", "staples", "changed", "removed"))
    return redirect(f"/shopping?week={wk.isoformat()}&built={'new' if summary['first'] else 'update'}&{counts}")


# ---- Shopping List ----

def _list_context(conn: sqlite3.Connection, week: date, session: auth.Session) -> dict:
    return {**_week_nav(week), "state": shopping.list_state(conn, week), "csrf_token": session.csrf_token}


def _summary_message(built: str, counts: dict[str, int]) -> str:
    """'List updated: 3 added, 1 changed.' built from numbers only."""
    if built not in ("new", "update"):
        return ""
    parts = []
    if counts["added"]:
        parts.append(f"{counts['added']} to buy")
    if counts["staples"]:
        parts.append(f"{counts['staples']} kitchen {'staple' if counts['staples'] == 1 else 'staples'} set aside")
    if counts["changed"]:
        parts.append(f"{counts['changed']} updated")
    if counts["removed"]:
        parts.append(f"{counts['removed']} no longer needed")
    if built == "new":
        return "Shopping list ready" + (f": {', '.join(parts)}." if parts else ".")
    return "Shopping list updated" + (f": {', '.join(parts)}." if parts else ". Nothing changed.")


@router.get("/shopping")
def shopping_page(request: Request, week: str = "", built: str = "", added: int = 0, staples: int = 0,
                  changed: int = 0, removed: int = 0,
                  session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    wk = _week(week)
    counts = {"added": added, "staples": staples, "changed": changed, "removed": removed}
    return render(request, "shopping.html", {
        **_list_context(conn, wk, session), "nav_active": "shopping",
        "message": _summary_message(built, counts),
    }, session)


@router.get("/shopping/{week}/live")
def shopping_live(week: str, since: int = -1, session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    """Polling endpoint: 204 if nothing changed since `since`, otherwise fresh list HTML.

    The version check is one tiny query, so phones can ask every few seconds cheaply.
    """
    wk = _week(week)
    if planner.versions(conn, wk)[1] == since:
        return Response(status_code=204)
    context = _list_context(conn, wk, session)
    return JSONResponse({"version": context["state"]["version"], "html": _fragment("_shopping_live.html", context)})


def _list_done(request: Request, conn: sqlite3.Connection, week: date, session: auth.Session,
               error: str = "", status_code: int = 200, rev: int | None = None):
    """After a list change: JSON with fresh HTML for JavaScript, or a redirect/page for plain forms."""
    if _wants_json(request):
        context = _list_context(conn, week, session)
        return JSONResponse({"version": context["state"]["version"], "rev": rev, "error": error,
                             "html": _fragment("_shopping_live.html", context)}, status_code=status_code)
    if error:
        return render(request, "shopping.html", {**_list_context(conn, week, session), "nav_active": "shopping",
                                                 "error": error}, session, status_code)
    return redirect(f"/shopping?week={week.isoformat()}")


def _list_error(exc: shopping.ListError) -> int:
    if isinstance(exc, shopping.Conflict):
        return 409
    if isinstance(exc, shopping.Missing):
        return 404
    return 422


@router.post("/shopping/{week}/items")
async def item_add(request: Request, week: str,
                   session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    try:
        shopping.add_item(conn, wk, str(form.get("name") or ""), str(form.get("quantity") or ""))
    except shopping.ListError as exc:
        return _list_done(request, conn, wk, session, str(exc), _list_error(exc))
    return _list_done(request, conn, wk, session)


@router.post("/shopping/{week}/items/{item_id}/{action}")
async def item_action(request: Request, week: str, item_id: int, action: str,
                      session: auth.Session = Depends(auth.require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    """status (Purchased / Already have / back to Need to Buy), edit, delete, or staple."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    rev = _int(form.get("rev"))
    new_rev = None
    try:
        if action == "status":
            new_rev = shopping.set_status(conn, wk, item_id, str(form.get("status") or ""), rev)
        elif action == "edit":
            new_rev = shopping.edit_item(conn, wk, item_id, str(form.get("name") or ""),
                                         str(form.get("quantity") or ""), rev)
        elif action == "delete":
            shopping.delete_item(conn, wk, item_id, rev)
        elif action == "staple":
            shopping.make_item_staple(conn, wk, item_id, rev)
        else:
            raise HTTPException(status_code=404, detail="We couldn't find that page.")
    except shopping.ListError as exc:
        return _list_done(request, conn, wk, session, str(exc), _list_error(exc))
    return _list_done(request, conn, wk, session, rev=new_rev)


@router.post("/shopping/{week}/clear-purchased")
async def clear_purchased(request: Request, week: str,
                          session: auth.Session = Depends(auth.require_session),
                          conn: sqlite3.Connection = Depends(get_db)):
    """Clear Purchased: hide everything already bought this week."""
    await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(week)
    shopping.clear_purchased(conn, wk)
    return _list_done(request, conn, wk, session)


# ---- Kitchen Staples ----

def _staples_page(request, conn, session, week: date, q: str = "", message: str = "", error: str = "",
                  name: str = "", status_code: int = 200):
    return render(request, "staples.html", {
        **_week_nav(week), "nav_active": "shopping", "q": q,
        "staples": shopping.list_staples(conn, q),
        "message": message, "error": error, "name": name,
    }, session, status_code)


@router.get("/staples")
def staples_page(request: Request, week: str = "", q: str = "", msg: str = "",
                 session: auth.Session = Depends(auth.require_session),
                 conn: sqlite3.Connection = Depends(get_db)):
    return _staples_page(request, conn, session, _week(week), q.strip()[:80], STAPLE_MESSAGES.get(msg, ""))


@router.post("/staples")
async def staple_add(request: Request, session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(str(form.get("week") or ""))
    name = str(form.get("name") or "")
    try:
        shopping.add_staple(conn, name)
    except shopping.ListError as exc:
        return _staples_page(request, conn, session, wk, error=str(exc), name=name[:80], status_code=422)
    return redirect(f"/staples?week={wk.isoformat()}&msg=added")


@router.post("/staples/{staple_id}/{action}")
async def staple_action(request: Request, staple_id: int, action: str,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    """edit, delete, or list (add to this week's shopping list)."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(str(form.get("week") or ""))
    try:
        if action == "edit":
            shopping.rename_staple(conn, staple_id, str(form.get("name") or ""))
            msg = "renamed"
        elif action == "delete":
            shopping.delete_staple(conn, staple_id)
            msg = "removed"
        elif action == "list":
            shopping.add_staple_to_list(conn, wk, staple_id)
            msg = "listed"
        else:
            raise HTTPException(status_code=404, detail="We couldn't find that page.")
    except shopping.ListError as exc:
        return _staples_page(request, conn, session, wk, error=str(exc), status_code=_list_error(exc))
    return redirect(f"/staples?week={wk.isoformat()}&msg={msg}")
