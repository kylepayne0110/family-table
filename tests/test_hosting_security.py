"""Checks that matter once Family Table is reachable from the internet (Tailscale Funnel)."""

# dataclasses.replace makes an HTTPS variant of the test settings.
from dataclasses import replace

# pytest provides parametrize.
import pytest

# APIRoute lets us list every route the app serves; TestClient drives the app.
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

# Project modules.
from app.auth import LoginLimiter
from app.main import create_app
from tests.conftest import create_recipe, login, make_image, recipe_id_from


# The only routes a stranger may use. Everything else must send them to sign in.
PUBLIC = {("GET", "/login"), ("POST", "/login"), ("GET", "/manifest.webmanifest"), ("GET", "/sw.js")}


def api_routes(routes):
    """Every APIRoute, including those inside included routers (FastAPI wraps them)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from api_routes(route.original_router.routes)


def all_routes(app):
    """(method, path with sample values) for every route the app defines."""
    samples = {"{recipe_id}": "1", "{note_id}": "1", "{filename}": "a" * 32 + ".webp", "{week}": "2026-11-09",
               "{day}": "0", "{item_id}": "1", "{action}": "status", "{staple_id}": "1"}
    for route in api_routes(app.routes):
        path = route.path
        for key, value in samples.items():
            path = path.replace(key, value)
        assert "{" not in path, f"add a sample value for {route.path}"
        for method in route.methods:
            yield method, path


def test_every_private_route_requires_sign_in(client):
    routes = list(all_routes(client.app))
    assert len(routes) > 30
    for method, path in routes:
        if (method, path) in PUBLIC:
            continue
        response = client.request(method, path, data={"csrf_token": "x"})
        assert response.status_code == 303 and response.headers["location"] == "/login", (method, path)


def test_public_routes_reveal_no_family_data(auth_client, settings):
    create_recipe(auth_client)   # "Chicken Tacos"
    auth_client.cookies.clear()
    for method, path in PUBLIC:
        if method == "GET":
            assert "Chicken Tacos" not in auth_client.get(path).text, path


def test_photos_need_sign_in_and_reject_odd_names(auth_client, client):
    rid = recipe_id_from(create_recipe(auth_client, photo=make_image()))
    page = auth_client.get(f"/recipes/{rid}").text
    photo = page.split('src="/photos/')[1].split('"')[0]
    assert auth_client.get(f"/photos/{photo}").status_code == 200
    for bad in ("../.env", "..%2F..%2F.env", "%2e%2e/family_table.db", "family_table.db", ".env"):
        assert auth_client.get(f"/photos/{bad}").status_code == 404, bad
    auth_client.cookies.clear()
    assert client.get(f"/photos/{photo}").headers["location"] == "/login"


@pytest.mark.parametrize("path", [
    "/.env", "/data/family_table.db", "/family_table.db", "/static/../.env", "/static/..%2F.env",
    "/static/%2e%2e/app/config.py", "/uploads/", "/data/uploads/", "/docs", "/redoc", "/openapi.json",
    "/.git/config", "/app/main.py",
])
def test_private_files_and_debug_pages_are_not_served(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 404, path
    assert "FAMILY_PASSWORD_HASH" not in response.text and "SQLite" not in response.text


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "TRACE"])
def test_unexpected_methods_cannot_change_data(auth_client, method):
    rid = recipe_id_from(create_recipe(auth_client))
    for path in (f"/recipes/{rid}", f"/recipes/{rid}/delete", "/shopping/2026-11-09/items"):
        assert auth_client.request(method, path).status_code in (404, 405), (method, path)
    assert auth_client.get(f"/recipes/{rid}").status_code == 200   # still there


def test_server_errors_show_no_internals(settings, monkeypatch):
    import app.routes as routes   # break one page on purpose

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail /home/kylep")

    monkeypatch.setattr(routes.meals, "list_recipes", boom)
    with TestClient(create_app(settings), raise_server_exceptions=False, follow_redirects=False) as c:
        login(c)
        response = c.get("/")
    assert response.status_code == 500
    assert "secret internal detail" not in response.text and "Traceback" not in response.text


# ---- Oversized uploads ----

def test_huge_declared_body_is_refused_before_reading(client):
    response = client.post("/login", content=b"x" * 10, headers={"content-length": str(50 * 1024 * 1024),
                                                               "content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


def test_huge_streamed_body_is_cut_off(client):
    def chunks():   # no content-length: sent in pieces
        for _ in range(20):
            yield b"x" * (1024 * 1024)
    response = client.post("/login", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


def test_normal_photo_upload_still_fits(auth_client):
    response = create_recipe(auth_client, photo=make_image(size=(4000, 3000)))
    assert response.status_code == 303


# ---- Login throttling ----

def test_login_limit_per_address():
    limiter = LoginLimiter(max_failures=3, site_max_failures=100)
    for _ in range(3):
        limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4") and not limiter.is_blocked("5.6.7.8")


def test_site_wide_limit_stops_guessers_rotating_addresses():
    limiter = LoginLimiter(max_failures=5, site_max_failures=10)
    for i in range(10):
        limiter.record_failure(f"10.0.0.{i}")   # each address stays under its own limit
    assert limiter.is_blocked("203.0.113.9")


def test_limiter_forgets_old_addresses():
    limiter = LoginLimiter(window_seconds=0)
    limiter.record_failure("1.2.3.4")
    assert not limiter.is_blocked("1.2.3.4")
    assert limiter._failures == {}


def test_spoofed_forwarded_for_does_not_dodge_the_limit(client):
    """The app never reads X-Forwarded-For itself; only uvicorn trusts it, and only from 127.0.0.1."""
    for i in range(5):
        login(client, password="wrong")   # same real address each time
    response = client.post("/login", headers={"x-forwarded-for": "198.51.100.7"},
                           data={"username": "family", "password": "wrong", "csrf_token": "x"})
    assert response.status_code in (400, 429)
    assert "Too many tries" in login(client).text


# ---- HTTPS settings ----

def test_https_mode_sends_hsts_and_secure_cookies(settings):
    https = replace(settings, cookie_secure=True)
    with TestClient(create_app(https), base_url="https://testserver", follow_redirects=False) as c:
        response = c.get("/login")
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert "Secure" in response.headers["set-cookie"]
        response = login(c, remember=True)
        assert all(flag in response.headers["set-cookie"] for flag in ("Secure", "HttpOnly", "SameSite=lax"))
        # Every cookie the server sets or clears is HTTPS-only, including the removals.
        for header in response.headers.get_list("set-cookie"):
            assert "Secure" in header, header.split("=")[0]
        csrf = c.get("/").text.split('name="csrf_token" value="')[1].split('"')[0]
        for header in c.post("/logout", data={"csrf_token": csrf}).headers.get_list("set-cookie"):
            assert "Secure" in header and "HttpOnly" in header, header.split("=")[0]


def test_development_mode_has_no_hsts(client):
    assert "strict-transport-security" not in client.get("/login").headers


# ---- Recipe importer can't reach this computer or the tailnet ----

@pytest.mark.parametrize("ip", [
    "100.100.100.100",        # Tailscale's built-in DNS / local service address
    "100.101.102.103",        # any tailnet device (100.64.0.0/10)
    "fd7a:115c:a1e0::1",      # tailnet IPv6
    "192.168.48.1",           # the Windows side of the WSL network
    "192.168.1.147",          # a home Wi-Fi address
    "127.0.0.1", "::1", "0.0.0.0",
    "2002:7f00:1::1",         # 6to4 wrapping 127.0.0.1
    "64:ff9b::a00:1",         # NAT64 wrapping 10.0.0.1
    "64:ff9b::7f00:1",        # NAT64 wrapping 127.0.0.1
])
def test_importer_refuses_local_and_tailnet_addresses(ip):
    from app.safe_fetch import is_public_ip
    assert not is_public_ip(ip)


def test_importer_still_allows_public_sites():
    from app.safe_fetch import is_public_ip
    assert is_public_ip("93.184.215.14") and is_public_ip("2606:4700::6810:84e5")
    assert is_public_ip("64:ff9b::5db8:d70e")   # NAT64 wrapping a public address


def test_importer_refuses_a_name_that_resolves_to_the_tailnet(monkeypatch):
    from app import safe_fetch
    monkeypatch.setattr(safe_fetch, "resolve_host", lambda host, port: ["100.100.100.100"])
    with pytest.raises(safe_fetch.FetchError) as err:
        safe_fetch.check_url("https://looks-public.example/recipe")
    assert err.value.reason == "blocked"
