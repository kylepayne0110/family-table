# sqlite3 types the database connection.
import sqlite3

# dataclass describes validated recipe data.
from dataclasses import dataclass, field

# date handles "last made" values.
from datetime import date

# urlsplit checks the optional source link.
from urllib.parse import urlsplit

# CATEGORIES lists the allowed recipe categories.
from app.config import CATEGORIES

# FILENAME_PATTERN validates an imported photo's temporary filename.
from app.images import FILENAME_PATTERN


# Field length limits (also keep the layout tidy).
LIMITS = {
    "name": 120, "description": 600, "cuisine": 40, "card_tag": 30,
    "amount": 50, "ingredient": 200, "step_title": 80, "step_body": 2000,
    "note_author": 40, "note_body": 1000, "source_url": 500,
}
MAX_INGREDIENTS = 100
MAX_STEPS = 50

# Columns shown on cards and the featured recipe.
CARD_COLUMNS = (
    "id, name, description, category, cuisine, prep_minutes, cook_minutes, "
    "servings, photo_filename, card_tag, last_made, source_url"
)


@dataclass
class RecipeData:
    """A recipe that has passed validation."""

    name: str
    description: str
    category: str
    cuisine: str
    prep_minutes: int | None
    cook_minutes: int | None
    servings: int | None
    card_tag: str
    last_made: str | None
    ingredients: list[tuple[str, str]] = field(default_factory=list)  # (amount, name)
    steps: list[tuple[str, str]] = field(default_factory=list)  # (title, body)
    note_author: str = ""
    note_body: str = ""
    source_url: str = ""


# ---- Form parsing and validation ----

