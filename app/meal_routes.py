# One planned dinner: the recipe at tonight's serving count, the "Add ingredients"
# checklist, and the Cooking Assistant. Every page works as plain HTML forms; the page's
# JavaScript sends the same forms in the background and swaps in the fresh HTML.

# json stores reply details; re checks request keys sent by the browser.
import json
import re

# secrets makes a fresh key for each checklist form (stops double adds).
import secrets

# sqlite3 types the database connection.
import sqlite3

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# run_in_threadpool keeps the slow model call off the event loop.

# JSON for the page's JavaScript.
from fastapi.responses import JSONResponse

# Project modules.
from app import assistant, auth, cook, meal_chat, meals, planner, shopping, tables
from app.model_gate import Busy
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


def _meal_or_404(conn: sqlite3.Connection, meal_id: int, session: auth.Session, kind: str | None = "recipe",
                 request: Request | None = None) -> dict:
    """A planned dinner in one of your Tables. Recipe-only features (servings, Add ingredients,
    assistant) pass kind="recipe". Someone else's dinner is simply 'not found'."""
    meal = planner.get_meal(conn, meal_id)
    if meal is None or not tables.is_member(conn, meal["table_id"], session.user_id):
        raise HTTPException(status_code=404, detail="That dinner isn't on the plan anymore.")
    if kind and meal["kind"] != kind:
        raise HTTPException(status_code=404, detail="That dinner isn't on the plan anymore.")
    if request is not None:
        request.state.scope = tables.resolve(conn, session, meal["table_id"], strict=True)
        if session.active_table_id != meal["table_id"]:
            tables.remember(conn, session, meal["table_id"])
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
        "week_iso": meal["week_start"], "table_id": meal["table_id"],
    }


def _updated(request: Request, conn: sqlite3.Connection, meal_id: int, session: auth.Session,
             message: str = "", error: str = "", status_code: int = 200, anchor: str = ""):
    """After a change: fresh fragments for JavaScript, or back to the page for plain forms."""
    meal = _meal_or_404(conn, meal_id, session, request=request)
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
    meal = _meal_or_404(conn, meal_id, session, kind=None, request=request)
    if meal["kind"] == "packaged":
        from app.packaged_routes import packaged_page   # a scanned store-bought meal
        return packaged_page(request, conn, meal, session, PACKAGED_MESSAGES.get(msg, ""))
    context = _context(request, conn, meal, session)
    return render(request, "planned_meal.html", {
        **context, **_cook_context(conn, meal, context["steps"]), "nav_active": "plan", "message": MESSAGES.get(msg, ""),
    }, session)


def _cook_context(conn: sqlite3.Connection, meal: dict, steps: list[dict]) -> dict:
    step = min(cook.get_step(conn, meal["id"]), len(steps))
    return {"cook_step": step, "cook_total": len(steps), "cook_greeting": cook.greeting(step, len(steps))}


