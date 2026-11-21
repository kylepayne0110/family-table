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

def household_today(request: Request) -> date:
    """Today in the household's time zone (HOUSEHOLD_TIMEZONE), not the server's."""
    return request.app.state.clock.today()


def _week(text: str | None, today: date | None = None) -> date:
    """A week from the URL (its Monday), or 404 for anything invalid."""
    week = planner.parse_week(text, today)
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


def _week_nav(week: date, today: date) -> dict:
    this_week = planner.monday_of(today)
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

def _board_context(conn: sqlite3.Connection, week: date, session: auth.Session, today: date) -> dict:
    days = planner.get_plan(conn, week, today)
    active = [d for d in days if not d["is_past"]]
    return {
        **_week_nav(week, today), "days": days, "csrf_token": session.csrf_token,
        "today_iso": today.isoformat(),
        "planned_count": sum(1 for d in active if d["recipe"]),
        "open_days": len(active),
        # Dinners from days that are over: kept as history, off the active board.
        "history": [d for d in days if d["is_past"] and d["recipe"]],
        "plan_version": planner.versions(conn, week)[0],
    }


def _plan_done(request: Request, conn: sqlite3.Connection, week: date, session: auth.Session,
               anchor: str = "", error: str = "", status_code: int = 200):
    """After a plan change: fresh board HTML for JavaScript, or a redirect for plain forms."""
    today = household_today(request)
    if _wants_json(request):
        context = _board_context(conn, week, session, today)
        return JSONResponse({"version": context["plan_version"], "today": context["today_iso"], "error": error,
                             "html": _fragment("_plan_board.html", context)}, status_code=status_code)
    if error:
        return render(request, "planner.html", {**_board_context(conn, week, session, today), "error": error,
                                                "nav_active": "plan"}, session, status_code)
    return redirect(f"/plan?week={week.isoformat()}{anchor}")


@router.get("/plan")
def plan_page(request: Request, week: str = "",
              session: auth.Session = Depends(auth.require_session),
              conn: sqlite3.Connection = Depends(get_db)):
    today = household_today(request)
    wk = _week(week, today)
    return render(request, "planner.html", {
        **_board_context(conn, wk, session, today), "nav_active": "plan",
        "recipe_total": conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0],
    }, session)


@router.get("/plan/{week}/live")
def plan_live(request: Request, week: str, since: int = -1, today: str = "",
              session: auth.Session = Depends(auth.require_session),
              conn: sqlite3.Connection = Depends(get_db)):
    """Polling endpoint: 204 if nothing changed, otherwise the new board.

    `today` is the date the phone's board was drawn for. After midnight (household time)
    the board is sent again even though the plan didn't change, so yesterday's dinner leaves.
    """
    now = household_today(request)
    wk = _week(week, now)
    if planner.versions(conn, wk)[0] == since and today in ("", now.isoformat()):
        return Response(status_code=204)
    context = _board_context(conn, wk, session, now)
    return JSONResponse({"version": context["plan_version"], "today": context["today_iso"],
                         "html": _fragment("_plan_board.html", context)})