def _text(form, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _texts(form, key: str) -> list[str]:
    return [v.strip() if isinstance(v, str) else "" for v in form.getlist(key)]


def _optional_int(raw: str, label: str, low: int, high: int, errors: dict, key: str) -> int | None:
    """Parse a whole number within a range, recording an error if invalid."""
    if raw == "":
        return None
    if not raw.isdigit() or not (low <= int(raw) <= high):
        errors[key] = f"{label} should be a whole number from {low} to {high}."
        return None
    return int(raw)


def parse_recipe_form(form) -> tuple[dict, RecipeData | None, dict[str, str]]:
    """Return (values to re-display, validated data or None, errors)."""
    errors: dict[str, str] = {}

    # Pair up the repeated ingredient/step inputs, dropping fully blank rows.
    ingredients = [
        {"amount": a, "name": n}
        for a, n in zip(_texts(form, "ingredient_amount"), _texts(form, "ingredient_name"))
        if a or n
    ]
    steps = [
        {"title": t, "body": b}
        for t, b in zip(_texts(form, "step_title"), _texts(form, "step_body"))
        if t or b
    ]

    values = {
        "name": _text(form, "name"),
        "description": _text(form, "description"),
        "category": _text(form, "category"),
        "cuisine": _text(form, "cuisine"),
        "prep_minutes": _text(form, "prep_minutes"),
        "cook_minutes": _text(form, "cook_minutes"),
        "servings": _text(form, "servings"),
        "card_tag": _text(form, "card_tag"),
        "last_made": _text(form, "last_made"),
        "note_author": _text(form, "note_author"),
        "note_body": _text(form, "note_body"),
        "source_url": _text(form, "source_url"),
        # A photo found by "Import from a website", waiting to be saved.
        "imported_photo": _text(form, "imported_photo") if FILENAME_PATTERN.fullmatch(_text(form, "imported_photo")) else "",
        "ingredients": ingredients,
        "steps": steps,
    }

    if not values["name"]:
        errors["name"] = "Give the recipe a name."
    if values["category"] not in CATEGORIES:
        errors["category"] = "Choose a category."

    # Simple length checks for single-line fields.
    for key, label in [("name", "Name"), ("description", "Description"),
                       ("cuisine", "Cuisine"), ("card_tag", "Card tag"),
                       ("note_author", "Note author"), ("note_body", "Note")]:
        if len(values[key]) > LIMITS[key]:
            errors[key] = f"{label} must be {LIMITS[key]} characters or fewer."

    prep = _optional_int(values["prep_minutes"], "Prep time", 0, 1440, errors, "prep_minutes")
    cook = _optional_int(values["cook_minutes"], "Cook time", 0, 1440, errors, "cook_minutes")
    servings = _optional_int(values["servings"], "Servings", 1, 100, errors, "servings")

    last_made = values["last_made"] or None
    if last_made:
        try:
            last_made = date.fromisoformat(last_made).isoformat()
        except ValueError:
            errors["last_made"] = "Use a real date for Last made."

    if len(ingredients) > MAX_INGREDIENTS:
        errors["ingredients"] = f"Keep it to {MAX_INGREDIENTS} ingredients or fewer."
    elif any(not i["name"] for i in ingredients):
        errors["ingredients"] = "Each ingredient with an amount also needs a name."
    elif any(len(i["name"]) > LIMITS["ingredient"] or len(i["amount"]) > LIMITS["amount"] for i in ingredients):
        errors["ingredients"] = "One of the ingredients is too long."

    if len(steps) > MAX_STEPS:
        errors["steps"] = f"Keep it to {MAX_STEPS} steps or fewer."
    elif any(not s["body"] for s in steps):
        errors["steps"] = "Each step needs instructions (the title is optional)."
    elif any(len(s["title"]) > LIMITS["step_title"] or len(s["body"]) > LIMITS["step_body"] for s in steps):
        errors["steps"] = "One of the steps is too long."

    if values["source_url"]:
        link = urlsplit(values["source_url"])
        if link.scheme not in ("http", "https") or not link.netloc:
            errors["source_url"] = "The original recipe link should start with http:// or https://."
        elif len(values["source_url"]) > LIMITS["source_url"]:
            errors["source_url"] = "That link is too long."

    if values["note_body"] and not values["note_author"]:
        errors["note_author"] = "Who's the note from? (e.g. Mom)"

    if errors:
        return values, None, errors

    data = RecipeData(
        name=values["name"], description=values["description"],
        category=values["category"], cuisine=values["cuisine"],
        prep_minutes=prep, cook_minutes=cook, servings=servings,
        card_tag=values["card_tag"], last_made=last_made,
        ingredients=[(i["amount"], i["name"]) for i in ingredients],
        steps=[(s["title"], s["body"]) for s in steps],
        note_author=values["note_author"], note_body=values["note_body"],
        source_url=values["source_url"],
    )
    return values, data, {}


def recipe_to_values(recipe: dict) -> dict:
    """Turn a stored recipe into form values for the edit page."""
    as_text = lambda v: "" if v is None else str(v)
    return {
        "name": recipe["name"], "description": recipe["description"],
        "category": recipe["category"], "cuisine": recipe["cuisine"],
        "prep_minutes": as_text(recipe["prep_minutes"]),
        "cook_minutes": as_text(recipe["cook_minutes"]),
        "servings": as_text(recipe["servings"]),
        "card_tag": recipe["card_tag"], "last_made": recipe["last_made"] or "",
        "note_author": "", "note_body": "",
        "source_url": recipe["source_url"], "imported_photo": "",
        "ingredients": [{"amount": i["amount"], "name": i["name"]} for i in recipe["ingredients"]],
        "steps": [{"title": s["title"], "body": s["body"]} for s in recipe["steps"]],
    }


# ---- Queries ----

def count_recipes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]


def _like(term: str) -> str:
    """Escape LIKE wildcards so a search for '50%' is literal."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_recipes(conn: sqlite3.Connection, query: str = "", category: str = "") -> list[sqlite3.Row]:
    """Recipes in card order, filtered by category and search words."""
    sql = f"SELECT {CARD_COLUMNS} FROM recipes WHERE 1 = 1"
    params: list = []

    if category:
        sql += " AND category = ?"
        params.append(category)

    # Every search word must appear in the name, description, labels, or an ingredient.
    for term in query.split()[:8]:
        sql += """ AND (
            name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'
            OR cuisine LIKE ? ESCAPE '\\' OR card_tag LIKE ? ESCAPE '\\'
            OR EXISTS (SELECT 1 FROM ingredients i
                       WHERE i.recipe_id = recipes.id AND i.name LIKE ? ESCAPE '\\'))"""
        params.extend([_like(term)] * 5)

    sql += " ORDER BY id"
    return conn.execute(sql, params).fetchall()


def latest_note(conn: sqlite3.Connection, recipe_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT author, body, created_at FROM notes WHERE recipe_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (recipe_id,),
    ).fetchone()


def featured_recipe(conn: sqlite3.Connection, today: date) -> sqlite3.Row | None:
    """Tonight's pick: rotates one recipe per day, preferring ones with photos."""
    for where in ("WHERE photo_filename IS NOT NULL", ""):
        total = conn.execute(f"SELECT COUNT(*) FROM recipes {where}").fetchone()[0]
        if total:
            return conn.execute(
                f"SELECT {CARD_COLUMNS} FROM recipes {where} ORDER BY id LIMIT 1 OFFSET ?",
                (today.toordinal() % total,),
            ).fetchone()
    return None


