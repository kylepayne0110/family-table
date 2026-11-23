# Recipe drafts (kept while you work on the Add/Edit form) and the form's voice helper.
# None of these routes ever adds a recipe to the recipe book: only the form's Save does.

# json reads the form snapshot sent by the page.
import json

# logging records draft conflicts for the maintainer (ids only).
import logging

# re picks the chosen idea.
import re

# sqlite3 types the database connection.
import sqlite3

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

# Project modules.
from app import auth, drafts, meals, recipe_voice, suggest, tables
from app.database import get_db
from app.images import ImageError, process_photo
from app.model_gate import Busy
from app.ollama import AssistantUnavailable
from app.routes import register_temp_photo

router = APIRouter()
log = logging.getLogger("family_table.drafts")
MAX_JSON = 120_000


def _json_field(form, name: str) -> dict:
    raw = str(form.get(name) or "")
    if len(raw) > MAX_JSON:
        raise HTTPException(status_code=413, detail="That draft is too large.")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise HTTPException(status_code=422, detail="The draft couldn't be read.") from None
    return data if isinstance(data, dict) else {}


def _recipe_for_draft(conn, session, scope, form) -> int | None:
    value = str(form.get("recipe_id") or "")
    if not value.isdigit():
        return None
    recipe_id = int(value)
    if meals.recipe_table(conn, recipe_id) != scope.table_id:
        raise HTTPException(status_code=404, detail="We couldn't find that recipe.")
    return recipe_id


# ---- Drafts ----

