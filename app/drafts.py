# Recipe drafts: unsaved Add/Edit recipe forms kept on the server so a lock screen, a lost
# connection, or a reload doesn't lose work. A draft is never a recipe: only the form's own
# Save button adds a recipe to the Table's recipe book.

# json stores the form fields.
import json

# sqlite3 types the connection.
import sqlite3

# Project modules.
from app.database import write_transaction
from app.meals import LIMITS, MAX_INGREDIENTS, MAX_STEPS

FIELDS = ("name", "description", "category", "cuisine", "prep_minutes", "cook_minutes", "servings", "card_tag",
          "creator_credit", "source_url", "note_author", "note_body", "imported_photo")
MAX_DRAFTS_PER_PERSON = 20


class DraftError(Exception):
    """The draft can't be used (missing, someone else's, or changed meanwhile)."""


class DraftConflict(DraftError):
    """The draft was saved from another tab or phone after this one loaded it."""


def clean_fields(raw: dict) -> dict:
    """Keep only known form fields, as strings within the form's own limits."""
    out = {}
    for key in FIELDS:
        value = raw.get(key, "")
        out[key] = str(value if value is not None else "")[:LIMITS.get(key, 600) if key in LIMITS else 600]
    ingredients = raw.get("ingredients") or []
    steps = raw.get("steps") or []
    out["ingredients"] = [{"amount": str(i.get("amount", ""))[:LIMITS["amount"]],
                           "name": str(i.get("name", ""))[:LIMITS["ingredient"]]}
                          for i in ingredients[:MAX_INGREDIENTS] if isinstance(i, dict)]
    out["steps"] = [{"title": str(s.get("title", ""))[:LIMITS["step_title"]],
                     "body": str(s.get("body", ""))[:LIMITS["step_body"]]}
                    for s in steps[:MAX_STEPS] if isinstance(s, dict)]
    out["ai_generated"] = bool(raw.get("ai_generated"))
    return out


def create(conn: sqlite3.Connection, table_id: int, user_id: int, fields: dict, recipe_id: int | None = None,
           voice: dict | None = None) -> int:
    with write_transaction(conn):
        count = conn.execute("SELECT COUNT(*) FROM recipe_drafts WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= MAX_DRAFTS_PER_PERSON:   # keep the newest; old abandoned drafts go
            conn.execute("""DELETE FROM recipe_drafts WHERE id IN (SELECT id FROM recipe_drafts WHERE user_id = ?
                            ORDER BY updated_at LIMIT ?)""", (user_id, count - MAX_DRAFTS_PER_PERSON + 1))
        return conn.execute(
            "INSERT INTO recipe_drafts (table_id, user_id, recipe_id, fields_json, voice_json) VALUES (?, ?, ?, ?, ?)",
            (table_id, user_id, recipe_id, json.dumps(clean_fields(fields)), json.dumps(voice or {}))).lastrowid


def get(conn: sqlite3.Connection, draft_id: int, user_id: int, table_id: int | None = None) -> dict | None:
    """Your own draft (in this Table, when given), or None."""
    sql = "SELECT * FROM recipe_drafts WHERE id = ? AND user_id = ?"
    params: list = [draft_id, user_id]
    if table_id is not None:
        sql += " AND table_id = ?"
        params.append(table_id)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft["fields"] = json.loads(draft["fields_json"])
    draft["voice"] = json.loads(draft["voice_json"])
    return draft


def latest(conn: sqlite3.Connection, user_id: int, table_id: int, recipe_id: int | None = None) -> dict | None:
    """Your most recent unsaved draft for a new recipe (or for this recipe's edit form)."""
    if recipe_id is None:
        row = conn.execute("""SELECT id FROM recipe_drafts WHERE user_id = ? AND table_id = ? AND recipe_id IS NULL
                              ORDER BY updated_at DESC LIMIT 1""", (user_id, table_id)).fetchone()
    else:
        row = conn.execute("""SELECT id FROM recipe_drafts WHERE user_id = ? AND table_id = ? AND recipe_id = ?
                              ORDER BY updated_at DESC LIMIT 1""", (user_id, table_id, recipe_id)).fetchone()
    return get(conn, row["id"], user_id) if row else None


def save(conn: sqlite3.Connection, draft_id: int, user_id: int, table_id: int, fields: dict | None,
         rev: int | None, voice: dict | None = None) -> int:
    """Store the form as it is now. Returns the new revision.

    A save carrying an older revision (another tab wrote meanwhile) is refused with
    DraftConflict, so two tabs never silently overwrite each other's draft.
    """
    with write_transaction(conn):
        row = conn.execute("SELECT rev FROM recipe_drafts WHERE id = ? AND user_id = ? AND table_id = ?",
                           (draft_id, user_id, table_id)).fetchone()
        if row is None:
            raise DraftError("That draft is no longer available.")
        if rev is not None and rev != row["rev"]:
            raise DraftConflict("This draft was changed in another tab or on another phone.")
        sets, params = ["rev = rev + 1", "updated_at = datetime('now')"], []
        if fields is not None:
            sets.append("fields_json = ?")
            params.append(json.dumps(clean_fields(fields)))
        if voice is not None:
            sets.append("voice_json = ?")
            params.append(json.dumps(voice))
        conn.execute(f"UPDATE recipe_drafts SET {', '.join(sets)} WHERE id = ?", (*params, draft_id))
        return row["rev"] + 1


def discard(conn: sqlite3.Connection, draft_id: int, user_id: int) -> None:
    """Remove a draft (after its recipe was saved, or when the person starts over)."""
    with conn:
        conn.execute("DELETE FROM recipe_drafts WHERE id = ? AND user_id = ?", (draft_id, user_id))
