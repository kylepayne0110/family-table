# io builds in-memory image files for upload tests.
import io

# re pulls the CSRF token out of rendered pages.
import re

# pytest provides fixtures.
import pytest

# TestClient drives the app like a browser would.
from fastapi.testclient import TestClient

# Pillow generates test photos.
from PIL import Image

# Project modules.
from app.auth import hash_password
from app.config import Settings
from app.main import create_app


USERNAME = "family"
PASSWORD = "correct horse battery"
PASSWORD_HASH = hash_password(PASSWORD)  # hashed once; scrypt is deliberately slow


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings pointing at a throwaway data folder (never the real one)."""
    return Settings(data_dir=tmp_path / "data", username=USERNAME, password_hash=PASSWORD_HASH)


@pytest.fixture
def client(settings):
    # "with" runs the app's startup, which creates the test database.
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


@pytest.fixture
def auth_client(client):
    response = login(client)
    assert response.status_code == 303
    return client


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "page has no CSRF token"
    return match.group(1)


def login(client, username=USERNAME, password=PASSWORD, remember=False):
    token = csrf_from(client.get("/login").text)
    data = {"username": username, "password": password, "csrf_token": token}
    if remember:
        data["remember"] = "on"
    return client.post("/login", data=data)


def make_image(fmt="JPEG", size=(2400, 1600), color=(180, 90, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


def recipe_form(**overrides) -> dict:
    """A valid recipe submission; override any field."""
    data = {
        "name": "Chicken Tacos",
        "description": "Crispy tacos with lime crema.",
        "category": "weeknight",
        "cuisine": "Tex-Mex",
        "prep_minutes": "15",
        "cook_minutes": "20",
        "servings": "4",
        "card_tag": "Taco Tuesday",
        "last_made": "",
        "ingredient_amount": ["1 lb", "8", ""],
        "ingredient_name": ["chicken thighs", "corn tortillas", ""],
        "step_title": ["Season", "", ""],
        "step_body": ["Season the chicken.", "Cook until crisp.", ""],
        "note_author": "",
        "note_body": "",
    }
    data.update(overrides)
    return data


def create_recipe(client, photo: bytes | None = None, photo_name="dinner.jpg", **overrides):
    token = csrf_from(client.get("/recipes/new").text)
    files = {"photo": (photo_name, photo, "image/jpeg")} if photo is not None else None
    return client.post("/recipes/new", data={**recipe_form(**overrides), "csrf_token": token}, files=files)


def recipe_id_from(response) -> int:
    match = re.match(r"/recipes/(\d+)", response.headers["location"])
    assert match, response.headers.get("location")
    return int(match.group(1))
