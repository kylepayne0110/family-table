# Community recipes: opt-in, published snapshots shared across Family Table.
#
# - A Table's recipes are private. Publishing makes a snapshot (a "listing") with its own
#   copy of the photo; editing the family recipe never changes it. Only an explicit
#   "Update community recipe" publishes a new version.
# - Published data is only the recipe itself plus credit: never notes, history, who added
#   it, conversations, or account details beyond the publisher's display name.
# - Saving a listing copies the exact version the person saw into their Table as an
#   independent recipe (with its own photo copy), keeping the original credit.
# - Search uses SQLite's full-text index over published listings only.

# json stores the snapshot of each version.
import json

# logging records moderation events (ids only).
import logging

# re cleans search words.
import re

# shutil copies photo files; sqlite3 types the connection.
import shutil
import sqlite3

# dataclass describes a published snapshot.
from dataclasses import asdict, dataclass, field

# Path builds photo paths.
from pathlib import Path

# Project modules.
from app.database import write_transaction
from app.images import delete_photo, photo_path


log = logging.getLogger("family_table.community")

PER_PAGE = 20
HIDE_AFTER_REPORTS = 3
REPORT_REASONS = {
    "spam": "Spam or advertising",
    "offensive": "Offensive or inappropriate",
    "unsafe": "Unsafe food advice",
    "copyright": "Copied without permission",
    "personal": "Contains someone's personal information",
    "other": "Something else",
}


class CommunityError(Exception):
    """Something that can't be done (the message is safe to show)."""


class NotAllowed(CommunityError):
    """Only members of the recipe's Table (or its publisher) can do that."""


@dataclass
class Snapshot:
    """Exactly what a listing version shows. Built only from recipe content, never private fields."""

    name: str
    description: str = ""
    category: str = ""
    cuisine: str = ""
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    servings: int | None = None
    creator_credit: str = ""
    source_url: str = ""
    ingredients: list[list[str]] = field(default_factory=list)   # [amount, name]
    steps: list[list[str]] = field(default_factory=list)         # [title, body]


def snapshot_of(recipe: dict, creator_credit: str | None = None) -> Snapshot:
    """What would be published from a family recipe. Notes, last made, the handwritten tag,
    who added it, and anything about the Table are deliberately left out."""
    return Snapshot(
        name=recipe["name"], description=recipe["description"], category=recipe["category"],
        cuisine=recipe["cuisine"], prep_minutes=recipe["prep_minutes"], cook_minutes=recipe["cook_minutes"],
        servings=recipe["servings"],
        creator_credit=(recipe.get("creator_credit") or "" if creator_credit is None else creator_credit).strip()[:80],
        source_url=recipe.get("source_url") or "",
        ingredients=[[i["amount"], i["name"]] for i in recipe["ingredients"]],
        steps=[[s["title"], s["body"]] for s in recipe["steps"]],
    )


# ---- Photos: every owner gets its own file ----

def copy_photo(upload_dir: Path, filename: str | None) -> str | None:
    """Copy a stored photo to a new random name (so a listing or a saved copy never shares a
    file with a private recipe). Returns the new name, or None."""
    import secrets
    source = photo_path(upload_dir, filename or "")
    if source is None or not source.is_file():
        return None
    new_name = f"{secrets.token_hex(16)}.webp"
    temp = upload_dir / f".{new_name}.tmp"
    shutil.copyfile(source, temp)
    temp.replace(upload_dir / new_name)
    return new_name


# ---- Reading ----

