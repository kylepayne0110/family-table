# hashlib recomputes file versions; io opens icon bytes; json reads the manifest; re finds links.
import hashlib
import io
import json
import re

# dataclasses.replace makes an HTTPS variant of the test settings.
from dataclasses import replace

# pytest provides parametrize.
import pytest

# TestClient runs the app; Pillow checks icon images.
from fastapi.testclient import TestClient
from PIL import Image

# Project modules.
from app.main import create_app
from app.pwa import OFFLINE_FILES, STATIC_DIR, file_version
from tests.conftest import create_recipe, login, recipe_id_from


def manifest(client) -> dict:
    return json.loads(client.get("/manifest.webmanifest").content)


# ---- Manifest ----

def test_manifest_is_public_and_valid(client):
    response = client.get("/manifest.webmanifest")   # signed out: phones fetch it without cookies
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(response.content)
    assert data["name"] == "Family Table" and data["short_name"] == "Family Table"
    assert data["description"]
    assert data["start_url"] == "/" and data["scope"] == "/" and data["id"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"] == data["background_color"] == "#F4EDE1"


def test_manifest_icons_cover_standard_and_maskable_sizes(client):
    icons = manifest(client)["icons"]
    found = {(i["sizes"], i["purpose"]) for i in icons}
    assert {("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"), ("512x512", "maskable")} <= found
    assert all(i["type"] == "image/png" for i in icons)


def test_manifest_shortcuts_stay_inside_the_app(client):
    data = manifest(client)
    for shortcut in data["shortcuts"]:
        assert shortcut["url"].startswith(data["scope"])
        assert client.get(shortcut["url"]).headers["location"] == "/login"   # still needs sign-in


@pytest.mark.parametrize("icon", [
    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "purpose": "any"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "purpose": "any"},
    {"src": "/static/icons/icon-maskable-192.png", "sizes": "192x192", "purpose": "maskable"},
    {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "purpose": "maskable"},
])
def test_every_manifest_icon_is_a_real_png_of_the_stated_size(client, icon):
    assert icon in [{k: i[k] for k in ("src", "sizes", "purpose")} for i in manifest(client)["icons"]]
    response = client.get(icon["src"])
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    image = Image.open(io.BytesIO(response.content))
    assert image.format == "PNG"
    assert "x".join(map(str, image.size)) == icon["sizes"]
    if icon["purpose"] == "maskable":
        assert image.mode == "RGB"   # full bleed: no transparent corners for Android to fill


def test_apple_touch_icon_is_opaque_180px_png(client):
    response = client.get("/static/icons/apple-touch-icon.png")
    assert response.status_code == 200
    image = Image.open(io.BytesIO(response.content))
    # iOS rounds the corners itself and shows transparency as black, so it must be opaque.
    assert image.format == "PNG" and image.size == (180, 180) and image.mode == "RGB"
    assert image.getpixel((0, 0)) == (0xA8, 0x43, 0x2A)   # terracotta to the edge


def test_favicon_is_svg(client):
    response = client.get("/static/favicon.svg")
    assert response.status_code == 200 and response.text.startswith("<svg")


# ---- iPhone metadata on every page ----

APPLE_TAGS = [
    '<link rel="manifest" href="/manifest.webmanifest">',
    '<meta name="apple-mobile-web-app-capable" content="yes">',
    '<meta name="mobile-web-app-capable" content="yes">',
    '<meta name="apple-mobile-web-app-title" content="Family Table">',
    '<meta name="apple-mobile-web-app-status-bar-style" content="default">',
    '<meta name="theme-color" content="#F4EDE1">',
    'content="width=device-width, initial-scale=1.0, viewport-fit=cover"',
    '<meta name="format-detection" content="telephone=no',
]


@pytest.mark.parametrize("url", ["/", "/plan", "/shopping", "/staples", "/recipes/new", "/nope"])
def test_signed_in_pages_have_install_metadata(auth_client, url):
    page = auth_client.get(url).text
    for tag in APPLE_TAGS:
        assert tag in page, tag
    assert re.search(r'<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon\.png\?v=\w+">', page)


