# io reads stored photos back with Pillow.
import io

# Pillow inspects processed images.
from PIL import Image

# Project modules and helpers.
from app.database import connect
from tests.conftest import create_recipe, csrf_from, make_image, recipe_form, recipe_id_from


def stored_photo(settings, recipe_id):
    conn = connect(settings.database_path)
    return conn.execute("SELECT photo_filename FROM recipes WHERE id = ?", (recipe_id,)).fetchone()[0]


def test_valid_photo_is_resized_and_converted(auth_client, settings):
    response = create_recipe(auth_client, photo=make_image("JPEG", (4000, 3000)))
    assert response.status_code == 303
    filename = stored_photo(settings, recipe_id_from(response))
    assert filename and filename.endswith(".webp")

    path = settings.upload_dir / filename
    with Image.open(path) as img:
        assert img.format == "WEBP"
        assert max(img.size) == 1800  # resized down
        assert img.size == (1800, 1350)  # aspect ratio kept

    served = auth_client.get(f"/photos/{filename}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/webp"
    Image.open(io.BytesIO(served.content)).verify()


def test_png_and_webp_are_accepted(auth_client, settings):
    for fmt in ("PNG", "WEBP"):
        response = create_recipe(auth_client, photo=make_image(fmt, (600, 400)), photo_name=f"x.{fmt.lower()}")
        assert response.status_code == 303, fmt


def test_exif_rotation_is_corrected(auth_client, settings):
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90° clockwise" as phones write it
    Image.new("RGB", (800, 400), (10, 120, 60)).save(buffer, "JPEG", exif=exif)
    filename = stored_photo(settings, recipe_id_from(create_recipe(auth_client, photo=buffer.getvalue())))
    with Image.open(settings.upload_dir / filename) as img:
        assert img.size == (400, 800)


def test_non_image_upload_is_rejected(auth_client, settings):
    response = create_recipe(auth_client, photo=b"#!/bin/sh\necho not a photo", photo_name="evil.jpg")
    assert response.status_code == 422
    assert "doesn&#39;t look like a valid photo" in response.text
    assert list(settings.upload_dir.glob("*.webp")) == []
    assert connect(settings.database_path).execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 0


def test_truncated_image_is_rejected(auth_client):
    corrupt = make_image("JPEG", (800, 600))[:400]
    response = create_recipe(auth_client, photo=corrupt)
    assert response.status_code == 422


def test_unsupported_format_is_rejected(auth_client):
    response = create_recipe(auth_client, photo=make_image("GIF", (100, 100)), photo_name="a.gif")
    assert response.status_code == 422
    assert "JPEG, PNG, or WebP" in response.text


def test_oversized_upload_is_rejected(auth_client, settings, monkeypatch):
    from dataclasses import replace
    auth_client.app.state.settings = replace(settings, max_upload_mb=0)  # everything is "too big"
    response = create_recipe(auth_client, photo=make_image("JPEG", (200, 200)))
    assert response.status_code == 422
    assert "Photos must be under" in response.text


def test_replacing_photo_removes_old_file(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client, photo=make_image()))
    old = stored_photo(settings, recipe_id)

    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}/edit").text)
    files = {"photo": ("new.png", make_image("PNG", (500, 500)), "image/png")}
    assert auth_client.post(f"/recipes/{recipe_id}/edit", data={**recipe_form(), "csrf_token": token},
                            files=files).status_code == 303
    new = stored_photo(settings, recipe_id)
    assert new != old
    assert (settings.upload_dir / new).exists()
    assert not (settings.upload_dir / old).exists()


def test_invalid_replacement_keeps_old_photo(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client, photo=make_image()))
    old = stored_photo(settings, recipe_id)
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}/edit").text)
    files = {"photo": ("bad.jpg", b"nope", "image/jpeg")}
    response = auth_client.post(f"/recipes/{recipe_id}/edit", data={**recipe_form(), "csrf_token": token}, files=files)
    assert response.status_code == 422
    assert stored_photo(settings, recipe_id) == old
    assert (settings.upload_dir / old).exists()


def test_edit_without_new_photo_keeps_existing(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client, photo=make_image()))
    old = stored_photo(settings, recipe_id)
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}/edit").text)
    auth_client.post(f"/recipes/{recipe_id}/edit", data={**recipe_form(name="Renamed"), "csrf_token": token})
    assert stored_photo(settings, recipe_id) == old


def test_remove_photo_option(auth_client, settings):
    recipe_id = recipe_id_from(create_recipe(auth_client, photo=make_image()))
    old = stored_photo(settings, recipe_id)
    token = csrf_from(auth_client.get(f"/recipes/{recipe_id}/edit").text)
    auth_client.post(f"/recipes/{recipe_id}/edit",
                     data={**recipe_form(), "remove_photo": "on", "csrf_token": token})
    assert stored_photo(settings, recipe_id) is None
    assert not (settings.upload_dir / old).exists()


def test_photos_require_login(auth_client, settings):
    filename = stored_photo(settings, recipe_id_from(create_recipe(auth_client, photo=make_image())))
    auth_client.cookies.clear()
    response = auth_client.get(f"/photos/{filename}")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_photo_route_blocks_path_traversal(auth_client, settings):
    (settings.data_dir / "secret.txt").write_text("private")
    for name in ["..%2Fsecret.txt", "../family_table.db", "family_table.db", "secret.txt"]:
        assert auth_client.get(f"/photos/{name}").status_code == 404, name


def test_data_folder_is_not_publicly_served(client):
    assert client.get("/data/family_table.db").status_code == 404
    assert client.get("/static/../data/family_table.db").status_code == 404


def test_featured_pick_prefers_recipes_with_photos(auth_client):
    create_recipe(auth_client, name="No Photo Stew")
    create_recipe(auth_client, name="Photo Pie", photo=make_image())
    page = auth_client.get("/").text
    featured = page.split('class="featured-card__title"')[1][:200]
    assert "Photo Pie" in featured