def listing_for_recipe(conn: sqlite3.Connection, recipe_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM community_listings WHERE source_recipe_id = ?", (recipe_id,)).fetchone()
    return dict(row) if row else None


def get_listing(conn: sqlite3.Connection, listing_id: int, version: int | None = None,
                include_unpublished: bool = False) -> dict | None:
    """A listing with one version's snapshot (the current one unless asked)."""
    row = conn.execute("SELECT * FROM community_listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None or (row["status"] != "published" and not include_unpublished):
        return None
    listing = dict(row)
    wanted = version or listing["version"]
    ver = conn.execute("SELECT * FROM community_versions WHERE listing_id = ? AND version = ?",
                       (listing_id, wanted)).fetchone()
    if ver is None:
        return None
    listing["shown_version"] = wanted
    listing["snapshot"] = json.loads(ver["snapshot_json"])
    listing["shown_photo"] = ver["photo_filename"]
    return listing


def _fts_query(text: str) -> str:
    """Search words as an FTS5 query: every word must match (as a prefix). Never raw syntax."""
    words = re.findall(r"[^\W_]{2,}", text.lower())[:8]
    return " ".join(f'"{w}"*' for w in words)


def search(conn: sqlite3.Connection, query: str = "", page: int = 1, per_page: int = PER_PAGE) -> dict:
    """Published listings matching a dish or ingredient, best matches first, one page at a time."""
    page = max(1, min(page, 500))
    offset = (page - 1) * per_page
    match = _fts_query(query[:100])
    columns = ("l.id, l.name, l.description, l.creator_credit, l.publisher_name, l.photo_filename, "
               "l.prep_minutes, l.cook_minutes, l.servings, l.version, l.cuisine, l.ingredient_count")
    if match:
        rows = conn.execute(
            f"""SELECT {columns} FROM community_fts f JOIN community_listings l ON l.id = f.rowid
                WHERE community_fts MATCH ? AND l.status = 'published'
                ORDER BY bm25(community_fts, 4.0, 2.0, 1.0), l.id DESC LIMIT ? OFFSET ?""",
            (match, per_page + 1, offset)).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {columns} FROM community_listings l WHERE l.status = 'published'
                ORDER BY l.updated_at DESC, l.id DESC LIMIT ? OFFSET ?""", (per_page + 1, offset)).fetchall()
    items = [dict(r) for r in rows[:per_page]]
    return {"items": items, "page": page, "has_more": len(rows) > per_page, "query": query}


def ingredient_matches(conn: sqlite3.Connection, words: list[str], limit: int = 8) -> list[dict]:
    """Published listings that use any of these ingredients (for "what can I make with…")."""
    terms = [w for w in (re.sub(r"[^\w]", "", w.lower()) for w in words) if len(w) >= 2][:10]
    if not terms:
        return []
    match = "ingredients : (" + " OR ".join(f'"{t}"*' for t in terms) + ")"
    rows = conn.execute(
        """SELECT l.id, l.name, l.creator_credit, l.publisher_name, l.version, l.photo_filename
           FROM community_fts f JOIN community_listings l ON l.id = f.rowid
           WHERE community_fts MATCH ? AND l.status = 'published'
           ORDER BY bm25(community_fts, 1.0, 4.0, 0.5) LIMIT ?""", (match, limit)).fetchall()
    out = []
    for r in rows:
        ver = conn.execute("SELECT snapshot_json FROM community_versions WHERE listing_id = ? AND version = ?",
                           (r["id"], r["version"])).fetchone()
        out.append({**dict(r), "ingredients": json.loads(ver["snapshot_json"])["ingredients"] if ver else []})
    return out


def saved_copy(conn: sqlite3.Connection, listing_id: int, version: int, table_id: int) -> int | None:
    row = conn.execute("SELECT recipe_id FROM community_saves WHERE listing_id = ? AND version = ? AND table_id = ?",
                       (listing_id, version, table_id)).fetchone()
    return row["recipe_id"] if row else None


def can_manage(conn: sqlite3.Connection, listing: dict, user_id: int) -> bool:
    """Members of the recipe's Table manage its listing; the publisher can always withdraw it."""
    if listing.get("publisher_id") == user_id:
        return True
    table_id = listing.get("source_table_id")
    return bool(table_id) and conn.execute("SELECT 1 FROM memberships WHERE table_id = ? AND user_id = ?",
                                           (table_id, user_id)).fetchone() is not None


# ---- Publishing ----

def _index(conn: sqlite3.Connection, listing_id: int, snap: Snapshot | dict) -> None:
    data = snap if isinstance(snap, dict) else asdict(snap)
    conn.execute("DELETE FROM community_fts WHERE rowid = ?", (listing_id,))
    conn.execute("INSERT INTO community_fts (rowid, name, ingredients, extra) VALUES (?, ?, ?, ?)",
                 (listing_id, data["name"], " ".join(i[1] for i in data["ingredients"]),
                  " ".join(filter(None, [data.get("cuisine", ""), data.get("description", ""),
                                         data.get("creator_credit", "")]))))


def _unindex(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("DELETE FROM community_fts WHERE rowid = ?", (listing_id,))


def publish(conn: sqlite3.Connection, upload_dir: Path, recipe: dict, user: dict, include_photo: bool,
            creator_credit: str, expected_recipe_version: int | None = None) -> tuple[int, str]:
    """Publish (or re-publish after withdrawing) a family recipe. Returns (listing id, outcome).

    outcome is "published", or "already" when it was already published (a double tap or retry).
    The caller has checked the person is a member of the recipe's Table.
    """
    snap = snapshot_of(recipe, creator_credit)
    if not snap.name or not snap.ingredients:
        raise CommunityError("Add the ingredients before sharing, so other families can cook it.")
    photo = copy_photo(upload_dir, recipe["photo_filename"]) if include_photo else None
    try:
        with write_transaction(conn):
            current = conn.execute("SELECT version FROM recipes WHERE id = ?", (recipe["id"],)).fetchone()
            if current is None:
                raise CommunityError("That recipe was deleted.")
            if expected_recipe_version is not None and current["version"] != expected_recipe_version:
                raise CommunityError("The recipe changed while you were looking at the preview. Check it again.")
            existing = conn.execute("SELECT id, status, version FROM community_listings WHERE source_recipe_id = ?",
                                    (recipe["id"],)).fetchone()
            if existing and existing["status"] == "published":
                delete_photo(upload_dir, photo)   # a repeat tap: the listing already exists
                return existing["id"], "already"
            if existing and existing["status"] in ("hidden", "removed"):
                raise CommunityError("This recipe's community page was hidden after reports and can't be re-shared.")
            if existing:   # withdrawn earlier: bring the same page back with a new version
                listing_id, version = existing["id"], existing["version"] + 1
            else:
                listing_id, version = None, 1
            fields = (recipe["table_id"], recipe["id"], current["version"], user["id"], user["display_name"][:60],
                      version, snap.name, snap.description, snap.category, snap.cuisine, snap.prep_minutes,
                      snap.cook_minutes, snap.servings, snap.creator_credit, photo, len(snap.ingredients))
            if listing_id is None:
                listing_id = conn.execute(
                    """INSERT INTO community_listings (source_table_id, source_recipe_id, source_recipe_version,
                           publisher_id, publisher_name, version, name, description, category, cuisine, prep_minutes,
                           cook_minutes, servings, creator_credit, photo_filename, ingredient_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", fields).lastrowid
            else:
                conn.execute(
                    """UPDATE community_listings SET source_table_id = ?, source_recipe_id = ?,
                           source_recipe_version = ?, publisher_id = ?, publisher_name = ?, version = ?, name = ?,
                           description = ?, category = ?, cuisine = ?, prep_minutes = ?, cook_minutes = ?,
                           servings = ?, creator_credit = ?, photo_filename = ?, ingredient_count = ?,
                           status = 'published', withdrawn_at = NULL, updated_at = datetime('now')
                       WHERE id = ?""", (*fields, listing_id))
            conn.execute("INSERT INTO community_versions (listing_id, version, snapshot_json, photo_filename) "
                         "VALUES (?, ?, ?, ?)", (listing_id, version, json.dumps(asdict(snap)), photo))
            _index(conn, listing_id, snap)
    except BaseException:
        delete_photo(upload_dir, photo)
        raise
    log.info("listing %s published (version %s) from recipe %s", listing_id, version, recipe["id"])
    return listing_id, "published"


def update_listing(conn: sqlite3.Connection, upload_dir: Path, recipe: dict, user: dict, include_photo: bool,
                   creator_credit: str, seen_listing_version: int | None) -> int:
    """'Update community recipe': publish the family copy as the next version. Returns the new version.

    Families who already saved an earlier version keep their own copies unchanged.
    """
    snap = snapshot_of(recipe, creator_credit)
    if not snap.ingredients:
        raise CommunityError("Add the ingredients before updating the community recipe.")
    photo = copy_photo(upload_dir, recipe["photo_filename"]) if include_photo else None
    try:
        with write_transaction(conn):
            row = conn.execute("SELECT id, status, version FROM community_listings WHERE source_recipe_id = ?",
                               (recipe["id"],)).fetchone()
            if row is None or row["status"] != "published":
                raise CommunityError("This recipe isn't shared with the community right now.")
            if seen_listing_version is not None and row["version"] != seen_listing_version:
                raise CommunityError("Someone else just updated the community recipe. Check the preview again.")
            version = row["version"] + 1
            current = conn.execute("SELECT version FROM recipes WHERE id = ?", (recipe["id"],)).fetchone()
            conn.execute(
                """UPDATE community_listings SET version = ?, source_recipe_version = ?, name = ?, description = ?,
                       category = ?, cuisine = ?, prep_minutes = ?, cook_minutes = ?, servings = ?,
                       creator_credit = ?, photo_filename = ?, ingredient_count = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (version, current["version"], snap.name, snap.description, snap.category, snap.cuisine,
                 snap.prep_minutes, snap.cook_minutes, snap.servings, snap.creator_credit, photo,
                 len(snap.ingredients), row["id"]))
            conn.execute("INSERT INTO community_versions (listing_id, version, snapshot_json, photo_filename) "
                         "VALUES (?, ?, ?, ?)", (row["id"], version, json.dumps(asdict(snap)), photo))
            _index(conn, row["id"], snap)
    except BaseException:
        delete_photo(upload_dir, photo)
        raise
    log.info("listing %s updated to version %s by user %s", row["id"], version, user["id"])
    return version


def withdraw(conn: sqlite3.Connection, upload_dir: Path, listing_id: int, user_id: int) -> None:
    """Take a recipe out of the community. Saved copies in other Tables keep working (they have their own photos)."""
    with write_transaction(conn):
        listing = conn.execute("SELECT * FROM community_listings WHERE id = ?", (listing_id,)).fetchone()
        if listing is None:
            raise CommunityError("That community recipe doesn't exist.")
        if not can_manage(conn, dict(listing), user_id):
            raise NotAllowed("Only members of the Table that shared this recipe can withdraw it.")
        if listing["status"] == "withdrawn":
            return
        photos = [r[0] for r in conn.execute(
            "SELECT DISTINCT photo_filename FROM community_versions WHERE listing_id = ? AND photo_filename IS NOT NULL",
            (listing_id,))]
        conn.execute("""UPDATE community_listings SET status = 'withdrawn', withdrawn_at = datetime('now'),
                        photo_filename = NULL, updated_at = datetime('now') WHERE id = ?""", (listing_id,))
        conn.execute("UPDATE community_versions SET photo_filename = NULL WHERE listing_id = ?", (listing_id,))
        _unindex(conn, listing_id)
    for photo in photos:
        delete_photo(upload_dir, photo)
    log.info("listing %s withdrawn by user %s", listing_id, user_id)


# ---- Saving into a Table ----

def save_to_table(conn: sqlite3.Connection, upload_dir: Path, listing_id: int, version: int, table_id: int,
                  user_id: int) -> tuple[int, bool]:
    """Copy one listing version into a Table. Returns (recipe id, created).

    Safe to repeat: the same version saved again to the same Table returns the existing copy.
    The caller has checked membership of `table_id`.
    """
    existing = saved_copy(conn, listing_id, version, table_id)
    if existing:
        return existing, False
    listing = get_listing(conn, listing_id, version)
    if listing is None:
        raise CommunityError("That community recipe was withdrawn or changed. Open it again to see what's there now.")
    snap = listing["snapshot"]
    photo = copy_photo(upload_dir, listing["shown_photo"])
    credit = f"Saved from the community, shared by {listing['publisher_name']}"
    try:
        with write_transaction(conn):
            again = conn.execute("SELECT recipe_id FROM community_saves WHERE listing_id = ? AND version = ? "
                                 "AND table_id = ?", (listing_id, version, table_id)).fetchone()
            if again:
                delete_photo(upload_dir, photo)
                return again["recipe_id"], False
            status = conn.execute("SELECT status FROM community_listings WHERE id = ?", (listing_id,)).fetchone()
            if status is None or status["status"] != "published":
                raise CommunityError("That community recipe was just withdrawn.")
            recipe_id = conn.execute(
                """INSERT INTO recipes (table_id, name, description, category, cuisine, prep_minutes, cook_minutes,
                       servings, photo_filename, source_url, creator_credit, added_by, community_listing_id,
                       community_version, community_credit)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (table_id, snap["name"][:120], snap["description"][:600], _category(snap["category"]),
                 snap["cuisine"][:40], snap["prep_minutes"], snap["cook_minutes"], snap["servings"], photo,
                 snap.get("source_url", "")[:500], snap["creator_credit"][:80], user_id, listing_id, version,
                 credit[:140])).lastrowid
            conn.executemany("INSERT INTO ingredients (recipe_id, position, amount, name) VALUES (?, ?, ?, ?)",
                             [(recipe_id, i, a[:50], n[:200]) for i, (a, n) in enumerate(snap["ingredients"]) if n])
            conn.executemany("INSERT INTO steps (recipe_id, position, title, body) VALUES (?, ?, ?, ?)",
                             [(recipe_id, i, t[:80], b[:2000]) for i, (t, b) in enumerate(snap["steps"]) if b])
            conn.execute("INSERT INTO community_saves (listing_id, version, table_id, recipe_id, saved_by) "
                         "VALUES (?, ?, ?, ?, ?)", (listing_id, version, table_id, recipe_id, user_id))
    except BaseException:
        delete_photo(upload_dir, photo)
        raise
    return recipe_id, True


def _category(value: str) -> str:
    from app.config import CATEGORIES
    return value if value in CATEGORIES else "weeknight"


# ---- Reports ----

def report(conn: sqlite3.Connection, listing_id: int, user_id: int, reason: str, details: str) -> bool:
    """Report a listing. Each person counts once; after a few reports it's hidden until reviewed.

    Returns True if this is a new report.
    """
    if reason not in REPORT_REASONS:
        raise CommunityError("Choose a reason for the report.")
    with write_transaction(conn):
        if conn.execute("SELECT 1 FROM community_listings WHERE id = ? AND status = 'published'",
                        (listing_id,)).fetchone() is None:
            raise CommunityError("That community recipe isn't available.")
        try:
            conn.execute("INSERT INTO community_reports (listing_id, reporter_id, reason, details) VALUES (?, ?, ?, ?)",
                         (listing_id, user_id, reason, " ".join(details.split())[:500]))
        except sqlite3.IntegrityError:
            return False
        count = conn.execute("SELECT COUNT(*) FROM community_reports WHERE listing_id = ?", (listing_id,)).fetchone()[0]
        conn.execute("UPDATE community_listings SET report_count = ? WHERE id = ?", (count, listing_id))
        if count >= HIDE_AFTER_REPORTS:
            conn.execute("UPDATE community_listings SET status = 'hidden' WHERE id = ?", (listing_id,))
            _unindex(conn, listing_id)
            log.warning("listing %s hidden after %s reports; review with python -m app.community reports",
                        listing_id, count)
    return True


def main(argv: list[str] | None = None) -> int:
    """Moderation for the person who runs Family Table:

        python -m app.community reports            # listings with reports
        python -m app.community restore LISTING_ID # show it again (clears reports)
        python -m app.community remove LISTING_ID  # take it down for good
    """
    import argparse
    from app.config import load_settings
    from app.database import connect, init_db
    parser = argparse.ArgumentParser(description="Community moderation")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reports")
    for name in ("restore", "remove"):
        sub.add_parser(name).add_argument("listing_id", type=int)
    args = parser.parse_args(argv)
    settings = load_settings()
    init_db(settings.database_path)
    conn = connect(settings.database_path)
    try:
        if args.command == "reports":
            for row in conn.execute("""SELECT l.id, l.name, l.status, l.report_count, group_concat(r.reason) AS reasons
                                       FROM community_listings l JOIN community_reports r ON r.listing_id = l.id
                                       GROUP BY l.id ORDER BY l.report_count DESC"""):
                print(f"{row['id']:>5}  {row['status']:<9} {row['report_count']} report(s)  {row['name']}  [{row['reasons']}]")
            return 0
        listing = get_listing(conn, args.listing_id, include_unpublished=True)
        if listing is None:
            print("No such listing.")
            return 1
        with write_transaction(conn):
            if args.command == "restore":
                conn.execute("UPDATE community_listings SET status = 'published', report_count = 0 WHERE id = ?",
                             (args.listing_id,))
                conn.execute("DELETE FROM community_reports WHERE listing_id = ?", (args.listing_id,))
                _index(conn, args.listing_id, listing["snapshot"])
            else:
                conn.execute("UPDATE community_listings SET status = 'removed' WHERE id = ?", (args.listing_id,))
                _unindex(conn, args.listing_id)
        print(f"Listing {args.listing_id}: {args.command}d.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
