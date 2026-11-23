# Welcome, Tables (switch, settings, invites, members), and the Account page.

# sqlite3 types the database connection.
import sqlite3

# quote builds invite links safely.
from urllib.parse import quote

# FastAPI routing, dependencies, and errors.
from fastapi import APIRouter, Depends, HTTPException, Request

# run_in_threadpool keeps password hashing off the event loop.
from fastapi.concurrency import run_in_threadpool

# Project modules.
from app import accounts, auth, tables
from app.database import get_db
from app.routes import SECTIONS, _public_page, redirect, render


router = APIRouter()

TABLE_MESSAGES = {
    "renamed": "Table renamed.",
    "invited": "Invite link ready. Send it to one person; it works for 7 days.",
    "revoked": "Invite link cancelled.",
    "removed": "Removed from this Table.",
    "created": "Your Table is ready.",
}
ACCOUNT_MESSAGES = {
    "name": "Name saved.",
    "password": "Password changed. Other devices were signed out.",
    "signed-out": "That device was signed out.",
    "signed-out-others": "Every other device was signed out.",
}


def _session_scope(request: Request, conn: sqlite3.Connection, session: auth.Session) -> tables.Scope:
    try:
        return tables.scope_for_get(request, conn, session)
    except tables.NoTable:
        raise HTTPException(status_code=404, detail="You're not in a Table yet.") from None


# ---- Welcome: a first Table ----

@router.get("/welcome")
def welcome(request: Request, session: auth.Session = Depends(auth.require_session),
            conn: sqlite3.Connection = Depends(get_db)):
    mine = tables.my_tables(conn, session.user_id)
    return render(request, "welcome.html", {"has_tables": bool(mine), "name": "My Table", "error": "",
                                            "nav_active": ""}, session)


