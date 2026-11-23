# Accounts, passwords, sessions, CSRF, and rate limits.

# base64 encodes salts and hashes as text.
import base64

# hashlib provides scrypt (password hashing) and SHA-256.
import hashlib

# hmac.compare_digest compares secrets in constant time.
import hmac

# re checks usernames.
import re

# secrets generates unguessable tokens.
import secrets

# sqlite3 types the database connection.
import sqlite3

# threading guards the in-memory limiters (requests run on several threads).
import threading

# time supplies timestamps for the limiters.
import time

# dataclass holds the current session's data.
from dataclasses import dataclass

# datetime calculates session expiry times.
from datetime import UTC, datetime, timedelta

# FastAPI pieces for dependencies and errors.
from fastapi import Depends, HTTPException, Request, Response

# get_db supplies the per-request database connection.
from app.database import get_db


# Plain name for http://localhost development; "__Host-" over HTTPS, so the cookie can only
# be set by this exact site, over HTTPS, for the whole path (browsers enforce the prefix).
SESSION_COOKIE = "ft_session"
SECURE_SESSION_COOKIE = "__Host-ft_session"
LOGIN_CSRF_COOKIE = "ft_login_csrf"

# scrypt cost settings (about 16 MB of memory per check).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
MIN_PASSWORD = 10
MAX_PASSWORD = 200
# Sign-ins record "last seen" at most this often (keeps reads from turning into writes).
LAST_SEEN_EVERY = timedelta(minutes=5)


class LoginRequired(Exception):
    """Raised when a protected page is visited without a session."""


@dataclass
class Session:
    token_hash: str
    csrf_token: str
    user_id: int = 0
    username: str = ""
    display_name: str = ""
    active_table_id: int | None = None


# ---- Passwords ----

def hash_password(password: str) -> str:
    """Return a salted scrypt hash string."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode()

    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{b64(salt)}:{b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored scrypt hash."""
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split(":")
        if algo != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(hash_b64)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked when the username doesn't exist, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def password_problem(password: str, username: str = "", display_name: str = "") -> str | None:
    """A message if a new password is too weak, else None."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters for your password."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    lowered = password.lower()
    for word in (username, display_name, "family table", "password"):
        if word and len(word) >= 3 and word.lower() in lowered and len(password) < 16:
            return "Pick a password that doesn't contain your name or the app's name."
    if len(set(password)) < 4:
        return "That password is too easy to guess."
    return None


def username_problem(username: str) -> str | None:
    if not USERNAME_PATTERN.fullmatch(username):
        return "Usernames are 3 to 40 letters, numbers, dots, dashes, or underscores."
    return None


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    """The account for these credentials, or None. Takes the same time whether or not the username exists."""
    row = conn.execute("SELECT id, username, display_name, password_hash, last_table_id FROM users "
                       "WHERE username = ?", (username.strip()[:40],)).fetchone()
    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    return row if row and ok else None


# ---- Rate limits ----

class RateLimiter:
    """In-memory sliding-window limit, per key and for the whole site.

    The per-key limit stops one guesser (key = client address, account, or both). The
    site-wide limit is a backstop for when addresses can't be trusted (a public tunnel where
    every request seems to come from 127.0.0.1, or a guesser rotating addresses).
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 15 * 60, site_max_failures: int = 30):
        self.max_failures = max_failures
        self.site_max_failures = site_max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._site: list[float] = []
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)   # don't keep growing with one-off addresses
        return recent

    def _site_recent(self) -> list[float]:
        cutoff = time.monotonic() - self.window
        self._site = [t for t in self._site if t > cutoff]
        return self._site

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.max_failures or len(self._site_recent()) >= self.site_max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._failures[key] = self._recent(key) + [now]
            self._site_recent().append(now)
            if len(self._failures) > 10_000:   # memory guard: forget the oldest keys
                for old in sorted(self._failures, key=lambda k: self._failures[k][-1])[:5_000]:
                    del self._failures[old]

    def hit(self, key: str) -> bool:
        """Count one use. False (and not counted) when the key or site is over its limit."""
        with self._lock:
            if len(self._recent(key)) >= self.max_failures or len(self._site_recent()) >= self.site_max_failures:
                return False
        self.record_failure(key)
        return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# The original name, kept for the sign-in limiter.
LoginLimiter = RateLimiter


def default_limiters() -> dict[str, RateLimiter]:
    """Limits for actions that are slow, public, or easy to abuse. Per account unless noted."""
    return {
        "login": RateLimiter(5, 15 * 60, 30),            # failed sign-ins per address / username
        "signup": RateLimiter(5, 60 * 60, 40),           # new accounts per address, and site-wide per hour
        "reset": RateLimiter(5, 15 * 60, 30),            # password-reset attempts
        "invite": RateLimiter(20, 60 * 60, 400),         # invite links made
        "join": RateLimiter(10, 15 * 60, 200),           # invite pages opened (guessing links)
        "import": RateLimiter(20, 60 * 60, 400),         # website imports
        "upload": RateLimiter(60, 60 * 60, 1000),        # photo uploads
        "search": RateLimiter(120, 60, 2000),            # community searches per minute
        "publish": RateLimiter(30, 60 * 60, 500),        # community publishes, updates, reports
        "model": RateLimiter(40, 5 * 60, 600),           # assistant / voice turns that may reach the model
        "voice": RateLimiter(120, 5 * 60, 3000),         # spoken turns (most are answered without the model)
    }


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def too_many(request: Request, name: str, key: str) -> bool:
    """True (and the attempt is refused) when this key has used `name` too often."""
    limiter: RateLimiter = request.app.state.limiters[name]
    return not limiter.hit(key)