def test_login_page_has_install_metadata(client):
    # Safari's Add to Home Screen may be used on the sign-in page, so it needs the tags too.
    page = client.get("/login").text
    for tag in APPLE_TAGS:
        assert tag in page, tag


def test_static_links_are_versioned_and_root_relative(auth_client):
    page = auth_client.get("/shopping").text
    links = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)
    assert any("css/styles.css" in u for u in links) and any("js/shopping.js" in u for u in links)
    for url in links:
        path, _, version = url.partition("?v=")
        assert version == file_version(path.removeprefix("/static/")), url
        assert auth_client.get(url).status_code == 200, url
    # Absolute http:// links would break as mixed content behind an HTTPS proxy.
    assert "http://testserver" not in page


def test_file_version_changes_with_content():
    css = (STATIC_DIR / "css" / "styles.css").read_bytes()
    assert file_version("css/styles.css") == hashlib.sha256(css).hexdigest()[:10]
    assert file_version("does/not/exist.css") == "0"


# ---- Service worker ----

def test_service_worker_is_served_from_the_root(client):
    response = client.get("/sw.js")   # public: the browser fetches it without cookies
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"   # updates are picked up promptly
    assert "__VERSION__" not in response.text and "__OFFLINE_FILES__" not in response.text


def sw_offline_files(client) -> list[str]:
    match = re.search(r"const OFFLINE_FILES = (\[.*?\]);", client.get("/sw.js").text)
    return json.loads(match.group(1))


def test_service_worker_caches_only_public_static_files(client):
    files = sw_offline_files(client)
    assert len(files) == len(OFFLINE_FILES) and files[0].startswith("/static/offline.html?v=")
    for url in files:
        assert url.startswith("/static/"), url   # never recipes, plans, lists, or photos
        assert client.get(url).status_code == 200, url   # available while signed out


def test_service_worker_never_caches_pages_or_saves(client):
    source = client.get("/sw.js").text
    assert 'if (req.method !== "GET") return;' in source          # posts always hit the server
    assert "cache.put" not in source                              # nothing is stored at runtime
    assert "fetch(req).catch(() => caches.match(OFFLINE_PAGE))" in source   # pages: network first


def test_service_worker_version_tracks_its_files(client):
    source = client.get("/sw.js").text
    version = re.search(r'const VERSION = "(\w+)"', source).group(1)
    assert len(version) == 10
    assert client.get("/sw.js").text == source   # stable between requests


def test_offline_page_contains_no_family_data(auth_client, settings):
    create_recipe(auth_client)   # "Chicken Tacos"
    page = auth_client.get(sw_offline_files(auth_client)[0]).text
    assert "Can't reach Family Table" in page and 'href=""' in page   # Try again reloads the same page
    assert "Chicken Tacos" not in page and "{{" not in page and "<script" not in page


def test_pwa_files_get_security_headers(client):
    for url in ("/manifest.webmanifest", "/sw.js"):
        headers = client.get(url).headers
        assert "default-src 'self'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"


# ---- Sign-in still protects the family's data ----

def test_private_pages_stay_uncached_and_signed_in_only(client, auth_client):
    for url in ("/", "/plan", "/shopping", "/staples"):
        response = auth_client.get(url)
        assert response.headers["cache-control"] == "no-store"
    auth_client.cookies.clear()
    assert client.get("/shopping").headers["location"] == "/login"


def test_remembered_session_cookie_is_persistent_and_http_only(client):
    response = login(client, remember=True)
    cookie = response.headers["set-cookie"]
    assert "ft_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=2592000" in cookie   # 30 days: the installed app stays signed in
    assert "Secure" not in cookie        # plain http during development


def test_cookies_are_secure_when_hosted_over_https(settings):
    https = replace(settings, cookie_secure=True)
    with TestClient(create_app(https), base_url="https://testserver", follow_redirects=False) as c:
        assert "Secure" in c.get("/login").headers["set-cookie"]
        response = login(c, remember=True)
        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]
        rid = recipe_id_from(create_recipe(c))
        assert c.get(f"/recipes/{rid}").status_code == 200   # the session works over HTTPS


def test_error_page_offers_back_button(auth_client):
    page = auth_client.get("/recipes/99999").text
    assert "data-history-back hidden" in page and 'href="/"' in page
