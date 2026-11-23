# Project helpers and fixtures.
from app.auth import SESSION_COOKIE, hash_password, verify_password
from tests.conftest import csrf_from, login


def test_app_starts_and_login_page_loads(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    assert "Come on in" in response.text
    assert "Forgot your password?" in response.text
    assert "Create an account" in response.text


def test_password_hash_is_not_plaintext():
    stored = hash_password("s3cret-pass")
    assert "s3cret-pass" not in stored
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("anything", "garbage")


def test_correct_credentials_log_in(client):
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    home = client.get("/")
    assert home.status_code == 200
    assert "tonight?" in home.text


def test_username_is_case_insensitive(client):
    assert login(client, username="FAMILY").status_code == 303


def test_wrong_credentials_rejected_with_generic_message(client):
    bad_password = login(client, password="nope")
    bad_username = login(client, username="someone")
    for response in (bad_password, bad_username):
        assert response.status_code == 401
        assert "username and password don&#39;t match" in response.text
    assert SESSION_COOKIE not in client.cookies


def test_login_requires_csrf_token(client):
    client.get("/login")
    response = client.post("/login", data={"username": "family", "password": "x", "csrf_token": "forged"})
    assert response.status_code == 400
    assert SESSION_COOKIE not in client.cookies


def test_repeated_failures_are_throttled(client):
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401
    # Even the right password is refused while blocked.
    response = login(client)
    assert response.status_code == 429
    assert "Too many tries" in response.text


def test_protected_routes_redirect_to_login(client):
    for path in ["/", "/recipes/new", "/recipes/1", "/recipes/1/edit"]:
        response = client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


def test_state_changing_routes_require_login(client):
    for path in ["/recipes/new", "/recipes/1/edit", "/recipes/1/notes", "/recipes/1/made",
                 "/recipes/1/delete", "/logout"]:
        response = client.post(path, data={"name": "x"})
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


def test_logout_invalidates_session(auth_client):
    old_cookie = auth_client.cookies.get(SESSION_COOKIE)
    token = csrf_from(auth_client.get("/").text)
    response = auth_client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    # Replaying the old cookie must not work: the session is gone server-side.
    auth_client.cookies.set(SESSION_COOKIE, old_cookie)
    assert auth_client.get("/").status_code == 303


def test_logout_requires_csrf(auth_client):
    response = auth_client.post("/logout", data={"csrf_token": "wrong"})
    assert response.status_code == 403
    assert auth_client.get("/").status_code == 200  # still signed in


def test_remember_me_sets_persistent_cookie(client):
    response = login(client, remember=True)
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("ft_session="))
    assert "Max-Age=2592000" in cookie


def test_without_remember_me_cookie_is_session_only(client):
    response = login(client)
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("ft_session="))
    assert "Max-Age=" not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_login_page_explains_missing_setup(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    unconfigured = Settings(data_dir=tmp_path / "d", username="", password_hash="")
    with TestClient(create_app(unconfigured)) as c:
        # Without the old shared login, people simply create their own accounts.
        assert 'href="/signup"' in c.get("/login").text
        assert login(c, username="", password="").status_code == 401


def test_security_headers_present(client):
    response = client.get("/login")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
