# Saving and reading the Cooking Assistant conversation for one planned dinner.
# Each planned dinner has its own conversation, even when two dinners use the same recipe.

# json stores structured reply details and proposed changes.
import json

# sqlite3 types the connection and reports a duplicate message.
import sqlite3

# write_transaction locks the database for each change.
from app.database import write_transaction

# Project modules.
from app import meals
from app.assistant import Reply


def history(conn: sqlite3.Connection, meal_id: int) -> list[dict]:
    """Every message for a dinner, oldest first, with any proposed recipe change attached."""
    messages = [dict(r) for r in conn.execute(
        "SELECT id, role, body, kind, data_json, created_at FROM meal_messages WHERE meal_id = ? ORDER BY id",
        (meal_id,),
    )]
    proposals = {r["message_id"]: dict(r) for r in conn.execute(
        "SELECT id, message_id, changes_json, status FROM recipe_edit_proposals WHERE meal_id = ?", (meal_id,)
    )}
    for message in messages:
        proposal = proposals.get(message["id"])
        if proposal:
            proposal["changes"] = json.loads(proposal["changes_json"])
        message["proposal"] = proposal
    return messages


def save_user_message(conn: sqlite3.Connection, meal_id: int, text: str, request_key: str | None) -> bool:
    """Store what the cook typed. False if this exact message (same request key) was already saved."""
    try:
        with write_transaction(conn):
            conn.execute("INSERT INTO meal_messages (meal_id, role, body, request_key) VALUES (?, 'user', ?, ?)",
                         (meal_id, text, request_key))
    except sqlite3.IntegrityError:
        return False   # a retried or double-sent message: it's already in the conversation
    return True


def save_reply(conn: sqlite3.Connection, meal: dict, reply: Reply) -> None:
    """Store the assistant's reply, plus a pending proposal when it suggests a recipe change."""
    with write_transaction(conn):
        message_id = conn.execute(
            "INSERT INTO meal_messages (meal_id, role, body, kind, data_json) VALUES (?, 'assistant', ?, ?, ?)",
            (meal["id"], reply.body, reply.kind, json.dumps(reply.data)),
        ).lastrowid
        if reply.changes:
            conn.execute(
                """INSERT INTO recipe_edit_proposals (meal_id, recipe_id, message_id, changes_json)
                   VALUES (?, ?, ?, ?)""",
                (meal["id"], meal["recipe_id"], message_id, json.dumps(reply.changes)),
            )


class ProposalError(Exception):
    """The proposal doesn't exist for this dinner."""


def decide_proposal(conn: sqlite3.Connection, meal: dict, proposal_id: int, confirm: bool) -> str:
    """Confirm (save to the recipe) or cancel a proposed change. Returns the proposal's new status.

    Deciding twice is harmless: a proposal that's already decided is left as it is.
    """
    with write_transaction(conn):
        row = conn.execute(
            "SELECT id, recipe_id, changes_json, status FROM recipe_edit_proposals WHERE id = ? AND meal_id = ?",
            (proposal_id, meal["id"]),
        ).fetchone()
        if row is None:
            raise ProposalError("That suggestion isn't part of this conversation.")
        if row["status"] != "pending":
            return row["status"]
        if confirm:
            try:
                meals.apply_recipe_changes_in_transaction(conn, row["recipe_id"], json.loads(row["changes_json"]))
                status, body = "confirmed", "Saved. The recipe box has the new version, with the same serving size as before."
            except meals.StaleChange:
                status, body = "stale", ("The recipe was edited since I suggested that, so I didn't change anything. "
                                         "Ask again and I'll suggest it against the current recipe.")
        else:
            status, body = "cancelled", "Okay, I left the saved recipe as it was."
        conn.execute("UPDATE recipe_edit_proposals SET status = ?, decided_at = datetime('now') WHERE id = ?",
                     (status, proposal_id))
        conn.execute("INSERT INTO meal_messages (meal_id, role, body) VALUES (?, 'assistant', ?)", (meal["id"], body))
    return status
