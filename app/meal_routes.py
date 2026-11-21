# One planned dinner: the recipe at tonight's serving count, the "Add ingredients"
# checklist, and the Cooking Assistant. Every page works as plain HTML forms; the page's
# JavaScript sends the same forms in the background and swaps in the fresh HTML.

# re checks request keys sent by the browser.
import re

# secrets makes a fresh key for each checklist form (stops double adds).
import secrets

# sqlite3 types the database connection.
import sqlite3

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# run_in_threadpool keeps the slow model call off the event loop.
from fastapi.concurrency import run_in_threadpool

# JSON for the page's JavaScript.
from fastapi.responses import JSONResponse

# Project modules.
from app import assistant, auth, meal_chat, meals, planner, shopping
from app.database import get_db
from app.plan_routes import _fragment, _wants_json, household_today
from app.routes import redirect, render
from app.scaling import clean_servings, scale_step, servings_factor


router = APIRouter()

_REQUEST_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MESSAGES = {
    "servings": "Servings updated for this dinner.",
    "yield": "Saved how many servings the recipe makes.",
    "added": "Added to the shopping list.",
    "updated": "Shopping list updated for the new serving count.",
    "duplicate": "Those were already added.",
}
PACKAGED_MESSAGES = {
    "planned": "Added to the plan. The shopping list wasn't changed.",
    "directions": "Directions saved.",
}


def _request_key(value) -> str | None:
    value = str(value or "")
    return value if _REQUEST_KEY.fullmatch(value) else None


def _meal_or_404(conn: sqlite3.Connection, meal_id: int, kind: str | None = "recipe") -> dict:
    """A planned dinner. Recipe-only features (servings, Add ingredients, assistant) pass kind="recipe"."""
    meal = planner.get_meal(conn, meal_id)
    if meal is None or (kind and meal["kind"] != kind):
        raise HTTPException(status_code=404, detail="That dinner isn't on the plan anymore.")
    return meal


def _context(request: Request, conn: sqlite3.Connection, meal: dict, session: auth.Session) -> dict:
    """Everything the planned recipe view, checklist, and assistant panel show."""
    recipe = meals.get_recipe(conn, meal["recipe_id"])
    factor = servings_factor(meal["servings"], meal["recipe_servings"])
    steps = [{"title": s["title"], "body": scale_step(s["body"], recipe["ingredients"], factor)}
             for s in recipe["steps"]]
    today = household_today(request)
    return {
        "meal": meal, "recipe": recipe, "lines": shopping.meal_lines(conn, meal), "steps": steps,
        "scaled": factor != 1, "checklist": shopping.meal_checklist(conn, meal),
        "messages": meal_chat.history(conn, meal["id"]),
        "request_key": secrets.token_urlsafe(16),
        "csrf_token": session.csrf_token, "is_past": meal["date"] < today, "is_tonight": meal["date"] == today,
        "week_iso": meal["week_start"],
    }


def _updated(request: Request, conn: sqlite3.Connection, meal_id: int, session: auth.Session,
             message: str = "", error: str = "", status_code: int = 200, anchor: str = ""):
    """After a change: fresh fragments for JavaScript, or back to the page for plain forms."""
    meal = _meal_or_404(conn, meal_id)
    if _wants_json(request):
        context = _context(request, conn, meal, session)
        return JSONResponse({
            "servings": meal["servings"], "message": message, "error": error,
            "recipe": _fragment("_planned_recipe.html", context),
            "checklist": _fragment("_meal_checklist.html", context),
            "log": _fragment("_assistant_log.html", context),
        }, status_code=status_code)
    if error:
        return render(request, "planned_meal.html", {**_context(request, conn, meal, session), "nav_active": "plan",
                                                      "error": error}, session, status_code)
    msg = next((k for k, v in MESSAGES.items() if v == message), "")
    return redirect(f"/plan/meals/{meal_id}{'?msg=' + msg if msg else ''}{anchor}")


