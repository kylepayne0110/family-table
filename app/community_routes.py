# Community pages (shared across Family Table), sharing a family recipe, and ideas from
# ingredients on hand.

# sqlite3 types the database connection.
import sqlite3

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# JSON for the page's JavaScript.
from fastapi.responses import JSONResponse

# Project modules.
from app import auth, community, drafts, planner, suggest, tables
from app.database import get_db
from app.model_gate import Busy
from app.ollama import AssistantUnavailable
from app.plan_routes import _week, _wants_json, household_today
from app.routes import recipe_or_404, redirect, render


router = APIRouter()


def _user(session: auth.Session) -> dict:
    return {"id": session.user_id, "display_name": session.display_name}


def _optional_scope(request, conn, session) -> tables.Scope | None:
    """Community is shared by everyone, so it works before you've started or joined a Table."""
    try:
        return tables.scope_for_get(request, conn, session)
    except tables.NoTable:
        return None


def _int(value) -> int | None:
    text = str(value or "")
    return int(text) if text.isdigit() and len(text) < 10 else None


# ---- Browse and search ----

@router.get("/community")
def community_home(request: Request, q: str = "", page: int = 1,
                   session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    current = _optional_scope(request, conn, session)
    q = q.strip()[:100]
    if q and auth.too_many(request, "search", f"user:{session.user_id}"):
        raise HTTPException(status_code=429, detail="That's a lot of searches. Wait a minute and try again.")
    results = community.search(conn, q, page)
    for item in results["items"]:
        item["saved_recipe"] = current and community.saved_copy(conn, item["id"], item["version"], current.table_id)
    return render(request, "community.html", {"results": results, "q": q, "nav_active": "community"}, session)


@router.get("/community/{listing_id}")
def community_detail(request: Request, listing_id: int, v: int | None = None, msg: str = "",
                     session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    current = _optional_scope(request, conn, session)
    listing = community.get_listing(conn, listing_id, v)
    if listing is None:
        raise HTTPException(status_code=404, detail="That community recipe isn't available. It may have been "
                                                    "withdrawn by the family that shared it.")
    mine = tables.my_tables(conn, session.user_id)
    return render(request, "community_detail.html", {
        "listing": listing, "nav_active": "community", "message": _COMMUNITY_MESSAGES.get(msg, ""),
        "saved_recipe": current and community.saved_copy(conn, listing_id, listing["shown_version"], current.table_id),
        "can_manage": community.can_manage(conn, listing, session.user_id), "tables_list": mine,
        "reasons": community.REPORT_REASONS, "request_key": "",
    }, session)


_COMMUNITY_MESSAGES = {"reported": "Thanks. We'll take a look; after a few reports a recipe is hidden until reviewed.",
                       "already-reported": "You've already reported this recipe."}


@router.post("/community/{listing_id}/save")
async def community_save(request: Request, listing_id: int, session: auth.Session = Depends(auth.require_session),
                         conn: sqlite3.Connection = Depends(get_db)):
    """'Save to Payne's Table': an independent copy of the version you were looking at. Repeat taps are safe."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    target = tables.scope_for_post(request, conn, session, form)
    version = _int(form.get("version"))
    if version is None:
        raise HTTPException(status_code=422, detail="Open the recipe again and try saving once more.")
    settings = request.app.state.settings
    try:
        recipe_id, created = community.save_to_table(conn, settings.upload_dir, listing_id, version,
                                                     target.table_id, session.user_id)
    except community.CommunityError as exc:
        if _wants_json(request):
            return JSONResponse({"error": str(exc)}, status_code=409)
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # "Save and plan for Tuesday" from the ideas page.
    week, day = str(form.get("plan_week") or ""), _int(form.get("plan_day"))
    if week and day is not None:
        wk = _week(week, household_today(request))
        try:
            planner.set_meal(conn, target.table_id, wk, day, recipe_id, household_today(request))
        except planner.PlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return redirect(f"/plan?week={wk.isoformat()}#day-{day}")
    if _wants_json(request):
        return JSONResponse({"recipe_id": recipe_id, "created": created, "table": target.name})
    return redirect(f"/recipes/{recipe_id}?msg={'copied' if created else 'already-copied'}")


@router.post("/community/{listing_id}/report")
async def community_report(request: Request, listing_id: int, session: auth.Session = Depends(auth.require_session),
                           conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    if auth.too_many(request, "publish", f"user:{session.user_id}"):
        raise HTTPException(status_code=429, detail="Too many reports and shares recently. Try again later.")
    try:
        new = community.report(conn, listing_id, session.user_id, str(form.get("reason") or ""),
                               str(form.get("details") or ""))
    except community.CommunityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not new:
        return redirect(f"/community/{listing_id}?msg=already-reported")
    listing = community.get_listing(conn, listing_id)
    return redirect(f"/community/{listing_id}?msg=reported" if listing else "/community")


# ---- Sharing a family recipe ----

def _share_page(request, conn, session, recipe, error: str = "", status_code: int = 200, credit: str | None = None,
                include_photo: bool | None = None):
    listing = community.listing_for_recipe(conn, recipe["id"])
    snap = community.snapshot_of(recipe, credit)
    return render(request, "share_preview.html", {
        "recipe": recipe, "snapshot": snap, "listing": listing, "error": error,
        "include_photo": bool(recipe["photo_filename"]) if include_photo is None else include_photo,
        "can_manage": listing is None or community.can_manage(conn, listing, session.user_id),
        "from_new": request.query_params.get("from") == "new",
    }, session, status_code)


@router.get("/recipes/{recipe_id}/share")
def share_preview(request: Request, recipe_id: int, session: auth.Session = Depends(auth.require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    recipe = recipe_or_404(conn, recipe_id, session, request)
    return _share_page(request, conn, session, recipe)


@router.post("/recipes/{recipe_id}/share/{action}")
async def share_action(request: Request, recipe_id: int, action: str,
                       session: auth.Session = Depends(auth.require_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    """publish, update ("Update community recipe"), or withdraw. Each is its own deliberate step."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    recipe = recipe_or_404(conn, recipe_id, session, request)   # rechecks membership at confirmation
    settings = request.app.state.settings
    credit = str(form.get("creator_credit") or "")
    include_photo = form.get("include_photo") == "on"
    if action in ("publish", "update") and auth.too_many(request, "publish", f"user:{session.user_id}"):
        return _share_page(request, conn, session, recipe, "Too many shares recently. Try again later.", 429)
    try:
        if action == "publish":
            listing_id, outcome = community.publish(conn, settings.upload_dir, recipe, _user(session), include_photo,
                                                    credit, _int(form.get("recipe_version")))
            return redirect(f"/recipes/{recipe_id}?msg=published")
        if action == "update":
            community.update_listing(conn, settings.upload_dir, recipe, _user(session), include_photo, credit,
                                     _int(form.get("listing_version")))
            return redirect(f"/recipes/{recipe_id}?msg=community-updated")
        if action == "withdraw":
            listing = community.listing_for_recipe(conn, recipe_id)
            if listing is None or form.get("confirm") != "yes":
                raise community.CommunityError("Confirm that you want to remove it from the community.")
            community.withdraw(conn, settings.upload_dir, listing["id"], session.user_id)
            return redirect(f"/recipes/{recipe_id}?msg=withdrawn")
    except community.CommunityError as exc:
        return _share_page(request, conn, session, recipe, str(exc), 409, credit, include_photo)
    raise HTTPException(status_code=404, detail="We couldn't find that page.")


# ---- Ideas from what's on hand ----

@router.get("/ideas")
def ideas(request: Request, have: str = "", source: str = "familiar", week: str = "", day: int | None = None,
          session: auth.Session = Depends(auth.require_session), conn: sqlite3.Connection = Depends(get_db)):
    """Pick from meals your Table has made, community recipes, or a new idea (asked for separately)."""
    current = tables.scope_for_get(request, conn, session)
    source = source if source in ("familiar", "community", "new") else "familiar"
    terms = suggest.on_hand_terms(have)
    today = household_today(request)
    plan_week = _week(week, today).isoformat() if week else ""
    results = []
    if terms and source == "familiar":
        results = suggest.familiar(conn, current.table_id, terms, today)
    elif terms and source == "community":
        if auth.too_many(request, "search", f"user:{session.user_id}"):
            raise HTTPException(status_code=429, detail="That's a lot of searches. Wait a minute and try again.")
        results = suggest.from_community(conn, current.table_id, terms)
    return render(request, "ideas.html", {
        "have": have[:300], "terms": terms, "source": source, "results": results, "nav_active": "plan",
        "plan_week": plan_week, "plan_day": day if day is not None and 0 <= day <= 6 else None,
        "day_name": planner.DAY_NAMES[day] if day is not None and 0 <= day <= 6 else "", "new_ideas": None,
        "error": "",
    }, session)


@router.post("/ideas/new")
async def ideas_new(request: Request, session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    """'Invent something new': three AI ideas from the typed ingredients only."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    tables.scope_for_post(request, conn, session, form)
    have = str(form.get("have") or "")[:300]
    terms = suggest.on_hand_terms(have)
    day = _int(form.get("plan_day"))
    context = {"have": have, "terms": terms, "source": "new", "results": [], "nav_active": "plan",
               "plan_week": str(form.get("plan_week") or "")[:10], "plan_day": day if day is not None and day <= 6 else None,
               "day_name": planner.DAY_NAMES[day] if day is not None and day <= 6 else "", "error": "", "new_ideas": []}
    if not terms:
        context["error"] = "Type a few ingredients you have first."
        return render(request, "ideas.html", context, session, 422)
    if auth.too_many(request, "model", f"user:{session.user_id}"):
        context["error"] = "That's a lot of ideas in a short time. Try again in a few minutes."
        return render(request, "ideas.html", context, session, 429)
    try:
        context["new_ideas"] = await request.app.state.model_gate.run(
            session.user_id, suggest.invent, request.app.state.ask_model, terms)
    except AssistantUnavailable:
        context["error"] = ("The local idea helper (Ollama) isn't answering right now. Meals you've made and "
                            "community recipes still work.")
    except Busy as exc:
        context["error"] = str(exc)
    return render(request, "ideas.html", context, session)


@router.post("/ideas/draft")
async def idea_to_draft(request: Request, session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    """Turn a chosen AI idea into an editable draft. Nothing is saved to the recipe book until you press Save."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    current = tables.scope_for_post(request, conn, session, form)
    terms = suggest.on_hand_terms(str(form.get("have") or ""))
    name = str(form.get("idea") or "")[:80]
    if auth.too_many(request, "model", f"user:{session.user_id}"):
        raise HTTPException(status_code=429, detail="That's a lot of ideas in a short time. Try again in a few minutes.")
    try:
        fields = await request.app.state.model_gate.run(session.user_id, suggest.draft_from_idea,
                                                        request.app.state.ask_model, name, terms)
    except Busy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    draft_id = drafts.create(conn, current.table_id, session.user_id, fields)
    return redirect(f"/recipes/new?draft={draft_id}")
