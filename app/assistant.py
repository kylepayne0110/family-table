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
    r"save (?:it|this|that|the change)|(?:update|change|edit) the (?:saved )?recipe|in the (?:saved )?recipe|"
    r"the saved recipe|for next time)\b")
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
              history: list[dict], text: str) -> Reply:
    messages = [{"role": "system", "content": SYSTEM_PROMPT + recipe_context(meal, recipe, lines, steps)}]
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

    substitution = parse_substitution(text)
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

    return ask_model(ask, meal, recipe, lines, steps, history, text)
