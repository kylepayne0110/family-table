# "What can we make?" Ideas from ingredients on hand.
#
# Three sources, kept clearly apart in the page:
#   - familiar: recipes in *your Table's* book that use what you have (marked as made, or
#     only ever planned);
#   - community: published community recipes that use them;
#   - something new: an idea the local model invents, labelled as AI-generated.
# Access rules come first: only this Table's recipes and published listings are searched,
# with indexed queries and small limits. The model only ever sees the words the person
# typed (never anyone's recipes).

# json reads the model's answer; re splits the typed list.
import json
import re

# sqlite3 types the connection; date compares planned days.
import sqlite3
from datetime import date

# Project modules.
from app import community
from app.ingredients import matches_staple, name_key
from app.ollama import AssistantUnavailable

MAX_TERMS = 12


def on_hand_terms(text: str) -> list[str]:
    """'Chicken, rice and broccoli' -> ['chicken', 'rice', 'broccoli'] (normalized, at most 12)."""
    text = re.sub(r"^\s*(?:i have|we have|i've got|we've got|got)\s+", "", (text or "").lower()[:300])
    parts = re.split(r",|;|\n|\band\b|&|\+|/", text)
    terms = []
    for part in parts:
        key = name_key(part.strip(" .!?"))
        if key and len(key) <= 40 and key not in terms:
            terms.append(key)
    return terms[:MAX_TERMS]


def _matches(term: str, ingredient_key: str) -> bool:
    words = ingredient_key.split()
    return term == ingredient_key or term in words or (len(term) > 3 and term in ingredient_key) \
        or all(w in words for w in term.split())


def _missing(ingredients: list[tuple[str, str]], terms: list[str], staples: set[str]) -> tuple[list[str], list[str]]:
    """(ingredients you have, extra ingredients you'd need), ignoring Kitchen Staples."""
    have, need = [], []
    for _amount, name in ingredients:
        key = name_key(name)
        if not key:
            continue
        if any(_matches(t, key) for t in terms):
            have.append(name)
        elif not matches_staple(key, staples):
            need.append(name)
    return have, need


def staples_of(conn: sqlite3.Connection, table_id: int) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name_key FROM staples WHERE table_id = ?", (table_id,))}


def familiar(conn: sqlite3.Connection, table_id: int, terms: list[str], today: date, limit: int = 6) -> list[dict]:
    """Recipes from this Table's own book that use what's on hand, best match first."""
    if not terms:
        return []
    likes = " OR ".join("i.name LIKE ? ESCAPE '\\'" for _ in terms)
    params = [table_id] + [f"%{t.replace('%', '').replace('_', '')}%" for t in terms]
    candidates = conn.execute(
        f"""SELECT DISTINCT r.id, r.name, r.last_made, r.photo_filename, r.prep_minutes, r.cook_minutes
            FROM recipes r JOIN ingredients i ON i.recipe_id = r.id
            WHERE r.table_id = ? AND ({likes}) LIMIT 60""", params).fetchall()
    staples = staples_of(conn, table_id)
    out = []
    for r in candidates:
        ings = [(row["amount"], row["name"]) for row in conn.execute(
            "SELECT amount, name FROM ingredients WHERE recipe_id = ? ORDER BY position", (r["id"],))]
        have, need = _missing(ings, terms, staples)
        if not have:
            continue
        last_planned = conn.execute(
            "SELECT MAX(date(week_start, '+' || day || ' days')) FROM planned_meals WHERE recipe_id = ? AND table_id = ?",
            (r["id"], table_id)).fetchone()[0]
        history = ("made" if r["last_made"] else
                   "planned" if last_planned and last_planned < today.isoformat() else "in the book")
        out.append({**dict(r), "have": have, "need": need[:6], "need_count": len(need), "history": history,
                    "score": (len({t for t in terms if any(_matches(t, name_key(h)) for h in have)}),
                              history == "made", -len(need))})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def from_community(conn: sqlite3.Connection, table_id: int, terms: list[str], limit: int = 6) -> list[dict]:
    """Published community recipes that use what's on hand."""
    staples = staples_of(conn, table_id)
    out = []
    for listing in community.ingredient_matches(conn, [w for t in terms for w in t.split()], limit=limit * 2):
        have, need = _missing([tuple(i) for i in listing["ingredients"]], terms, staples)
        if have:
            out.append({**listing, "have": have, "need": need[:6], "need_count": len(need),
                        "score": (len(have), -len(need))})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