def get_recipe(conn: sqlite3.Connection, recipe_id: int) -> dict | None:
    """A full recipe with ingredients, steps, and notes."""
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    recipe = dict(row)
    recipe["ingredients"] = conn.execute(
        "SELECT amount, name FROM ingredients WHERE recipe_id = ? ORDER BY position", (recipe_id,)
    ).fetchall()
    recipe["steps"] = conn.execute(
        "SELECT title, body FROM steps WHERE recipe_id = ? ORDER BY position", (recipe_id,)
    ).fetchall()
    recipe["notes"] = conn.execute(
        "SELECT id, author, body, created_at FROM notes WHERE recipe_id = ? ORDER BY created_at DESC, id DESC",
        (recipe_id,),
    ).fetchall()
    return recipe


# ---- Writes ----

def save_recipe(conn: sqlite3.Connection, data: RecipeData, photo_filename: str | None,
                recipe_id: int | None = None) -> int:
    """Insert or update a recipe and its children in one transaction."""
    fields = (data.name, data.description, data.category, data.cuisine, data.prep_minutes,
              data.cook_minutes, data.servings, photo_filename, data.card_tag, data.last_made,
              data.source_url)

    # "with conn" commits on success and rolls back on any error.
    with conn:
        if recipe_id is None:
            cur = conn.execute(
                """INSERT INTO recipes (name, description, category, cuisine, prep_minutes,
                       cook_minutes, servings, photo_filename, card_tag, last_made, source_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                fields,
            )
            recipe_id = cur.lastrowid
            if data.note_body:
                conn.execute(
                    "INSERT INTO notes (recipe_id, author, body) VALUES (?, ?, ?)",
                    (recipe_id, data.note_author, data.note_body),
                )
        else:
            conn.execute(
                """UPDATE recipes SET name = ?, description = ?, category = ?, cuisine = ?,
                       prep_minutes = ?, cook_minutes = ?, servings = ?, photo_filename = ?,
                       card_tag = ?, last_made = ?, source_url = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (*fields, recipe_id),
            )
            # Simplest correct approach: replace the ordered lists wholesale.
            conn.execute("DELETE FROM ingredients WHERE recipe_id = ?", (recipe_id,))
            conn.execute("DELETE FROM steps WHERE recipe_id = ?", (recipe_id,))

        conn.executemany(
            "INSERT INTO ingredients (recipe_id, position, amount, name) VALUES (?, ?, ?, ?)",
            [(recipe_id, pos, amount, name) for pos, (amount, name) in enumerate(data.ingredients)],
        )
        conn.executemany(
            "INSERT INTO steps (recipe_id, position, title, body) VALUES (?, ?, ?, ?)",
            [(recipe_id, pos, title, body) for pos, (title, body) in enumerate(data.steps)],
        )
    return recipe_id


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> None:
    """Delete a recipe; ingredients, steps, and notes cascade."""
    with conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))


def mark_made(conn: sqlite3.Connection, recipe_id: int, made_on: date) -> None:
    with conn:
        conn.execute(
            "UPDATE recipes SET last_made = ?, updated_at = datetime('now') WHERE id = ?",
            (made_on.isoformat(), recipe_id),
        )


def add_note(conn: sqlite3.Connection, recipe_id: int, author: str, body: str) -> None:
    with conn:
        conn.execute("INSERT INTO notes (recipe_id, author, body) VALUES (?, ?, ?)", (recipe_id, author, body))


def delete_note(conn: sqlite3.Connection, recipe_id: int, note_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM notes WHERE id = ? AND recipe_id = ?", (note_id, recipe_id))
