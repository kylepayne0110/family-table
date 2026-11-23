# Recipe drafts: unsaved Add/Edit recipe forms kept on the server so work isn't lost.
# (Filled in by the recipe-creation stage.)

# sqlite3 types the connection.
import sqlite3


def discard(conn: sqlite3.Connection, draft_id: int, user_id: int) -> None:
    """Remove a draft after its recipe was saved."""
    with conn:
        conn.execute("DELETE FROM recipe_drafts WHERE id = ? AND user_id = ?", (draft_id, user_id))