@router.post("/plan/meals/{meal_id}/servings")
async def meal_servings(request: Request, meal_id: int,
                        session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    """The serving selector: change, or reset to the recipe's own yield. Only this dinner changes."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    _meal_or_404(conn, meal_id, session, request=request)
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
    meal = _meal_or_404(conn, meal_id, session, request=request)
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
    meal = _meal_or_404(conn, meal_id, session, request=request)
    context = _context(request, conn, meal, session)
    if _wants_json(request):
        return JSONResponse({"html": _fragment("_meal_checklist.html", context)})
    return render(request, "meal_checklist.html", {**context, "nav_active": "plan"}, session)


def _checklist_done(request, conn, meal_id, session, message="", error="", status_code=200):
    if _wants_json(request) or error:
        return _updated(request, conn, meal_id, session, message, error, status_code)
    meal = _meal_or_404(conn, meal_id, session, request=request)
    return redirect(f"/shopping?week={meal['week_start']}")


@router.post("/plan/meals/{meal_id}/ingredients")
async def meal_add_ingredients(request: Request, meal_id: int,
                               session: auth.Session = Depends(auth.require_session),
                               conn: sqlite3.Connection = Depends(get_db)):
    """Add only the checked ingredients of this dinner to the shared list."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    _meal_or_404(conn, meal_id, session, request=request)
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
    _meal_or_404(conn, meal_id, session, request=request)
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
    meal = _meal_or_404(conn, meal_id, session, request=request)
    text = " ".join(str(form.get("message") or "").split())[:assistant.MAX_MESSAGE]
    if not text:
        return _updated(request, conn, meal_id, session, error="Type or say a question first.", status_code=422,
                        anchor="#assistant")
    previous = meal_chat.history(conn, meal_id)
    if not meal_chat.save_user_message(conn, meal_id, text, _request_key(form.get("request_key"))):
        return _updated(request, conn, meal_id, session, anchor="#assistant")   # already sent (a retry)

    context = _context(request, conn, meal, session)
    reply = await _assistant_reply(request, session, meal, context, previous, text)
    _apply_reply_actions(request, conn, session, meal, reply)
    meal_chat.save_reply(conn, meal, reply)
    return _updated(request, conn, meal_id, session, anchor="#assistant")


async def _assistant_reply(request: Request, session: auth.Session, meal: dict, context: dict, history: list[dict],
                           text: str, extra: str = "") -> assistant.Reply:
    """The app's own answer if it has one; otherwise the model, on its own pool (never holding a transaction)."""
    steps = [s["body"] for s in context["steps"]]
    local = assistant.local_reply(meal, context["recipe"], context["lines"], history, text)
    if local is not None:
        return local
    if auth.too_many(request, "model", f"user:{session.user_id}"):
        return assistant.Reply("That's a lot of questions in a few minutes. Give me a moment and ask again.", kind="busy")
    try:
        return await request.app.state.model_gate.run(
            session.user_id, assistant.ask_model, request.app.state.ask_model, meal, context["recipe"],
            context["lines"], steps, history, text, extra)
    except Busy as exc:
        return assistant.Reply(str(exc), kind="busy")


def _apply_reply_actions(request: Request, conn: sqlite3.Connection, session: auth.Session, meal: dict,
                         reply: assistant.Reply) -> None:
    """The app makes every change itself, using the same functions as the page's own controls."""
    try:
        if reply.recipe_yield:
            planner.set_recipe_yield(conn, meal["recipe_id"], reply.recipe_yield)
        if reply.servings or reply.reset_servings:
            planner.set_meal_servings(conn, meal["id"], None if reply.reset_servings else reply.servings)
    except planner.PlanError as exc:
        reply.body, reply.kind, reply.servings, reply.reset_servings = str(exc), "text", None, False
        return
    if reply.kind == "servings" and shopping.meal_checklist(conn, _meal_or_404(conn, meal["id"], session))["outdated"]:
        reply.body += (" Groceries you already added for this dinner are still for the old amount; "
                       "open Add ingredients if you want to update them.")


@router.post("/plan/meals/{meal_id}/proposals/{proposal_id}/{decision}")
async def proposal_decision(request: Request, meal_id: int, proposal_id: int, decision: str,
                            session: auth.Session = Depends(auth.require_session),
                            conn: sqlite3.Connection = Depends(get_db)):
    """Confirm saves the proposed wording to the recipe; Cancel leaves it alone."""
    await auth.read_form_with_csrf(request, session.csrf_token)
    if decision not in ("confirm", "cancel"):
        raise HTTPException(status_code=404, detail="We couldn't find that page.")
    meal = _meal_or_404(conn, meal_id, session, request=request)
    try:
        meal_chat.decide_proposal(conn, meal, proposal_id, decision == "confirm")
    except meal_chat.ProposalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _updated(request, conn, meal_id, session, anchor="#assistant")


# ---- Cook with me (spoken conversation) ----

def _packaged_steps(item: dict) -> list[dict]:
    """A packaged meal's directions as steps: only what the app has as text, never invented."""
    steps = []
    for section, label in (("oven", "Oven"), ("microwave", "Microwave"), ("other", "Directions")):
        text = (item or {}).get(f"{section}_directions") or ""
        if text.strip():
            steps.append({"title": label, "body": " ".join(text.split())})
    return steps


def _cook_state(conn: sqlite3.Connection, meal: dict, steps: list[dict]) -> dict:
    step = min(cook.get_step(conn, meal["id"]), len(steps))
    return {"step": step, "total": len(steps), "greeting": cook.greeting(step, len(steps)),
            "say_greeting": cook.speakable(cook.greeting(step, len(steps))),
            "steps": [cook.step_text(steps, n) for n in range(1, len(steps) + 1)]}


@router.get("/plan/meals/{meal_id}/cook")
def cook_state(request: Request, meal_id: int, session: auth.Session = Depends(auth.require_session),
               conn: sqlite3.Connection = Depends(get_db)):
    """Where the cook is (restored when the page is reopened, or after the phone was locked)."""
    meal = _meal_or_404(conn, meal_id, session, kind=None, request=request)
    if meal["kind"] == "packaged":
        from app import packaged
        steps = _packaged_steps(packaged.get_packaged(conn, meal_id))
    else:
        steps = _context(request, conn, meal, session)["steps"]
    return JSONResponse(_cook_state(conn, meal, steps))


def _cook_json(request, conn, session, meal, steps, reply: "cook.CookReply", servings_changed: bool = False):
    total = len(steps)
    out = {"say": reply.say, "show": reply.show, "step": reply.step, "total": total, "kind": reply.kind,
           "proposal_id": reply.proposal_id, "steps": [cook.step_text(steps, n) for n in range(1, total + 1)]}
    if meal["kind"] == "recipe":
        fresh = _meal_or_404(conn, meal["id"], session)
        context = _context(request, conn, fresh, session)
        out["servings"] = fresh["servings"]
        out["log"] = _fragment("_assistant_log.html", context)
        if servings_changed:
            out["recipe"] = _fragment("_planned_recipe.html", context)
    return JSONResponse(out)


@router.post("/plan/meals/{meal_id}/cook")
async def cook_turn(request: Request, meal_id: int, session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    """One spoken (or typed) turn in Cook with me. Every turn is saved with the dinner's conversation."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    meal = _meal_or_404(conn, meal_id, session, kind=None, request=request)
    text = " ".join(str(form.get("text") or "").split())[:assistant.MAX_MESSAGE]
    key = _request_key(form.get("request_key"))
    heard = str(form.get("proposal_id") or "")
    heard_proposal = int(heard) if heard.isdigit() else None
    if not text:
        return JSONResponse({"error": "I didn't catch anything."}, status_code=422)
    if auth.too_many(request, "voice", f"user:{session.user_id}"):
        return JSONResponse({"error": "That's a lot of talking in a few minutes. Give it a moment."}, status_code=429)

    if meal["kind"] == "packaged":
        return _packaged_turn(request, conn, session, meal, text, key)

    context = _context(request, conn, meal, session)
    steps = context["steps"]
    step = min(cook.get_step(conn, meal_id), len(steps))
    history = meal_chat.history(conn, meal_id)
    if not meal_chat.save_user_message(conn, meal_id, text, key):
        # The same turn arrived twice (a retry or a repeated transcript): answer once, change nothing.
        reply = cook.CookReply("", "", step, "duplicate")
        return _cook_json(request, conn, session, meal, steps, reply)

    def finish(reply: "cook.CookReply", kind: str = "text", servings_changed: bool = False):
        if reply.show:
            with conn:
                conn.execute("INSERT INTO meal_messages (meal_id, role, body, kind, data_json) VALUES (?, 'assistant', ?, ?, ?)",
                             (meal_id, reply.show, kind, json.dumps({"step": reply.step} if kind == "step" else {})))
        if reply.step != step:
            cook.set_step(conn, meal_id, reply.step)
        return _cook_json(request, conn, session, meal, steps, reply, servings_changed)

    # A spoken yes/no about a pending recipe change: only ever the narrow, safe path.
    pending = cook.pending_proposal(conn, meal_id)
    if cook.is_confirm(text) or cook.is_cancel(text):
        if pending is None:
            msg = "There's nothing waiting to be saved." if cook.is_confirm(text) else "Okay."
            return finish(cook.CookReply(msg, msg, step, "answer"))
        if cook.is_cancel(text):
            meal_chat.decide_proposal(conn, meal, pending["id"], confirm=False)
            msg = "Okay, I left the saved recipe as it was."
            return _cook_json(request, conn, session, meal, steps, cook.CookReply(msg, msg, step, "confirm"))
        if heard_proposal != pending["id"] or not pending["is_latest"] or \
                pending["age_minutes"] > cook.MAX_PROPOSAL_AGE_MINUTES:
            msg = ("I won't change the saved recipe without a clear yes right after hearing the change. "
                   "Tap Confirm on the screen, or ask me to read the change again.")
            return finish(cook.CookReply(msg, msg, step, "answer"))
        status = meal_chat.decide_proposal(conn, meal, pending["id"], confirm=True)
        msg = ("Saved. The recipe box has the new version." if status == "confirmed" else
               "The recipe was edited since I suggested that, so I didn't change anything.")
        return _cook_json(request, conn, session, meal, steps, cook.CookReply(msg, msg, step, "confirm"))
    if pending and re.search(r"\b(read|say|tell me)\b.*\b(change|that)\b.*\bagain\b|^what (?:change|was the change)",
                             text.lower()):
        proposal = next((m["proposal"] for m in reversed(history) if m.get("proposal") and
                         m["proposal"]["id"] == pending["id"]), None)
        if proposal:
            msg = cook.proposal_speech(proposal["changes"])
            with conn:   # read again: it becomes the latest thing said, so a yes right after it counts
                message_id = conn.execute(
                    "INSERT INTO meal_messages (meal_id, role, body, kind) VALUES (?, 'assistant', ?, 'proposal')",
                    (meal_id, "Here's the change again. Nothing is saved until you confirm.")).lastrowid
                conn.execute("UPDATE recipe_edit_proposals SET message_id = ? WHERE id = ?", (message_id, pending["id"]))
            return _cook_json(request, conn, session, meal, steps,
                              cook.CookReply(msg, msg, step, "proposal", proposal_id=pending["id"]))

    plain = cook._plain(text)
    guided = cook.guide(steps, text, step, history)
    if guided is None and cook._WHOLE.search(plain):
        guided = cook.whole_recipe(context["lines"], steps, meal["servings"])
        guided.step = step
    elif guided is None and cook._INGREDIENTS.search(plain):
        guided = cook.ingredient_list(context["lines"], meal["servings"])
        guided.step = step
    if guided is not None:
        return finish(guided, "step" if guided.kind == "step" else "clarify" if guided.kind == "clarify" else "text")

    # A question or request: the same assistant as the typed chat (amounts, conversions, servings, edits, model).
    current = cook.step_text(steps, step) if step else ""
    extra = ("\nSPOKEN CONVERSATION: the cook is listening with busy hands. Reply in one or two short sentences."
             + (f" They are on step {step} of {len(steps)}: {current}" if step else ""))
    reply = await _assistant_reply(request, session, meal, context, history, text, extra)
    _apply_reply_actions(request, conn, session, meal, reply)
    meal_chat.save_reply(conn, meal, reply)
    say = cook.speakable(reply.body)
    proposal_id = None
    if reply.kind == "proposal" and reply.changes:
        pending = cook.pending_proposal(conn, meal_id)
        proposal_id = pending["id"] if pending else None
        say = cook.speakable(reply.body.split(" Nothing is saved")[0]) + " " + cook.proposal_speech(reply.changes)
    elif step and reply.kind in ("text", "servings"):
        say += f" We're still on step {step}."
    kind = {"proposal": "proposal", "clarify": "clarify", "offline": "offline", "busy": "busy"}.get(reply.kind, "answer")
    return _cook_json(request, conn, session, meal, steps, cook.CookReply(say, reply.body, step, kind, proposal_id),
                      servings_changed=bool(reply.servings or reply.reset_servings or reply.recipe_yield))


def _packaged_turn(request, conn, session, meal, text: str, key: str | None):
    """Cook with me for a store-bought meal: only the directions the app has as text; never invented."""
    from app import packaged
    item = packaged.get_packaged(conn, meal["id"])
    steps = _packaged_steps(item)
    step = min(cook.get_step(conn, meal["id"]), len(steps))
    history = meal_chat.history(conn, meal["id"])
    if not meal_chat.save_user_message(conn, meal["id"], text, key):
        return _cook_json(request, conn, session, meal, steps, cook.CookReply("", "", step, "duplicate"))
    reply = cook.guide(steps, text, step, history) if steps else None
    if reply is None and steps and cook._WHOLE.search(cook._plain(text)):
        joined = " ".join(f"{s['title']}: {s['body']}" for s in steps)
        reply = cook.CookReply(cook.speakable(joined), joined, step, "answer")
    if reply is None:
        if not steps and item and item.get("label_photo"):
            msg = ("The directions for this one are only in the photo of the box, and I can't read photos. "
                   "Please check the label. You can tap the photo to enlarge it.")
        elif not steps:
            msg = "There are no cooking directions saved for this meal. Please check the package."
        else:
            msg = ("I can only read out the directions saved for this meal. For anything else, check the package. "
                   "Say next or repeat to go through the directions.")
        reply = cook.CookReply(msg, msg, step, "answer")
    with conn:
        conn.execute("INSERT INTO meal_messages (meal_id, role, body, kind) VALUES (?, 'assistant', ?, ?)",
                     (meal["id"], reply.show, "step" if reply.kind == "step" else "text"))
    if reply.step != step:
        cook.set_step(conn, meal["id"], reply.step)
    return _cook_json(request, conn, session, meal, steps, reply)
