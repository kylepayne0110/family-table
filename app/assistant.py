# The Cooking Assistant for one planned dinner.
#
# The app owns every calculation and every database write. Clear requests (unit
# conversions, "make this for eight", "how much flour do I need?", "from now on use X
# instead of Y") are understood here without the model, so they work even when Ollama is
# offline. Everything else goes to the local model with the open recipe in the prompt;
# the model may only *propose* an action, which the app checks before doing anything.

# json reads the model's structured reply.
import json

# re recognizes the requests the app handles itself.
import re

# dataclass describes a reply.
from dataclasses import dataclass, field

# Fraction carries exact quantities into conversions.
from fractions import Fraction

# Project modules.
from app.ingredients import _singular, normalize_name
from app.meals import LIMITS
from app.ollama import AssistantUnavailable
from app.scaling import clean_servings, conversion_unit, convert, parse_number, scale_ingredients, servings_factor


MAX_MESSAGE = 500
HISTORY_FOR_MODEL = 8

OFFLINE_TEXT = (
    "I can't reach the local cooking model right now (Ollama isn't answering). The recipe, the "
    "serving selector, and the shopping checklist still work, and I can still convert units, "
    "change servings, and tell you ingredient amounts."
)


@dataclass
class Reply:
    body: str
    kind: str = "text"            # text, clarify, servings, proposal, offline
    data: dict = field(default_factory=dict)
    servings: int | None = None   # set: change this dinner to this many servings (app applies it)
    reset_servings: bool = False
    recipe_yield: int | None = None  # set: record the saved recipe's missing yield first
    changes: list[dict] | None = None  # set: proposed permanent recipe changes (await Confirm)


# ---- Reading numbers and phrases ----

_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a dozen": 12, "dozen": 12, "fifteen": 15, "twenty": 20,
}
_COUNT = r"(\d{1,3}|a dozen|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
_QTY = (r"(?P<qty>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+|half an?|half|an?|one|two|three|four|five|six|seven|"
        r"eight|nine|ten|twelve)")
_NOT_PEOPLE = r"(?!\s*(?:minutes?|mins?|hours?|hrs?|seconds?|degrees|°|inch|inches|cups?|tbsp|tsp))"


def _count(text: str) -> int | None:
    text = text.strip().lower()
    return int(text) if text.isdigit() else _WORD_NUMBERS.get(text)


def _number_word(value) -> str:
    words = {v: k for k, v in _WORD_NUMBERS.items() if k not in ("a", "an", "a dozen", "dozen")}
    return words.get(value, "zzz") if isinstance(value, int) else "zzz"


def _qty(text: str) -> Fraction | None:
    text = (text or "").strip().lower()
    if text.startswith("half"):
        return Fraction(1, 2)
    if text in _WORD_NUMBERS:
        return Fraction(_WORD_NUMBERS[text])
    return parse_number(text)


def _clean(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split()).strip(" .!")


_QUESTION_START = re.compile(
    r"^(how|what|when|why|which|where|who|is|are|does|do|did|should|can i|could i|may i|would it|will it|"
    r"what if|how about|is it)\b")
_POLITE_REQUEST = re.compile(r"^(?:please\s+|ok(?:ay)?,?\s+|now,?\s+)?(?:can|could|would|will) you\b")
_PERMANENT = re.compile(
    r"\b(from now on|going forward|permanent(?:ly)?|for good|always|every time|in the future|"
    r"save (?:it|this|that|the change)|(?:update|change|edit) the (?:saved |original )?recipe|"
    r"in the (?:saved |original )?recipe|the (?:saved|original) recipe|for next time)\b")
_TONIGHT_ONLY = re.compile(r"\b(tonight|just this (?:time|once)|only this time|this time|for now|today|just for (?:this|now))\b")


def is_question(text: str) -> bool:
    text = _clean(text)
    if _POLITE_REQUEST.match(text):
        return False
    return bool(_QUESTION_START.match(text)) or text.endswith("?")


# ---- Unit conversion questions ----

_CONVERSION_PATTERNS = [
    re.compile(rf"^how many (?P<to>[a-z .]+?) (?:are |is )?(?:there )?in (?:{_QTY} )?(?P<from>[a-z .]+?)"
               r"(?: of (?P<ing>[a-z' -]+?))?$"),
    re.compile(rf"^how many (?P<to>[a-z .]+?) (?:is|are|does|do|makes?|equals?) {_QTY} (?P<from>[a-z .]+?)"
               r"(?: of (?P<ing>[a-z' -]+?))?(?: make| equal)?$"),
    re.compile(rf"^(?:convert |what is |what's |how much is )?{_QTY} (?P<from>[a-z .]+?)(?: of (?P<ing>[a-z' -]+?))?"
               r" (?:to|in|into|in to) (?P<to>[a-z .]+?)$"),
]


