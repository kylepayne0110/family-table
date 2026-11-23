"""Case A: upgrade an existing single-family installation, then use it in a real browser.

The server starts on a database in the old layout (one shared login, no accounts), with
photos on disk. Kyle signs in with the same username and password and sees everything;
Nat follows an invite, creates her own login, and lands in the same Table.
"""

import sqlite3

import pytest

from tests.browser.conftest import start_server
from tests.conftest import make_image

PHOTO = "e" * 32 + ".webp"


@pytest.fixture(scope="module")
def server(tmp_path_factory, fake_ollama):
    from app.migrations import LEGACY_ADDED_COLUMNS, LEGACY_SCHEMA
    data = tmp_path_factory.mktemp("legacy-data")
    (data / "uploads").mkdir()
    (data / "uploads" / PHOTO).write_bytes(make_image("WEBP", (40, 30)))
    db = sqlite3.connect(data / "family_table.db")
    db.executescript(LEGACY_SCHEMA)
    for table, column, definition in LEGACY_ADDED_COLUMNS:
        if column not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    monday = today - timedelta(days=today.weekday())
    last_week = (monday - timedelta(days=7)).isoformat()
    db.executescript(f"""
        INSERT INTO recipes (id, name, category, servings, photo_filename) VALUES (4, 'Old Family Lasagna', 'sunday-dinner', 8, '{PHOTO}');
        INSERT INTO ingredients (recipe_id, position, amount, name) VALUES (4, 0, '1 lb', 'lasagna noodles');
        INSERT INTO steps (recipe_id, position, body) VALUES (4, 0, 'Layer and bake.');
        INSERT INTO notes (recipe_id, author, body) VALUES (4, 'Mom', 'Extra ricotta.');
        INSERT INTO week_plans (week_start) VALUES ('{monday.isoformat()}'), ('{last_week}');
        INSERT INTO planned_meals (week_start, day, recipe_id, servings) VALUES ('{monday.isoformat()}', 6, 4, 10);
        INSERT INTO planned_meals (week_start, day, recipe_id) VALUES ('{last_week}', 2, 4);
        INSERT INTO shopping_items (week_start, item_key, name, quantity, status) VALUES ('{monday.isoformat()}', 'ricotta', 'Ricotta', '2 cups', 'need');
        INSERT INTO app_meta (key, value) VALUES ('staples_seeded', 'yes');
    """)
    db.commit()
    db.close()
    yield from start_server(data, fake_ollama, {"FAMILY_TABLE_NAME": "", "FAMILY_DISPLAY_NAME": "Kyle"})


def test_upgraded_install_signs_in_and_nat_joins(new_phone, server):
    from tests.browser.conftest import OWNER, OWNER_PASSWORD, sign_in, sign_up
    kyle = new_phone()
    sign_in(kyle, OWNER, OWNER_PASSWORD)                       # same username and password as before
    assert "Kyle's Table" in kyle.inner_text(".table-bar")     # named from the old login (editable)
    kyle.goto("/")
    assert "Old Family Lasagna" in kyle.inner_text("main")
    assert kyle.locator(f"img[src='/photos/{PHOTO}']").count() >= 1
    kyle.goto("/plan")
    assert "Old Family Lasagna" in kyle.inner_text("main")
    assert "For 10" in kyle.inner_text("main")                 # the serving override survived
    kyle.goto("/shopping")
    assert "Ricotta" in kyle.inner_text("main")
    kyle.goto("/recipes/4")
    assert "Extra ricotta." in kyle.inner_text("main")
    backups = list((server["data"] / "backups").glob("*.db"))
    assert len(backups) == 1

    kyle.goto("/table")
    kyle.fill("#table-name", "Payne's Table")
    kyle.click("button:has-text('Rename')")
    kyle.click("button:has-text('Make an invite link')")
    link = kyle.input_value("[data-invite-url]")
    nat = new_phone()
    nat.goto(link)
    assert "Kyle invited you to Payne's Table" in nat.inner_text("main")
    sign_up(nat, "nat", "Nat", invite_url=link)
    assert "Payne's Table" in nat.inner_text(".table-bar")
    assert "Old Family Lasagna" in nat.inner_text("main")
    assert nat.locator("[data-table-switch]").count() == 0