# ---- Sessions ----

def _utc(dt: datetime) -> str:
    """Format a time the same way SQLite's datetime('now') does."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cookie_name(settings) -> str:
    return SECURE_SESSION_COOKIE if settings.cookie_secure else SESSION_COOKIE


def device_label(user_agent: str) -> str:
    """A rough, privacy-friendly label for the sessions list ("iPhone · Safari")."""
    ua = user_agent or ""
    device = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua
              else "Mac" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua
              else "Browser")
    browser = ("Edge" if "Edg/" in ua else "Firefox" if "Firefox/" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua)
               else "Safari" if "Safari/" in ua else "")
    return f"{device} · {browser}" if browser else device


def create_session(conn: sqlite3.Connection, user_id: int, lifetime: timedelta, device: str = "",
                   active_table_id: int | None = None) -> str:
    """Store a new session and return the raw cookie token (only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + lifetime
    with conn:
        # Clear out expired sessions while we're here.
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        conn.execute(
            """INSERT INTO sessions (token_hash, user_id, csrf_token, active_table_id, device, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (hash_token(token), user_id, secrets.token_urlsafe(32), active_table_id, device[:60], _utc(expires)),
        )
    return token


def load_session(conn: sqlite3.Connection, token: str | None) -> Session | None:
    """Return the live session for a cookie token, if any."""
    if not token:
        return None
    row = conn.execute(
        """SELECT s.token_hash, s.csrf_token, s.user_id, s.active_table_id, s.last_seen_at,
                  u.username, u.display_name
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > datetime('now')""",
        (hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    seen = datetime.fromisoformat(row["last_seen_at"]).replace(tzinfo=UTC)
    if datetime.now(UTC) - seen > LAST_SEEN_EVERY:
        try:
            with conn:
                conn.execute("UPDATE sessions SET last_seen_at = datetime('now') WHERE token_hash = ?",
                             (row["token_hash"],))
        except sqlite3.OperationalError:
            pass   # busy: "last seen" can wait for the next request
    return Session(row["token_hash"], row["csrf_token"], row["user_id"], row["username"], row["display_name"],
                   row["active_table_id"])


def delete_session(conn: sqlite3.Connection, session: Session) -> None:
    """Invalidate a session on the server."""
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))


def list_sessions(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT token_hash, device, created_at, last_seen_at, expires_at FROM sessions
           WHERE user_id = ? AND expires_at > datetime('now') ORDER BY last_seen_at DESC""", (user_id,))]


def end_session(conn: sqlite3.Connection, user_id: int, session_ref: str) -> bool:
    """Sign out one of your own sessions, named by a short prefix of its hash."""
    if not re.fullmatch(r"[0-9a-f]{12}", session_ref or ""):
        return False
    with conn:
        return conn.execute("DELETE FROM sessions WHERE user_id = ? AND substr(token_hash, 1, 12) = ?",
                            (user_id, session_ref)).rowcount > 0


def end_other_sessions(conn: sqlite3.Connection, user_id: int, keep_token_hash: str | None) -> int:
    with conn:
        return conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                            (user_id, keep_token_hash or "")).rowcount


def set_session_cookie(response: Response, token: str, settings, remember: bool) -> None:
    """Attach the session cookie; persistent only when 'keep me signed in'."""
    max_age = settings.remember_days * 86400 if remember else None
    response.set_cookie(
        cookie_name(settings), token, max_age=max_age, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )


def clear_session_cookie(response: Response, settings) -> None:
    # Deleted with the same flags it was set with, so browsers accept the removal over HTTPS.
    response.delete_cookie(cookie_name(settings), path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")


def session_from_request(request: Request, conn: sqlite3.Connection) -> Session | None:
    return load_session(conn, request.cookies.get(cookie_name(request.app.state.settings)))


def require_session(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Session:
    """Dependency for protected routes: sends visitors to the login page."""
    session = session_from_request(request, conn)
    if session is None:
        raise LoginRequired()
    request.state.session = session
    return session


# ---- CSRF ----

def csrf_ok(submitted, expected: str) -> bool:
    return isinstance(submitted, str) and bool(expected) and hmac.compare_digest(submitted, expected)


async def read_form_with_csrf(request: Request, expected: str):
    """Parse a submitted form and reject it if the CSRF token is wrong."""
    form = await request.form()
    submitted = form.get("csrf_token") or request.headers.get("x-csrf-token")
    if not csrf_ok(submitted, expected):
        raise HTTPException(status_code=403, detail="This form expired. Please go back, refresh, and try again.")
    return form


# ---- One-time tokens (invites, password resets) ----

def new_token() -> tuple[str, str]:
    """(token for the link, hash to store)."""
    token = secrets.token_urlsafe(24)
    return token, hash_token(token)


def utc_in(delta: timedelta) -> str:
    return _utc(datetime.now(UTC) + delta)