def parse_conversion(text: str):
    """'How many cups are in one US quart?' -> (1, 'quart', 'cup', ''); not a conversion -> None."""
    text = _clean(text).rstrip("?").strip()
    for pattern in _CONVERSION_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        to_unit, from_unit = conversion_unit(match["to"]), conversion_unit(match["from"])
        qty = _qty(match["qty"]) if match["qty"] else Fraction(1)
        if to_unit and from_unit and qty:
            return qty, from_unit, to_unit, (match["ing"] or "").strip()
    return None


# ---- Serving changes ----

_SERVINGS_PATTERNS = [
    re.compile(rf"\b(?:make|cook|scale|change|set|adjust|switch|bump|update|resize|do|prep|prepare)\b[^?]*?"
               rf"\b(?:for|to|into)\s+{_COUNT}\b{_NOT_PEOPLE}"),
    re.compile(rf"\b(?:we(?:'re| are)|i(?:'m| am))\s+(?:now\s+)?(?:cooking|feeding|making (?:it|this|dinner))\s+for\s+{_COUNT}\b"),
    re.compile(rf"\b(?:there(?:'ll| will) be|we(?:'ll| will) have|we have)\s+{_COUNT}\b(?:\s+(?:of us|people|guests))"),
    re.compile(rf"^(?:serves?|feed)\s+{_COUNT}\b"),
    re.compile(rf"^{_COUNT}\s+(?:servings|people|portions)$"),
]