@router.get("/plan/{week}/day/{day}/choose")
def choose_recipe(request: Request, week: str, day: int, q: str = "",
                  session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    today = household_today(request)
    wk = _week(week, today)
    if not 0 <= day <= 6:
        raise HTTPException(status_code=404, detail="We couldn't find that day.")
    if planner.is_past(wk, day, today):
        raise HTTPException(status_code=404, detail="That day is over, so its dinner stays in the history.")
    q = q.strip()[:100]
    current = conn.execute(
        "SELECT recipe_id FROM planned_meals WHERE week_start = ? AND day = ?", (wk.isoformat(), day)
    ).fetchone()
    return render(request, "plan_pick.html", {
        **_week_nav(wk, today), "nav_active": "plan", "day": day, "q": q,
        "day_name": planner.DAY_NAMES[day], "day_date": wk + timedelta(days=day),
        "recipes": planner.recipe_choices(conn, q),
        "current_id": current["recipe_id"] if current else None,
    }, session)


@router.post("/plan/{week}/day/{day}")
async def plan_set(request: Request, week: str, day: int,
                   session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    today = household_today(request)
    wk = _week(week, today)
    recipe_id = _int(form.get("recipe_id"))
    try:
        if recipe_id is None:
            raise planner.PlanError("Choose a recipe first.")
        planner.set_meal(conn, wk, day, recipe_id, today)
    except planner.PlanError as exc:
        return _plan_done(request, conn, wk, session, error=str(exc), status_code=422)
    return _plan_done(request, conn, wk, session, anchor=f"#day-{day}")


@router.post("/plan/{week}/day/{day}/clear")
async def plan_clear(request: Request, week: str, day: int,
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    today = household_today(request)
    wk = _week(week, today)
    try:
        planner.clear_meal(conn, wk, day, today)
    except planner.PlanError as exc:
        return _plan_done(request, conn, wk, session, error=str(exc), status_code=422)
    return _plan_done(request, conn, wk, session, anchor=f"#day-{day}")


@router.post("/plan/{week}/move")
async def plan_move(request: Request, week: str,
                    session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    """Drag-and-drop and the Move menu both land here. Two dinners swap days."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    today = household_today(request)
    wk = _week(week, today)
    from_day, to_day = _int(form.get("from_day"), -1), _int(form.get("to_day"), -1)
    try:
        planner.move_meal(conn, wk, from_day, to_day, today)
    except planner.PlanError as exc:
        return _plan_done(request, conn, wk, session, error=str(exc), status_code=422)
    return _plan_done(request, conn, wk, session, anchor=f"#day-{to_day}")


# ---- Shopping List ----

def _list_context(conn: sqlite3.Connection, week: date, session: auth.Session, today: date) -> dict:
    return {**_week_nav(week, today), "state": shopping.list_state(conn, week), "csrf_token": session.csrf_token}


@router.get("/shopping")
def shopping_page(request: Request, week: str = "",
                  session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    today = household_today(request)
    wk = _week(week, today)
    return render(request, "shopping.html", {
        **_list_context(conn, wk, session, today), "nav_active": "shopping",
    }, session)


@router.get("/shopping/{week}/live")
def shopping_live(request: Request, week: str, since: int = -1, session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    """Polling endpoint: 204 if nothing changed since `since`, otherwise fresh list HTML.

    The version check is one tiny query, so phones can ask every few seconds cheaply.
    """
    wk = _week(week, household_today(request))
    if planner.versions(conn, wk)[1] == since:
        return Response(status_code=204)
    context = _list_context(conn, wk, session, household_today(request))
    return JSONResponse({"version": context["state"]["version"], "html": _fragment("_shopping_live.html", context)})


def _list_done(request: Request, conn: sqlite3.Connection, week: date, session: auth.Session,
               error: str = "", status_code: int = 200, rev: int | None = None):
    """After a list change: JSON with fresh HTML for JavaScript, or a redirect/page for plain forms."""
    today = household_today(request)
    if _wants_json(request):
        context = _list_context(conn, week, session, today)
        return JSONResponse({"version": context["state"]["version"], "rev": rev, "error": error,
                             "html": _fragment("_shopping_live.html", context)}, status_code=status_code)
    if error:
        return render(request, "shopping.html", {**_list_context(conn, week, session, today), "nav_active": "shopping",
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
    wk = _week(week, household_today(request))
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
    wk = _week(week, household_today(request))
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
    wk = _week(week, household_today(request))
    shopping.clear_purchased(conn, wk)
    return _list_done(request, conn, wk, session)


# ---- Kitchen Staples ----

def _staples_page(request, conn, session, week: date, q: str = "", message: str = "", error: str = "",
                  name: str = "", status_code: int = 200):
    return render(request, "staples.html", {
        **_week_nav(week, household_today(request)), "nav_active": "shopping", "q": q,
        "staples": shopping.list_staples(conn, q),
        "message": message, "error": error, "name": name,
    }, session, status_code)


@router.get("/staples")
def staples_page(request: Request, week: str = "", q: str = "", msg: str = "",
                 session: auth.Session = Depends(auth.require_session),
                 conn: sqlite3.Connection = Depends(get_db)):
    return _staples_page(request, conn, session, _week(week, household_today(request)), q.strip()[:80], STAPLE_MESSAGES.get(msg, ""))


@router.post("/staples")
async def staple_add(request: Request, session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    wk = _week(str(form.get("week") or ""), household_today(request))
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
    wk = _week(str(form.get("week") or ""), household_today(request))
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
