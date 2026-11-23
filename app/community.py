# Community recipes: opt-in, published snapshots shared across Family Table.
# (Filled in by the community stage.)

# sqlite3 types the connection.
import sqlite3


def listing_for_recipe(conn: sqlite3.Connection, recipe_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM community_listings WHERE source_recipe_id = ?", (recipe_id,)).fetchone()
    return dict(row) if row else None
