# base64 encodes salts and hashes as text.
import base64

# hashlib provides scrypt (password hashing) and SHA-256.
import hashlib

# hmac.compare_digest compares secrets in constant time.
import hmac

# secrets generates unguessable tokens.
import secrets

# sqlite3 types the database connection.
import sqlite3

# time supplies timestamps for the login limiter.
import time

# dataclass holds the current session's data.
from dataclasses import dataclass

# datetime calculates session expiry times.
from datetime import UTC, datetime, timedelta

# FastAPI pieces for dependencies and errors.
from fastapi import Depends, HTTPException, Request, Response

# get_db supplies the per-request database connection.
from app.database import get_db


SESSION_COOKIE = "ft_session"
LOGIN_CSRF_COOKIE = "ft_login_csrf"

# scrypt cost settings (about 16 MB of memory per check).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class LoginRequired(Exception):
    """Raised when a protected page is visited without a session."""


@dataclass
class Session:
    token_hash: str
    csrf_token: str


# ---- Passwords ----

def hash_password(password: str) -> str:
    """Return a salted scrypt hash string safe to store in .env."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    b64 = lambda raw: base64.urlsafe_b64encode(raw).decode()
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


def check_credentials(settings, username: str, password: str) -> bool:
    """True only if both username and password match."""
    if not settings.login_configured:
        return False
    # Always run the hash so timing doesn't reveal a wrong username.
    password_ok = verify_password(password, settings.password_hash)
    username_ok = hmac.compare_digest(username.strip().lower(), settings.username.lower())
    return username_ok and password_ok


# ---- Login throttling ----

class LoginLimiter:
    """In-memory limit on failed logins, per client address and for the whole site.

    The per-address limit stops one guesser. The site-wide limit is a backstop for when
    addresses can't be trusted (for example a public tunnel where every request seems to
    come from 127.0.0.1, or a guesser rotating addresses): after that many failures from
    anywhere, all logins pause until the window passes. Signed-in phones are unaffected.
    """

    def __init__(self, max_failures: int = 5, window_seconds: int = 15 * 60, site_max_failures: int = 30):
        self.max_failures = max_failures
        self.site_max_failures = site_max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._site: list[float] = []

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
        return len(self._recent(key)) >= self.max_failures or len(self._site_recent()) >= self.site_max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._failures[key] = self._recent(key) + [now]
        self._site_recent().append(now)
        if len(self._failures) > 10_000:   # memory guard: forget the oldest addresses
            for old in sorted(self._failures, key=lambda k: self._failures[k][-1])[:5_000]:
                del self._failures[old]

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


# ---- Sessions ----

def _utc(dt: datetime) -> str:
    """Format a time the same way SQLite's datetime('now') does."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, lifetime: timedelta) -> str:
    """Store a new session and return the raw cookie token."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + lifetime
    with conn:
        # Clear out expired sessions while we're here.
        conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")
        conn.execute(
            "INSERT INTO sessions (token_hash, csrf_token, expires_at) VALUES (?, ?, ?)",
            (_hash_token(token), secrets.token_urlsafe(32), _utc(expires)),
        )
    return token


def load_session(conn: sqlite3.Connection, token: str | None) -> Session | None:
    """Return the live session for a cookie token, if any."""
    if not token:
        return None
    row = conn.execute(
        "SELECT token_hash, csrf_token FROM sessions WHERE token_hash = ? AND expires_at > datetime('now')",
        (_hash_token(token),),
    ).fetchone()
    return Session(row["token_hash"], row["csrf_token"]) if row else None


def delete_session(conn: sqlite3.Connection, session: Session) -> None:
    """Invalidate a session on the server."""
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session.token_hash,))


def set_session_cookie(response: Response, token: str, settings, remember: bool) -> None:
    """Attach the session cookie; persistent only when 'keep me signed in'."""
    max_age = settings.remember_days * 86400 if remember else None
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )


def require_session(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Session:
    """Dependency for protected routes: sends visitors to the login page."""
    session = load_session(conn, request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise LoginRequired()
    return session


# ---- CSRF ----

async def read_form_with_csrf(request: Request, expected: str):
    """Parse a submitted form and reject it if the CSRF token is wrong."""
    form = await request.form()
    submitted = form.get("csrf_token")
    if not isinstance(submitted, str) or not expected or not hmac.compare_digest(submitted, expected):
        raise HTTPException(status_code=403, detail="This form expired. Please go back, refresh, and try again.")
    return form