def parse_servings_request(text: str, current: int | None) -> int | str | None:
    """A requested serving count, 'reset', or None if this isn't a request to change servings."""
    text = _clean(text)
    if is_question(text):
        return None
    if re.search(r"\b(?:reset|go back|back)\b(?: it| this)?(?: to)? (?:the )?(?:original|recipe|saved|normal|default)\b", text) \
            or re.fullmatch(r"reset(?: (?:it|this|servings|the servings))?", text):
        return "reset"
    if current and re.search(r"\b(double|twice)\b", text) and re.search(r"\b(it|this|recipe|dinner|batch|that)\b", text):
        return current * 2
    if current and re.search(r"\b(halve|half)\b", text) and re.search(r"\b(it|this|recipe|dinner|batch)\b", text):
        return max(1, -(-current // 2))
    for pattern in _SERVINGS_PATTERNS:
        match = pattern.search(text)
        if match:
            return _count(match.group(1))
    return None


# ---- Permanent edits (word substitutions) ----

_SUBSTITUTION_PATTERNS = [
    re.compile(r"\b(?:use|using|try)\s+(?P<new>.+?)\s+instead\s+of\s+(?P<old>.+)$"),
    re.compile(r"\binstead\s+of\s+(?P<old>.+?),?\s+(?:use|using)\s+(?P<new>.+)$"),
    re.compile(r"\b(?:replace|swap|switch)\s+(?:out\s+)?(?P<old>.+?)\s+(?:with|for)\s+(?P<new>.+)$"),
    re.compile(r"\bsubstitute\s+(?P<new>.+?)\s+for\s+(?P<old>.+)$"),
]
_FILLER_WORDS = {"the", "my", "our", "regular", "normal", "plain", "usual", "ordinary", "standard", "some", "any"}


def _strip_phrase(text: str) -> str:
    text = re.sub(_PERMANENT, " ", text)
    text = re.sub(r"\b(tonight|please|in this recipe|in it|in there)\b", " ", text)
    return " ".join(text.split()).strip(" ,.;:!?")


def parse_substitution(text: str) -> tuple[str, str] | None:
    """'From now on, use panko instead of regular breadcrumbs' -> ('regular breadcrumbs', 'panko')."""
    cleaned = _clean(text).rstrip("?")
    for pattern in _SUBSTITUTION_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            old, new = _strip_phrase(match["old"]), _strip_phrase(match["new"])
            if old and new and old != new and not re.search(r"\d", old + new):
                return old, new
    return None


def _phrase_pattern(words: list[str]) -> re.Pattern:
    last = _singular(words[-1])
    parts = [re.escape(w) for w in words[:-1]] + [re.escape(last) + r"(?:e?s)?"]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.I)


def _replace(text: str, pattern: re.Pattern, new: str) -> str:
    def swap(match: re.Match) -> str:
        return new[:1].upper() + new[1:] if match.group(0)[:1].isupper() else new
    return pattern.sub(swap, text)


def substitution_changes(recipe: dict, old: str, new: str) -> list[dict]:
    """The exact changes to the saved recipe for 'use NEW instead of OLD'.

    Only names and instruction text change. Amounts and the serving count stay as saved,
    so a dinner scaled for tonight never changes the recipe's baseline.
    """
    words = old.split()
    tries = [words, [w for w in words if w not in _FILLER_WORDS] or words, words[-1:]]
    for attempt in tries:
        pattern = _phrase_pattern(attempt)
        changes = []
        for ing in recipe["ingredients"]:
            after = _replace(ing["name"], pattern, new)
            if after != ing["name"]:
                changes.append({"kind": "ingredient", "id": ing["id"], "amount": ing["amount"],
                                "before": ing["name"], "after": after})
        for number, step in enumerate(recipe["steps"], start=1):
            for part in ("title", "body"):
                after = _replace(step[part], pattern, new)
                if after != step[part]:
                    changes.append({"kind": f"step_{part}", "id": step["id"], "step": number,
                                    "before": step[part], "after": after})
        if changes:
            return changes
    return []


def proposal_reply(recipe: dict, old: str, new: str) -> Reply:
    changes = substitution_changes(recipe, old, new)
    if not changes:
        return Reply(f"I couldn't find {old} in the saved recipe, so there's nothing to change.")
    limits = {"ingredient": LIMITS["ingredient"], "step_title": LIMITS["step_title"], "step_body": LIMITS["step_body"]}
    if any(len(c["after"]) > limits[c["kind"]] for c in changes):
        return Reply("That change would make part of the recipe too long to save. Edit the recipe instead.")
    yield_note = f" The saved recipe still makes {recipe['servings']} servings." if recipe.get("servings") else ""
    return Reply(f"Here's what I'd change in the saved recipe. Nothing is saved until you confirm.{yield_note}",
                 kind="proposal", changes=changes, data={"old": old, "new": new})


# ---- Permanent edits (amounts) ----

_AMOUNT_WORDS = {
    "a": "1", "an": "1", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "half": "1/2", "a half": "1/2", "one half": "1/2", "a quarter": "1/4", "one quarter": "1/4", "quarter": "1/4",
    "three quarters": "3/4", "three quarter": "3/4", "three fourths": "3/4", "a third": "1/3", "one third": "1/3",
    "two thirds": "2/3", "an eighth": "1/8", "one eighth": "1/8",
}
_UNIT_WORDS = (r"cups?|tablespoons?|tbsps?|tbs|teaspoons?|tsps?|pounds?|lbs?|ounces?|oz|grams?|g|kilograms?|kg|"
               r"milliliters?|ml|liters?|cloves?|cans?|pinch(?:es)?|dash(?:es)?|sticks?|slices?|pieces?|quarts?|pints?")
_SPOKEN_NUM = (r"(?:\d+|one|two|three|four|five|six)\s+and\s+(?:a\s+)?(?:half|quarter|third)|"
               r"\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+|\d*[½⅓⅔¼¾⅛]|"
               + "|".join(sorted((re.escape(w) for w in _AMOUNT_WORDS), key=len, reverse=True)))
_AMOUNT_RE = rf"(?P<num>{_SPOKEN_NUM})(?:\s+of)?(?:\s+(?:an?\s+)?(?P<unit>{_UNIT_WORDS}))?"
_AMOUNT_EDIT_PATTERNS = [
    re.compile(rf"\b(?:use|using|put in|add|make it|it should be|it needs)\s+(?:only\s+|just\s+)?{_AMOUNT_RE}\s+"
               rf"(?:of\s+)?(?:the\s+)?(?P<ing>[a-z][a-z' -]*?)(?=\s+(?:in|for|from|going|instead|next|permanently|"
               rf"every|always|not)\b|$)"),
    re.compile(rf"\b(?:change|set|make|update)\s+(?:the\s+)?(?P<ing>[a-z][a-z' -]*?)\s+(?:amount\s+)?to\s+{_AMOUNT_RE}\b"),
]
_FRACTION_GLYPHS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8"}


def spoken_amount(num: str, unit: str | None) -> str | None:
    """'three quarters', 'cup' -> '3/4 cup'; '1 and a half', 'cups' -> '1 1/2 cups'."""
    num = num.strip()
    for glyph, frac in _FRACTION_GLYPHS.items():
        num = re.sub(rf"(\d){glyph}", rf"\1 {frac}", num).replace(glyph, frac)
    combo = re.fullmatch(r"(\d+|one|two|three|four|five|six)\s+and\s+(?:a\s+)?(half|quarter|third)", num)
    if combo:
        whole = combo.group(1) if combo.group(1).isdigit() else _AMOUNT_WORDS[combo.group(1)]
        num = f"{whole} {_AMOUNT_WORDS['a ' + combo.group(2)]}"
    num = _AMOUNT_WORDS.get(num, num)
    if parse_number(num) is None or parse_number(num) <= 0:
        return None
    if not unit:
        return num
    unit = unit.strip()
    value = parse_number(num)
    singular = {"cups": "cup", "tablespoons": "tablespoon", "teaspoons": "teaspoon", "pounds": "pound", "ounces": "ounce",
                "cloves": "clove", "cans": "can", "grams": "gram", "slices": "slice", "pieces": "piece", "sticks": "stick",
                "quarts": "quart", "pints": "pint", "liters": "liter", "milliliters": "milliliter"}.get(unit, unit)
    plural = {v: k for k, v in {"cups": "cup", "tablespoons": "tablespoon", "teaspoons": "teaspoon", "pounds": "pound",
                                 "ounces": "ounce", "cloves": "clove", "cans": "can", "grams": "gram",
                                 "slices": "slice", "pieces": "piece", "sticks": "stick", "quarts": "quart",
                                 "pints": "pint", "liters": "liter", "milliliters": "milliliter"}.items()}
    word = plural.get(singular, singular) if value > 1 else singular
    return f"{num} {word}"


def parse_amount_edit(text: str) -> tuple[str, str] | None:
    """'Use ¾ cup of flour in the original recipe' -> ('flour', '3/4 cup'); otherwise None."""
    cleaned = _clean(text).rstrip("?")
    for pattern in _AMOUNT_EDIT_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        amount = spoken_amount(match["num"], match["unit"])
        ingredient = _strip_phrase(match["ing"]).strip()
        ingredient = re.sub(r"^(?:of|the)\s+", "", ingredient)
        if amount and ingredient and len(ingredient) <= 60:
            return ingredient, amount
    return None


def _recipe_line(recipe: dict, name: str) -> list:
    """Saved-recipe ingredients that a spoken name refers to (best matches only)."""
    lines = [{"name": i["name"], "amount": i["amount"], "id": i["id"], "row": i} for i in recipe["ingredients"]]
    return [line["row"] for line in _mentioned_lines(name, lines)]


def amount_changes(recipe: dict, ingredient, new_amount: str) -> list[dict]:
    """Changes to the saved recipe for a new amount: the ingredient line, plus any step that quotes the old amount."""
    old = ingredient["amount"]
    changes = [{"kind": "amount", "id": ingredient["id"], "name": ingredient["name"], "before": old, "after": new_amount}]
    if old and old != new_amount:
        noun = re.escape(normalize_name(ingredient["name"])[0].split()[-1] if normalize_name(ingredient["name"])[0] else "")
        if noun:
            pattern = re.compile(rf"\b{re.escape(old)}(\s+(?:of\s+)?(?:[a-z-]+\s+){{0,3}}{noun}(?:e?s)?)\b", re.I)
            for number, step in enumerate(recipe["steps"], start=1):
                after = pattern.sub(lambda m: new_amount + m.group(1), step["body"])
                if after != step["body"]:
                    changes.append({"kind": "step_body", "id": step["id"], "step": number, "before": step["body"],
                                    "after": after})
    return changes


def amount_proposal(meal: dict, recipe: dict, ingredient, new_amount: str) -> Reply:
    """A proposed permanent amount change, measured against the saved recipe's own yield."""
    if new_amount == ingredient["amount"]:
        return Reply(f"The saved recipe already uses {new_amount} {ingredient['name']}.")
    if len(new_amount) > LIMITS["amount"]:
        return Reply("That amount is too long to save. Edit the recipe instead.")
    changes = amount_changes(recipe, ingredient, new_amount)
    made = recipe.get("servings")
    factor = servings_factor(meal.get("servings"), made)
    tonight = ""
    if factor != 1 and made:
        from app.scaling import scale_amount
        tonight = f" Tonight's {meal['servings']} servings will show {scale_amount(new_amount, factor)}."
    base = f" (it makes {made} servings)" if made else ""
    return Reply(f"Here's the change for the saved recipe{base}: {ingredient['name']}, {ingredient['amount'] or 'no amount'} "
                 f"becomes {new_amount}. Nothing is saved until you confirm.{tonight}",
                 kind="proposal", changes=changes, data={"ingredient": ingredient["name"], "after": new_amount})


def amount_edit_reply(meal: dict, recipe: dict, text: str, ingredient_name: str, new_amount: str) -> Reply:
    found = _recipe_line(recipe, ingredient_name)
    if not found:
        return Reply(f"I couldn't find {ingredient_name} in the saved recipe, so there's nothing to change.")
    if len(found) > 1:
        names = " or ".join(f["name"] for f in found[:3])
        return Reply(f"Which one do you mean: {names}?", kind="clarify",
                     data={"pending": {"type": "amount_which", "after": new_amount,
                                       "options": [{"id": f["id"], "name": f["name"]} for f in found[:3]]}})
    ingredient = found[0]
    cleaned = _clean(text)
    factor = servings_factor(meal.get("servings"), recipe.get("servings"))
    says_original = re.search(r"\b(original|saved|as written)\b", cleaned)
    if not _PERMANENT.search(cleaned):
        return Reply("Do you want me to change the saved recipe from now on, or is that just for tonight?",
                     kind="clarify", data={"pending": {"type": "amount", "id": ingredient["id"], "after": new_amount}})
    if factor != 1 and not says_original:
        return Reply(f"Is {new_amount} for the original recipe ({recipe['servings']} servings), or for tonight's "
                     f"{meal['servings']}?", kind="clarify",
                     data={"pending": {"type": "amount_basis", "id": ingredient["id"], "after": new_amount}})
    return amount_proposal(meal, recipe, ingredient, new_amount)


def _ingredient_by_id(recipe: dict, ingredient_id):
    return next((i for i in recipe["ingredients"] if i["id"] == ingredient_id), None)


# Common swaps, so "from now on, use panko" knows what panko replaces.
_SWAPS = {
    "panko": ["breadcrumbs", "bread crumbs", "crumbs"], "breadcrumbs": ["panko"], "bread crumbs": ["panko"],
    "greek yogurt": ["sour cream", "mayonnaise"], "sour cream": ["greek yogurt"], "margarine": ["butter"],
    "butter": ["margarine", "oil"], "olive oil": ["vegetable oil", "canola oil", "oil"], "avocado oil": ["oil"],
    "brown rice": ["white rice", "rice"], "turkey": ["beef"], "ground turkey": ["ground beef"],
    "honey": ["sugar", "maple syrup"], "maple syrup": ["honey", "sugar"], "almond milk": ["milk"], "oat milk": ["milk"],
    "coconut milk": ["cream", "milk"], "half and half": ["cream", "milk"], "chicken thighs": ["chicken breasts"],
    "chicken breasts": ["chicken thighs"], "shallots": ["onion"], "kale": ["spinach"], "spinach": ["kale"],
}


def implied_substitution(recipe: dict, text: str) -> tuple[str, str] | None:
    """'From now on, use panko' -> ('breadcrumbs', 'panko') when the recipe has exactly one likely match."""
    cleaned = _clean(text).rstrip("?")
    match = re.search(r"\b(?:use|using|switch to|go with)\s+(?P<new>[a-z][a-z' -]{1,40}?)(?:\s+(?:from now on|going forward|"
                      r"permanently|always|every time|in the (?:saved |original )?recipe|for next time))?$", cleaned)
    if not match or not _PERMANENT.search(cleaned) or re.search(r"\d", cleaned):
        return None
    new = _strip_phrase(match["new"])
    if not new or any(new in normalize_name(i["name"])[0] for i in recipe["ingredients"]):
        return None
    candidates = list(_SWAPS.get(new, []))
    head = new.split()[-1]
    candidates += [i["name"] for i in recipe["ingredients"] if head in normalize_name(i["name"])[0].split()]
    found = []
    for candidate in candidates:
        for ing in recipe["ingredients"]:
            key = normalize_name(ing["name"])[0]
            if candidate in key or key in candidate:
                old = candidate if candidate in key else key
                if old not in [f for f in found]:
                    found.append(old)
    found = list(dict.fromkeys(found))
    return (found[0], new) if len(found) == 1 else None


# ---- Ingredient amount questions ----

_NOT_NAMES = {"fresh", "ground", "white", "black", "red", "green", "yellow", "large", "small", "medium", "dried",
              "boneless", "skinless", "whole", "light", "dark", "extra", "virgin", "unsalted", "salted", "plain",
              "of", "and", "or", "for", "the", "a"}


def _mentioned_lines(text: str, lines: list[dict]) -> list[dict]:
    """Ingredients the question names: the full name first, then the main word, then any telling word."""
    lowered = text.lower()
    words = {_singular(w) for w in re.findall(r"[a-z]+", lowered)}
    full, head, partial = [], [], []
    for line in lines:
        key = normalize_name(line["name"])[0]
        if not key:
            continue
        key_words = key.split()
        if re.search(rf"\b{re.escape(key)}s?\b", lowered):
            full.append(line)
        elif key_words[-1] in words:
            head.append(line)
        elif any(w in words for w in key_words if w not in _NOT_NAMES and len(w) > 2):
            partial.append(line)
    return full or head or partial


def _amount_text(line: dict) -> str:
    return f"{line['amount']} {line['name']}".strip() if line["amount"] else f"{line['name']} (no exact amount given)"


def quantity_reply(text: str, meal: dict, recipe: dict, lines: list[dict]) -> Reply | None:
    """'How much flour do I need tonight?' answered from this dinner's scaled amounts."""
    cleaned = _clean(text)
    if not re.search(r"\bhow (?:much|many)\b|\bwhat amount\b|\bquantity of\b", cleaned):
        return None
    found = _mentioned_lines(cleaned, lines)
    if not found:
        return None
    servings = meal["servings"]
    other = re.search(rf"\bfor\s+{_COUNT}\b{_NOT_PEOPLE}", cleaned)
    asked = _count(other.group(1)) if other else None
    if asked and recipe.get("servings") and asked != servings:
        hypothetical = scale_ingredients(
            [{"amount": l["original_amount"], "name": l["original_name"]} for l in found],
            servings_factor(asked, recipe["servings"]))
        amounts = "; ".join(_amount_text(l) for l in hypothetical)
        return Reply(f"For {asked} servings you'd need {amounts}. Tonight's dinner is set for {servings}; "
                     f"say \"make this for {asked}\" if you want to change it.")
    amounts = "; ".join(_amount_text(l) for l in found)
    serving_text = f"tonight's {servings} servings" if servings else "this recipe as written"
    return Reply(f"For {serving_text}: {amounts}.")


# ---- The model ----

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "action": {"type": "string", "enum": ["none", "set_servings", "propose_edit", "clarify"]},
        "servings": {"type": ["integer", "null"]},
        "replacements": {"type": "array", "items": {
            "type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
            "required": ["old", "new"]}},
    },
    "required": ["reply", "action"],
}


