# hashlib fingerprints static files so browsers fetch new copies after an update.
import hashlib

# lru_cache computes each fingerprint once per server start.
from functools import lru_cache

# APIRouter groups the install-related routes; Response sends raw JavaScript.
from fastapi import APIRouter, Response

# JSONResponse sends the manifest with its own media type.
from fastapi.responses import JSONResponse

# Project root, for reading static files.
from app.config import BASE_DIR


STATIC_DIR = BASE_DIR / "static"
router = APIRouter()

THEME = "#F4EDE1"   # warm page background: the iPhone status bar and splash screen colour

# Everything the service worker keeps for the "can't reach Family Table" screen.
# Only public, non-personal files: never recipes, lists, or signed-in pages.
OFFLINE_FILES = ["offline.html", "css/styles.css", "favicon.svg", "icons/icon-192.png"]


@lru_cache(maxsize=None)
def file_version(path: str) -> str:
    """Short content hash of a static file (cached until the server restarts)."""
    try:
        return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def static_url(path: str) -> str:
    """Root-relative URL with a version, e.g. /static/css/styles.css?v=1a2b3c4d5e.

    Root-relative so pages work unchanged behind an HTTPS proxy, and versioned so an
    installed iPhone app never keeps using yesterday's CSS or JavaScript.
    """
    path = path.lstrip("/")
    return f"/static/{path}?v={file_version(path)}"


MANIFEST = {
    "id": "/",
    "name": "Family Table",
    "short_name": "Family Table",
    "description": "Our family recipes, weekly dinner plan, and shared shopping list.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "theme_color": THEME,
    "background_color": THEME,
    "lang": "en",
    "categories": ["food", "lifestyle"],
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
    "shortcuts": [
        {"name": "Shopping List", "url": "/shopping", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
        {"name": "Meal Planner", "url": "/plan", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
    ],
}


# The manifest and service worker are public: phones fetch them without the session cookie.
@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@lru_cache(maxsize=None)
def _service_worker_source() -> str:
    """static/sw.js with its file list and a version that changes whenever those files do."""
    urls = [static_url(f) for f in OFFLINE_FILES]
    version = hashlib.sha256(" ".join(urls).encode()).hexdigest()[:10]
    source = (STATIC_DIR / "sw.js").read_text()
    return source.replace('"__VERSION__"', f'"{version}"').replace('["__OFFLINE_FILES__"]', repr(urls).replace("'", '"'))


# Served from the site root (not /static/) so the worker can cover every page.
@router.get("/sw.js", include_in_schema=False)
def service_worker():
    return Response(_service_worker_source(), media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})