@router.post("/recipes/drafts")
async def draft_create(request: Request, session: auth.Session = Depends(auth.require_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    scope = tables.scope_for_post(request, conn, session, form)
    draft_id = drafts.create(conn, scope.table_id, session.user_id, _json_field(form, "fields"),
                             _recipe_for_draft(conn, session, scope, form), _json_field(form, "voice"))
    return JSONResponse({"id": draft_id, "rev": 1})


@router.post("/recipes/drafts/{draft_id}")
async def draft_save(request: Request, draft_id: int, session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    scope = tables.scope_for_post(request, conn, session, form)
    rev = str(form.get("rev") or "")
    fields = _json_field(form, "fields") if form.get("fields") is not None else None
    voice = _json_field(form, "voice") if form.get("voice") is not None else None
    try:
        new_rev = drafts.save(conn, draft_id, session.user_id, scope.table_id, fields, int(rev) if rev.isdigit() else None,
                              voice)
    except drafts.DraftConflict as exc:
        log.info("draft-save conflict: user %s draft %s", session.user_id, draft_id)
        return JSONResponse({"error": str(exc), "conflict": True}, status_code=409)
    except drafts.DraftError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"id": draft_id, "rev": new_rev})


@router.post("/recipes/drafts/{draft_id}/discard")
async def draft_discard(request: Request, draft_id: int, session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    drafts.discard(conn, draft_id, session.user_id)
    return JSONResponse({"ok": True})


@router.post("/recipes/photo")
async def draft_photo(request: Request, session: auth.Session = Depends(auth.require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    """Keep a chosen photo with the draft (so a reload or a lost connection doesn't lose it). Private to you."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    scope = tables.scope_for_post(request, conn, session, form)
    upload = form.get("photo")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return JSONResponse({"error": "Choose a photo first."}, status_code=422)
    if auth.too_many(request, "upload", f"user:{session.user_id}"):
        return JSONResponse({"error": "Too many photo uploads. Try again later."}, status_code=429)
    settings = request.app.state.settings
    max_bytes = settings.max_upload_mb * 1024 * 1024
    raw = await upload.read(max_bytes + 1)
    try:
        filename = await run_in_threadpool(process_photo, raw, settings.import_dir, max_bytes)
    except ImageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    register_temp_photo(conn, filename, session.user_id, scope.table_id)
    return JSONResponse({"filename": filename, "url": f"/photos/import/{filename}"})


# ---- The voice helper ----

_ORDINALS = {"first": 0, "one": 0, "1": 0, "second": 1, "two": 1, "2": 1, "third": 2, "three": 2, "3": 2, "last": -1}


def _pick(ideas: list[str], text: str) -> str | None:
    plain = text.lower()
    for word, index in _ORDINALS.items():
        if re.search(rf"\b{word}\b", plain) and ideas:
            return ideas[index]
    best = max(ideas, key=lambda name: sum(1 for w in name.lower().split() if len(w) > 3 and w in plain), default=None)
    if best and any(len(w) > 3 and w in plain for w in best.lower().split()):
        return best
    return None


def _fill_ops(fields: dict, form: dict) -> list[dict]:
    """An AI draft as edits that only fill what's empty (the person's own words are kept)."""
    edits = recipe_voice.Edits(form, {})
    for name in ("name", "description", "servings", "prep_minutes", "cook_minutes"):
        if fields.get(name):
            edits.set(name, fields[name])
    for row in fields.get("ingredients", []):
        edits.ingredient(row.get("amount", ""), row.get("name", ""))
    for step in fields.get("steps", []):
        edits.step(step.get("body", ""))
    return edits.ops


@router.post("/recipes/voice")
async def voice_turn(request: Request, session: auth.Session = Depends(auth.require_session),
                     conn: sqlite3.Connection = Depends(get_db)):
    """One spoken turn on the recipe form: edits for the visible form, and maybe one question."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    tables.scope_for_post(request, conn, session, form)
    if auth.too_many(request, "voice", f"user:{session.user_id}"):
        return JSONResponse({"error": "That's a lot of talking in a few minutes. Give it a moment."}, status_code=429)
    text = " ".join(str(form.get("text") or "").split())[:recipe_voice.MAX_TEXT]
    mode = "next" if form.get("mode") == "next" else "fill"
    snapshot = recipe_voice.clean_form(_json_field(form, "form"))
    voice = recipe_voice.clean_voice(_json_field(form, "voice"))
    gate, ask = request.app.state.model_gate, request.app.state.ask_model

    def model_allowed() -> bool:
        return not auth.too_many(request, "model", f"user:{session.user_id}")

    # Choosing one of the invented ideas.
    pending = voice.get("pending") or {}
    if mode == "fill" and pending.get("field") == "pick_idea" and voice.get("ideas"):
        chosen = _pick(voice["ideas"], text)
        if chosen:
            voice["pending"] = None
            try:
                fields = await gate.run(session.user_id, suggest.draft_from_idea, ask, chosen,
                                        suggest.on_hand_terms(voice.get("on_hand", "")))
            except Busy as exc:
                return JSONResponse({"ops": [], "say": str(exc), "voice": voice})
            ops = _fill_ops(fields, snapshot)
            say = (f"I've filled in a starting point for {chosen}. It's an AI suggestion, not a tested recipe, so "
                   "check the amounts and steps. Tell me anything you'd change.") if not fields.get("offline") else \
                ("The assistant on your computer isn't answering, so I only added the name. You can keep describing it.")
            return JSONResponse({"ops": ops + [{"op": "mark_ai"}], "say": say, "voice": voice, "ask": ""})

    result = recipe_voice.turn(text, snapshot, voice, mode, ask if model_allowed() else None) if mode == "next" else None
    if result is None:
        # Model-backed understanding runs on the model's own pool (never on the page threads).
        def run():
            return recipe_voice.turn(text, snapshot, voice, mode, ask if model_allowed() else None)
        needs_model = mode == "fill" and len(text.split()) >= 4
        try:
            result = await gate.run(session.user_id, run) if needs_model else run()
        except Busy:
            result = recipe_voice.turn(text, snapshot, voice, mode, None)

    # "Help me invent something": three ideas from the model, then the person picks one.
    if (result.voice.get("pending") or {}).get("field") == "invent":
        terms = suggest.on_hand_terms(re.sub(r"^.*?\b(?:with|using|from|have)\b", "", text.lower()) if
                                      re.search(r"\b(?:with|using|from|have)\b", text.lower()) else "")
        try:
            ideas = await gate.run(session.user_id, suggest.invent, ask, terms or ["chicken", "rice"]) \
                if model_allowed() else []
        except (AssistantUnavailable, Busy):
            ideas = []
        if not ideas:
            result.voice["pending"] = None
            result.say = ("I can't reach the idea helper on your computer right now. Tell me what you'd like to make "
                          "and I'll write it down as you go.")
        else:
            names = [i["name"] for i in ideas]
            result.voice.update({"ideas": names, "on_hand": ", ".join(terms),
                                 "pending": {"field": "pick_idea"}})
            spoken = "; ".join(f"{n + 1}, {i['name']}: {i['summary']}" for n, i in enumerate(ideas))
            result.say = f"Here are a few ideas. {spoken}. Which one sounds good?"
    # A spoken note is signed by whoever is talking, unless the form already says who it's from.
    if any(op.get("field") == "note_body" for op in result.ops) and not snapshot.get("note_author", "").strip():
        result.ops.append({"op": "set", "field": "note_author", "value": session.display_name[:40]})
    return JSONResponse({"ops": result.ops, "say": result.say, "voice": result.voice, "ask": result.ask,
                         "done": result.done})