def recipe_context(meal: dict, recipe: dict, lines: list[dict], steps: list[str]) -> str:
    """The open recipe, exactly as the cook sees it tonight, for the model's prompt."""
    made = f"{recipe['servings']} servings" if recipe.get("servings") else "an unknown number of servings"
    out = [f"RECIPE: {recipe['name']}", f"The saved recipe makes {made}."]
    if meal["servings"]:
        out.append(f"Tonight's dinner is for {meal['servings']} servings.")
    out.append("INGREDIENTS FOR TONIGHT (already scaled; use these amounts exactly):")
    out += [f"- {_amount_text(l)}" for l in lines] or ["- (none listed)"]
    out.append("INSTRUCTIONS FOR TONIGHT:")
    out += [f"{i}. {body}" for i, body in enumerate(steps, start=1)] or ["(none written down)"]
    return "\n".join(out)


SYSTEM_PROMPT = """You are the Cooking Assistant in Family Table, helping a home cook make one recipe tonight.
Answer from the recipe below and general cooking knowledge. Keep answers short (1 to 3 sentences), plain text, no markdown.
Ingredient amounts below are already scaled for tonight. Quote them exactly and never do your own scaling arithmetic.
Respond with JSON: {"reply": "...", "action": "none" | "set_servings" | "propose_edit" | "clarify", "servings": integer or null, "replacements": [{"old": "...", "new": "..."}]}
- "set_servings": the cook wants tonight's dinner made for a different number of people. Put the number in "servings".
- "propose_edit": the cook clearly wants the SAVED recipe changed permanently (for example "from now on"). Put word swaps in "replacements". Never claim the recipe was changed; the app asks the cook to confirm.
- "clarify": it is unclear whether the cook is asking a question or wants a permanent recipe change. Ask one short question in "reply".
- "none": answer the question in "reply".

"""