# ---- Something new (the local model) ----

IDEAS_SCHEMA = {
    "type": "object",
    "properties": {"ideas": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "summary": {"type": "string"},
        "also_needs": {"type": "array", "items": {"type": "string"}}},
        "required": ["name", "summary"]}}},
    "required": ["ideas"],
}

IDEAS_PROMPT = """You suggest simple home dinners. The cook has these ingredients: {items}.
Suggest exactly 3 different dinners that mainly use them. For each, give a short name (under 6 words), one plain
sentence describing it, and up to 4 extra ingredients they'd probably need (not salt, pepper, or oil).
Respond only with JSON: {{"ideas": [{{"name": "...", "summary": "...", "also_needs": ["..."]}}]}}"""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "description": {"type": "string"},
        "servings": {"type": ["integer", "null"]}, "prep_minutes": {"type": ["integer", "null"]},
        "cook_minutes": {"type": ["integer", "null"]},
        "ingredients": {"type": "array", "items": {"type": "object", "properties": {
            "amount": {"type": "string"}, "name": {"type": "string"}}, "required": ["name"]}},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "ingredients", "steps"],
}

DRAFT_PROMPT = """Write a simple home recipe for "{name}"{using}. Keep it to at most 10 ingredients and 6 short steps.
Use common US measurements. Respond only with JSON: {{"name": "...", "description": "one sentence",
"servings": 4, "prep_minutes": 10, "cook_minutes": 20, "ingredients": [{{"amount": "1 cup", "name": "rice"}}],
"steps": ["..."]}}"""


def _json(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(match.group(0)) if match else {}
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _int(value, low: int, high: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def invent(ask, terms: list[str]) -> list[dict]:
    """Up to three new dinner ideas from the model, checked and trimmed. Raises AssistantUnavailable."""
    if not terms:
        return []
    data = _json(ask([{"role": "user", "content": IDEAS_PROMPT.format(items=", ".join(terms))}], IDEAS_SCHEMA))
    ideas = []
    for item in data.get("ideas") or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), 80)
        if not name or any(name.lower() == i["name"].lower() for i in ideas):
            continue
        needs = [_text(x, 40) for x in (item.get("also_needs") or []) if isinstance(x, str) and x.strip()][:4]
        ideas.append({"name": name, "summary": _text(item.get("summary"), 200), "also_needs": needs})
    return ideas[:3]


def draft_from_idea(ask, name: str, terms: list[str]) -> dict:
    """Form fields for a chosen AI idea, marked as AI-generated. Falls back to a bare draft offline."""
    name = _text(name, 80) or "New dinner idea"
    base = {"name": name, "category": "weeknight", "ai_generated": True,
            "ingredients": [{"amount": "", "name": t} for t in terms], "steps": [],
            "description": "", "note_author": "", "note_body": ""}
    using = f" using {', '.join(terms)}" if terms else ""
    try:
        data = _json(ask([{"role": "user", "content": DRAFT_PROMPT.format(name=name, using=using)}], DRAFT_SCHEMA))
    except AssistantUnavailable:
        return {**base, "offline": True}
    ingredients = []
    for item in data.get("ingredients") or []:
        if isinstance(item, dict) and _text(item.get("name"), 200):
            ingredients.append({"amount": _text(item.get("amount"), 50), "name": _text(item.get("name"), 200)})
    steps = [{"title": "", "body": _text(s, 2000)} for s in (data.get("steps") or []) if isinstance(s, str) and s.strip()]
    servings, prep, cook = _int(data.get("servings"), 1, 100), _int(data.get("prep_minutes"), 0, 1440), \
        _int(data.get("cook_minutes"), 0, 1440)
    return {**base, "name": _text(data.get("name"), 120) or name, "description": _text(data.get("description"), 600),
            "servings": str(servings or ""), "prep_minutes": str(prep if prep is not None else ""),
            "cook_minutes": str(cook if cook is not None else ""),
            "ingredients": ingredients[:40] or base["ingredients"], "steps": steps[:20]}