# ---- The planned recipe ----

@router.get("/plan/meals/{meal_id}")
def planned_meal(request: Request, meal_id: int, msg: str = "",
                 session: auth.Session = Depends(auth.require_session),
                 conn: sqlite3.Connection = Depends(get_db)):
    meal = _meal_or_404(conn, meal_id, kind=None)
    if meal["kind"] == "packaged":
        from app.packaged_routes import packaged_page   # a scanned store-bought meal
        return packaged_page(request, conn, meal, session, PACKAGED_MESSAGES.get(msg, ""))
    return render(request, "planned_meal.html", {
        **_context(request, conn, meal, session), "nav_active": "plan", "message": MESSAGES.get(msg, ""),
    }, session)


@router.post("/plan/meals/{meal_id}/servings")
async def meal_servings(request: Request, meal_id: int,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    """The serving selector: change, or reset to the recipe's own yield. Only this dinner changes."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    _meal_or_404(conn, meal_id)
    reset = form.get("reset") is not None
    # The −/+ buttons send "servings"; typing a number (or pressing Enter) sends "servings_typed".
    servings = None if reset else clean_servings(form.get("servings") or form.get("servings_typed"))
    try:
        if not reset and servings is None:
            raise planner.PlanError("Choose from 1 to 100 servings.")
        planner.set_meal_servings(conn, meal_id, servings)
    except planner.PlanError as exc:
        return _updated(request, conn, meal_id, session, error=str(exc), status_code=422)
    return _updated(request, conn, meal_id, session, message=MESSAGES["servings"])


@router.post("/plan/meals/{meal_id}/yield")
async def recipe_yield(request: Request, meal_id: int,
                       session: auth.Session = Depends(auth.require_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    """An older recipe with no serving count: record how many it makes so it can be scaled."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    meal = _meal_or_404(conn, meal_id)
    servings = clean_servings(form.get("servings"))
    if servings is None:
        return _updated(request, conn, meal_id, session, error="Enter how many servings the recipe makes (1 to 100).",
                        status_code=422)
    planner.set_recipe_yield(conn, meal["recipe_id"], servings)
    return _updated(request, conn, meal_id, session, message=MESSAGES["yield"])


# ---- Add ingredients (one dinner at a time) ----

@router.get("/plan/meals/{meal_id}/ingredients")
def meal_checklist(request: Request, meal_id: int,
                   session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    meal = _meal_or_404(conn, meal_id)
    context = _context(request, conn, meal, session)
    if _wants_json(request):
        return JSONResponse({"html": _fragment("_meal_checklist.html", context)})
    return render(request, "meal_checklist.html", {**context, "nav_active": "plan"}, session)


def _checklist_done(request, conn, meal_id, session, message="", error="", status_code=200):
    if _wants_json(request) or error:
        return _updated(request, conn, meal_id, session, message, error, status_code)
    meal = _meal_or_404(conn, meal_id)
    return redirect(f"/shopping?week={meal['week_start']}")


@router.post("/plan/meals/{meal_id}/ingredients")
async def meal_add_ingredients(request: Request, meal_id: int,
                               session: auth.Session = Depends(auth.require_session),
                               conn: sqlite3.Connection = Depends(get_db)):
    """Add only the checked ingredients of this dinner to the shared list."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    _meal_or_404(conn, meal_id)
    indexes = [int(v) for v in form.getlist("ingredient") if str(v).isdigit()]
    seen = form.get("servings_seen")
    try:
        result = shopping.add_meal_ingredients(conn, meal_id, indexes, _request_key(form.get("request_key")),
                                               int(seen) if str(seen or "").isdigit() else None)
    except shopping.ListError as exc:
        return _checklist_done(request, conn, meal_id, session, error=str(exc),
                               status_code=409 if isinstance(exc, shopping.Conflict) else 422)
    if result["duplicate"]:
        return _checklist_done(request, conn, meal_id, session, MESSAGES["duplicate"])
    names = result["names"]
    message = f"Added {', '.join(names[:3])}{' and more' if len(names) > 3 else ''} to the shopping list."
    return _checklist_done(request, conn, meal_id, session, message)


@router.post("/plan/meals/{meal_id}/ingredients/update")
async def meal_update_ingredients(request: Request, meal_id: int,
                                  session: auth.Session = Depends(auth.require_session),
                                  conn: sqlite3.Connection = Depends(get_db)):
    """Only when asked: change this dinner's amounts on the list to its new serving count."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    _meal_or_404(conn, meal_id)
    try:
        result = shopping.update_meal_ingredients(conn, meal_id, _request_key(form.get("request_key")))
    except shopping.ListError as exc:
        return _checklist_done(request, conn, meal_id, session, error=str(exc), status_code=422)
    return _checklist_done(request, conn, meal_id, session,
                           MESSAGES["duplicate"] if result["duplicate"] else MESSAGES["updated"])


# ---- Cooking Assistant ----

@router.get("/plan/meals/{meal_id}/chat")
def chat_log(request: Request, meal_id: int,
             session: auth.Session = Depends(auth.require_session),
             conn: sqlite3.Connection = Depends(get_db)):
    """The saved conversation (the panel reloads it when the phone wakes up)."""
    return _updated(request, conn, meal_id, session) if _wants_json(request) else \
        redirect(f"/plan/meals/{meal_id}#assistant")


@router.post("/plan/meals/{meal_id}/chat")
async def chat_send(request: Request, meal_id: int,
                    session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    meal = _meal_or_404(conn, meal_id)
    text = " ".join(str(form.get("message") or "").split())[:assistant.MAX_MESSAGE]
    if not text:
        return _updated(request, conn, meal_id, session, error="Type or say a question first.", status_code=422,
                        anchor="#assistant")
    previous = meal_chat.history(conn, meal_id)
    if not meal_chat.save_user_message(conn, meal_id, text, _request_key(form.get("request_key"))):
        return _updated(request, conn, meal_id, session, anchor="#assistant")   # already sent (a retry)

    context = _context(request, conn, meal, session)
    ask = request.app.state.ask_model
    reply = await run_in_threadpool(assistant.respond, meal, context["recipe"], context["lines"],
                                    [s["body"] for s in context["steps"]], previous, text, ask)
    # The app makes every change itself, using the same functions as the page's own controls.
    try:
        if reply.recipe_yield:
            planner.set_recipe_yield(conn, meal["recipe_id"], reply.recipe_yield)
        if reply.servings or reply.reset_servings:
            planner.set_meal_servings(conn, meal_id, None if reply.reset_servings else reply.servings)
    except planner.PlanError as exc:
        reply = assistant.Reply(str(exc))
    if reply.kind == "servings" and shopping.meal_checklist(conn, _meal_or_404(conn, meal_id))["outdated"]:
        reply.body += (" Groceries you already added for this dinner are still for the old amount; "
                       "open Add ingredients if you want to update them.")
    meal_chat.save_reply(conn, meal, reply)
    return _updated(request, conn, meal_id, session, anchor="#assistant")


@router.post("/plan/meals/{meal_id}/proposals/{proposal_id}/{decision}")
async def proposal_decision(request: Request, meal_id: int, proposal_id: int, decision: str,
                            session: auth.Session = Depends(auth.require_session),
                            conn: sqlite3.Connection = Depends(get_db)):
    """Confirm saves the proposed wording to the recipe; Cancel leaves it alone."""
    await auth.read_form_with_csrf(request, session.csrf_token)
    if decision not in ("confirm", "cancel"):
        raise HTTPException(status_code=404, detail="We couldn't find that page.")
    meal = _meal_or_404(conn, meal_id)
    try:
        meal_chat.decide_proposal(conn, meal, proposal_id, decision == "confirm")
    except meal_chat.ProposalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _updated(request, conn, meal_id, session, anchor="#assistant")