def _model_reply(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(match.group(0)) if match else {"reply": raw, "action": "none"}
        except ValueError:
            data = {"reply": raw, "action": "none"}
    return data if isinstance(data, dict) else {"reply": str(data), "action": "none"}


def ask_model(ask, meal: dict, recipe: dict, lines: list[dict], steps: list[str],
              history: list[dict], text: str, extra: str = "") -> Reply:
    messages = [{"role": "system", "content": SYSTEM_PROMPT + recipe_context(meal, recipe, lines, steps) + extra}]
    for message in history[-HISTORY_FOR_MODEL:]:
        messages.append({"role": message["role"], "content": message["body"]})
    messages.append({"role": "user", "content": text})
    try:
        data = _model_reply(ask(messages, REPLY_SCHEMA))
    except AssistantUnavailable:
        return Reply(OFFLINE_TEXT, kind="offline")

    reply = str(data.get("reply") or "").strip()[:2000]
    action = data.get("action")
    if action == "set_servings":
        servings = clean_servings(data.get("servings"))
        if servings:
            return _servings_reply(meal, recipe, servings)
    if action == "propose_edit":
        for swap in data.get("replacements") or []:
            if isinstance(swap, dict) and swap.get("old") and swap.get("new"):
                proposal = proposal_reply(recipe, str(swap["old"]).strip()[:80], str(swap["new"]).strip()[:80])
                if proposal.kind == "proposal":
                    return proposal
        return Reply(reply or "I couldn't work out which words to change in the saved recipe. "
                              "Try \"from now on, use X instead of Y\".")
    if action == "clarify" and reply:
        return Reply(reply, kind="clarify")
    return Reply(reply or "Sorry, I didn't catch that. Could you ask another way?")


# ---- Putting it together ----

def _servings_reply(meal: dict, recipe: dict, servings: int | str) -> Reply:
    if not recipe.get("servings"):
        wanted = servings if isinstance(servings, int) else None
        return Reply("This recipe doesn't say how many servings it makes, so I can't scale it accurately yet. "
                     "How many servings does it make as written?",
                     kind="clarify", data={"pending": {"type": "yield", "servings": wanted}})
    if servings == "reset":
        return Reply(f"Back to the recipe's {recipe['servings']} servings. The amounts above are updated.",
                     kind="servings", reset_servings=True)
    return Reply(f"Done: tonight's dinner is now set for {servings} servings (the saved recipe makes "
                 f"{recipe['servings']}). The amounts above are updated. The saved recipe and shopping list "
                 "haven't changed.", kind="servings", servings=servings)


def _answer_pending(pending: dict, text: str, meal: dict, recipe: dict) -> Reply | None:
    """A reply to the assistant's own clarifying question."""
    cleaned = _clean(text)
    if pending.get("type") == "substitution":
        old, new = pending["old"], pending["new"]
        if _PERMANENT.search(cleaned) or re.search(r"\b(yes|yeah|yep|save|saved|permanent|recipe|update|change it|both)\b", cleaned):
            return proposal_reply(recipe, old, new)
        if _TONIGHT_ONLY.search(cleaned) or re.search(r"\b(no|nope|just|only|question|asking)\b", cleaned):
            return Reply(f"Got it. Tonight, use {new} in place of {old}. The saved recipe stays the same.")
        return None
    if pending.get("type") in ("amount", "amount_basis"):
        ingredient = _ingredient_by_id(recipe, pending.get("id"))
        if ingredient is None:
            return Reply("The recipe changed since I asked, so I didn't change anything.")
        after = pending.get("after", "")
        if pending["type"] == "amount":
            if _PERMANENT.search(cleaned) or re.search(r"\b(yes|yeah|yep|save|saved|recipe|permanent|from now on|always)\b", cleaned):
                factor = servings_factor(meal.get("servings"), recipe.get("servings"))
                if factor != 1:
                    return Reply(f"Is {after} for the original recipe ({recipe['servings']} servings), or for tonight's "
                                 f"{meal['servings']}?", kind="clarify",
                                 data={"pending": {"type": "amount_basis", "id": ingredient["id"], "after": after}})
                return amount_proposal(meal, recipe, ingredient, after)
            if _TONIGHT_ONLY.search(cleaned) or re.search(r"\b(no|nope|just|only)\b", cleaned):
                return Reply(f"Got it. Tonight, use {after} {ingredient['name']}. The saved recipe stays the same.")
            return None
        tonight = re.search(rf"\b(tonight|today|this time|{meal.get('servings') or 'x'}|"
                            rf"{_number_word(meal.get('servings'))})\b", cleaned)
        original = re.search(rf"\b(original|saved|recipe|as written|{recipe.get('servings') or 'x'}|"
                             rf"{_number_word(recipe.get('servings'))})\b", cleaned)
        if tonight and not original:
            from app.scaling import scale_amount
            factor = servings_factor(meal.get("servings"), recipe.get("servings"))
            base = scale_amount(after, 1 / factor) if factor else after
            return amount_proposal(meal, recipe, ingredient, base)
        if original:
            return amount_proposal(meal, recipe, ingredient, after)
        return None
    if pending.get("type") == "amount_which":
        for option in pending.get("options", []):
            if any(w in cleaned for w in normalize_name(option["name"])[0].split() if len(w) > 2):
                ingredient = _ingredient_by_id(recipe, option["id"])
                if ingredient:
                    return amount_edit_reply(meal, recipe, "from now on in the saved recipe", ingredient["name"],
                                             pending.get("after", ""))
        return None
    if pending.get("type") == "swap_target":
        target = _strip_phrase(re.sub(r"^(?:the|it replaces|replace|instead of)\s+", "", cleaned))
        if target:
            return proposal_reply(recipe, target, pending.get("new", ""))
        return None
    if pending.get("type") == "yield":
        match = re.search(_COUNT, cleaned)
        made = clean_servings(_count(match.group(1))) if match else None
        if made:
            target = pending.get("servings") or made
            reply = Reply(f"Thanks: the recipe makes {made} servings as written, so I saved that. "
                          + (f"Tonight's dinner is now set for {target} servings. The amounts above are updated."
                             if target != made else "Amounts are shown for that many servings."),
                          kind="servings", recipe_yield=made, servings=target if target != made else None)
            return reply
    return None


def respond(meal: dict, recipe: dict, lines: list[dict], steps: list[str], history: list[dict], text: str,
            ask) -> Reply:
    """Work out the reply (and any action) for one message about this planned dinner."""
    return local_reply(meal, recipe, lines, history, text) or ask_model(ask, meal, recipe, lines, steps, history, text)


def local_reply(meal: dict, recipe: dict, lines: list[dict], history: list[dict], text: str) -> Reply | None:
    """Everything the app answers itself, without the model (works offline). None means "ask the model"."""
    last = history[-1] if history else None
    if last and last["role"] == "assistant" and last["kind"] == "clarify":
        pending = json.loads(last["data_json"] or "{}").get("pending")
        if pending:
            answered = _answer_pending(pending, text, meal, recipe)
            if answered:
                return answered

    conversion = parse_conversion(text)
    if conversion:
        qty, from_unit, to_unit, ingredient = conversion
        return Reply(convert(qty, from_unit, to_unit, ingredient).text)

    servings = parse_servings_request(text, meal["servings"])
    if servings is not None:
        if isinstance(servings, int) and not clean_servings(servings):
            return Reply("I can set a dinner for 1 to 100 servings.")
        return _servings_reply(meal, recipe, servings)

    amount_edit = parse_amount_edit(text)
    if amount_edit:
        return amount_edit_reply(meal, recipe, text, *amount_edit)

    substitution = parse_substitution(text) or implied_substitution(recipe, text)
    if substitution is None and _PERMANENT.search(_clean(text)):
        bare = re.search(r"\b(?:use|using|switch to)\s+(?P<new>[a-z][a-z' -]{1,40})$", _strip_phrase(_clean(text)))
        if bare and not re.search(r"\d", text):
            new = _strip_phrase(bare["new"])
            return Reply(f"Which ingredient should {new} replace in the saved recipe?", kind="clarify",
                         data={"pending": {"type": "swap_target", "new": new}})
    if substitution:
        old, new = substitution
        cleaned = _clean(text)
        if _PERMANENT.search(cleaned):
            return proposal_reply(recipe, old, new)
        if _TONIGHT_ONLY.search(cleaned):
            return Reply(f"Sure. Tonight, use {new} in place of {old}. The saved recipe stays the same.")
        if not is_question(text):
            return Reply("Do you want me to change the saved recipe from now on, or is this just for tonight?",
                         kind="clarify", data={"pending": {"type": "substitution", "old": old, "new": new}})

    answer = quantity_reply(text, meal, recipe, lines)
    if answer:
        return answer
    return None