@router.post("/tables/new")
async def new_table(request: Request, session: auth.Session = Depends(auth.require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    name = str(form.get("name") or "")
    try:
        table_id = tables.create_table(conn, session.user_id, name)
    except tables.TableError as exc:
        return render(request, "welcome.html", {"has_tables": bool(tables.my_tables(conn, session.user_id)),
                                                "name": tables.clean_name(name), "error": str(exc),
                                                "nav_active": ""}, session, 422)
    tables.remember(conn, session, table_id)
    return redirect("/plan?created=1")


@router.post("/tables/switch")
async def switch_table(request: Request, session: auth.Session = Depends(auth.require_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    """Make another of your Tables current, staying in the same section where practical."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    target = tables.resolve(conn, session, form.get("switch_to"), strict=True)
    tables.remember(conn, session, target.table_id)
    section = str(form.get("section") or "")
    return redirect(SECTIONS.get(section, "/"))


# ---- Table settings: name, members, invites ----

def _table_page(request, conn, session, current: tables.Scope, message: str = "", error: str = "",
                invite_link: str = "", status_code: int = 200):
    return render(request, "table_settings.html", {
        "table": tables.get_table(conn, current.table_id), "members": tables.members(conn, current.table_id),
        "invites": tables.pending_invites(conn, current.table_id), "message": message, "error": error,
        "invite_link": invite_link, "nav_active": "", "me": session.user_id,
    }, session, status_code)


@router.get("/table")
def table_settings(request: Request, msg: str = "", session: auth.Session = Depends(auth.require_session),
                   conn: sqlite3.Connection = Depends(get_db)):
    current = _session_scope(request, conn, session)
    return _table_page(request, conn, session, current, TABLE_MESSAGES.get(msg, ""))


def _origin(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.post("/table/{action}")
async def table_action(request: Request, action: str, session: auth.Session = Depends(auth.require_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    """rename, invite, revoke, remove, leave. Every member may do each of these."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    current = tables.scope_for_post(request, conn, session, form)
    try:
        if action == "rename":
            tables.rename_table(conn, current, str(form.get("name") or ""))
            return redirect("/table?msg=renamed")
        if action == "invite":
            if auth.too_many(request, "invite", f"user:{session.user_id}"):
                raise tables.TableError("You've made a lot of invite links recently. Try again later.")
            token = tables.create_invite(conn, current, session.display_name)
            return _table_page(request, conn, session, current, TABLE_MESSAGES["invited"],
                               invite_link=f"{_origin(request)}/invite/{quote(token)}")
        if action == "revoke":
            invite_id = str(form.get("invite_id") or "")
            if invite_id.isdigit():
                tables.revoke_invite(conn, current, int(invite_id))
            return redirect("/table?msg=revoked")
        if action == "remove":
            member = str(form.get("member_id") or "")
            if form.get("confirm") != "yes" or not member.isdigit():
                raise tables.TableError("Confirm the removal first.")
            tables.remove_member(conn, current, int(member))
            return redirect("/table?msg=removed")
        if action == "leave":
            if form.get("confirm") != "yes":
                raise tables.TableError("Confirm that you want to leave first.")
            tables.leave_table(conn, current)
            remaining = tables.my_tables(conn, session.user_id)
            if remaining:
                tables.remember(conn, session, remaining[0]["id"])
                return redirect("/plan")
            return redirect("/welcome")
    except tables.TableError as exc:
        return _table_page(request, conn, session, current, error=str(exc), status_code=422)
    raise HTTPException(status_code=404, detail="We couldn't find that page.")


# ---- Invite links ----

@router.get("/invite/{token}")
def invite_page(request: Request, token: str, conn: sqlite3.Connection = Depends(get_db)):
    """'Kyle invited you to Payne's Table.' Works signed in or out; the link survives sign-up and sign-in."""
    session = auth.session_from_request(request, conn)
    if auth.too_many(request, "join", auth.client_key(request)):
        raise HTTPException(status_code=429, detail="Too many invite links opened from here. Wait a few minutes.")
    view = tables.view_invite(conn, token, session.user_id if session else None)
    if view.status in ("expired", "revoked", "used", "missing"):
        import logging
        logging.getLogger("family_table.access").info("invite not usable: %s", view.status)
    context = {"view": view, "token": token, "nav_active": ""}
    if session is None:
        return _public_page(request, "invite.html", context, 200 if view.status == "ok" else 410)
    request.state.session = session
    return render(request, "invite.html", context, session, 200 if view.status in ("ok", "member") else 410)


@router.post("/invite/{token}")
async def accept_invite(request: Request, token: str, session: auth.Session = Depends(auth.require_session),
                        conn: sqlite3.Connection = Depends(get_db)):
    await auth.read_form_with_csrf(request, session.csrf_token)
    try:
        view = tables.accept_invite(conn, token, session.user_id)
    except tables.TableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if view.status in ("joined", "member"):
        tables.remember(conn, session, view.table_id)
        return redirect("/?msg=joined" if view.status == "joined" else "/")
    return render(request, "invite.html", {"view": view, "token": token, "nav_active": ""}, session, 410)


# ---- Account: name, password, signed-in devices ----

def _account_page(request, conn, session, message: str = "", errors: dict | None = None, status_code: int = 200):
    current = auth.list_sessions(conn, session.user_id)
    for s in current:
        s["ref"] = s["token_hash"][:12]
        s["this"] = s["token_hash"] == session.token_hash
        del s["token_hash"]
    return render(request, "account.html", {
        "sessions": current, "message": message, "errors": errors or {}, "nav_active": "",
        "username": session.username, "display_name": session.display_name,
    }, session, status_code)


@router.get("/account")
def account(request: Request, msg: str = "", session: auth.Session = Depends(auth.require_session),
            conn: sqlite3.Connection = Depends(get_db)):
    return _account_page(request, conn, session, ACCOUNT_MESSAGES.get(msg, ""))


@router.post("/account/{action}")
async def account_action(request: Request, action: str, session: auth.Session = Depends(auth.require_session),
                         conn: sqlite3.Connection = Depends(get_db)):
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    if action == "name":
        name = " ".join(str(form.get("display_name") or "").split())[:60]
        if not name:
            return _account_page(request, conn, session, errors={"display_name": "Your name can't be empty."},
                                 status_code=422)
        with conn:
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (name, session.user_id))
        return redirect("/account?msg=name")
    if action == "password":
        current_pw = str(form.get("current_password") or "")[:auth.MAX_PASSWORD]
        new_pw = str(form.get("new_password") or "")[:auth.MAX_PASSWORD + 1]
        limiter: auth.RateLimiter = request.app.state.login_limiter
        key = f"user:{session.username.lower()}"
        if limiter.is_blocked(key):
            return _account_page(request, conn, session, errors={"password": "Too many tries. Wait 15 minutes."},
                                 status_code=429)
        user = await run_in_threadpool(auth.authenticate, conn, session.username, current_pw)
        if user is None:
            limiter.record_failure(key)
            return _account_page(request, conn, session, errors={"current_password": "That isn't your current password."},
                                 status_code=422)
        problem = auth.password_problem(new_pw, session.username, session.display_name)
        if problem:
            return _account_page(request, conn, session, errors={"new_password": problem}, status_code=422)
        accounts.change_password(conn, session.user_id, await run_in_threadpool(auth.hash_password, new_pw),
                                 session.token_hash)
        return redirect("/account?msg=password")
    if action == "end-session":
        auth.end_session(conn, session.user_id, str(form.get("session") or ""))
        return redirect("/account?msg=signed-out")
    if action == "end-others":
        auth.end_other_sessions(conn, session.user_id, session.token_hash)
        return redirect("/account?msg=signed-out-others")
    raise HTTPException(status_code=404, detail="We couldn't find that page.")


# ---- Diagnostics from the page (fixed codes only; never content) ----

DIAG_CODES = {"mic_denied", "mic_unavailable", "speech_unavailable", "speech_network", "tts_unavailable",
              "voice_connection_lost"}


@router.post("/diag")
async def client_diagnostic(request: Request, session: auth.Session = Depends(auth.require_session)):
    """A phone reports a voice problem so the maintainer can see it in the server log."""
    form = await auth.read_form_with_csrf(request, session.csrf_token)
    code = str(form.get("code") or "")
    if code in DIAG_CODES and not auth.too_many(request, "voice", f"diag:{session.user_id}"):
        import logging
        logging.getLogger("family_table.voice").warning("client reported %s (user %s, %s)", code, session.user_id,
                                                        auth.device_label(request.headers.get("user-agent", "")))
    from fastapi.responses import Response
    return Response(status_code=204)
