# The voice helper on the Add/Edit recipe form.
#
# The form on the screen is the source of truth. Each request carries a snapshot of it plus
# what the person just said; the answer is a list of small, validated edits ("ops") that the
# page applies to the visible form (and can undo). Nothing here saves a recipe: only the
# form's own Save button does, after the person has looked it over.
#
# Understanding speech:
#   1. Clear patterns are handled here, without the model ("These are Grandma's chicken
#      nuggets. They feed four. I use two chicken breasts, cut them into chunks...").
#   2. Anything else may go to the local model, whose suggested edits are checked strictly
#      against the fields that exist and the rows on the form.
# Questions come only after the person has finished talking, one at a time, and never for
# something already on the form or something they chose to skip.

# json reads the model's answer; re reads speech.
import json
import re

# dataclass describes a turn's result.
from dataclasses import dataclass, field

# Project modules.
from app.assistant import _AMOUNT_WORDS, _SPOKEN_NUM, _UNIT_WORDS, spoken_amount
from app.config import CATEGORIES
from app.ingredients import name_key
from app.meals import LIMITS, MAX_INGREDIENTS, MAX_STEPS
from app.ollama import AssistantUnavailable

SCALAR_FIELDS = {
    "name": LIMITS["name"], "description": LIMITS["description"], "cuisine": LIMITS["cuisine"],
    "creator_credit": LIMITS["creator_credit"], "card_tag": LIMITS["card_tag"], "note_body": LIMITS["note_body"],
    "note_author": LIMITS["note_author"], "category": 20, "prep_minutes": 4, "cook_minutes": 4, "servings": 3,
}
NUMBER_FIELDS = {"prep_minutes": (0, 1440), "cook_minutes": (0, 1440), "servings": (1, 100)}
MAX_TEXT = 1500

DONE = re.compile(r"^(?:that's it|that is it|that's all|that is all|i'm done|i am done|done|that's the recipe|"
                  r"that's everything|finished|all done|nothing else|no(?:,)? that's it|that's about it)$")
CHATTER = re.compile(r"^(?:ok(?:ay)?|cool|great|good|nice|perfect|thanks|thank you|sounds good|that sounds good|"
                     r"looks good|that looks good|awesome|yes|yeah|yep|sure|alright|all right|got it|hmm+|um+|uh+)$")
SKIP = re.compile(r"^(?:skip(?: it| that| this)?|pass|no idea|i don't know|not sure|leave it(?: blank| empty)?|"
                  r"no thanks|nah|none|no notes?|nothing|next question|move on)$")
UNDO = re.compile(r"^(?:undo(?: that)?|scratch that|take that back|go back|never ?mind(?: that)?|oops,? undo)$")
INVENT = re.compile(r"\b(?:invent|come up with|make up|create) (?:a |something|some|new|an?)\b|\bhelp me (?:make|create|think)|"
                    r"\bwhat (?:can|could|should) (?:i|we) make\b|\bany ideas\b|\bsuggest (?:a|some|something)\b")

STEP_VERBS = (r"cut|chop|dice|mince|slice|mix|stir|whisk|combine|coat|dip|dredge|bread|bake|fry|deep fry|pan fry|cook|"
              r"boil|simmer|heat|preheat|warm|add|pour|season|sprinkle|toss|marinate|roll|place|put|lay|arrange|serve|let|"
              r"grill|roast|saute|sauté|melt|brown|sear|drain|rinse|blend|puree|knead|spread|layer|cover|top|flip|turn|"
              r"remove|take|transfer|whip|beat|fold|garnish|chill|refrigerate|freeze|rest|squeeze|peel|grate|shred|"
              r"wrap|stuff|fill|pound|press|strain|bring|reduce|thicken|microwave|air fry|broil|steam|soak|scoop|shape|form")
_CLAUSE_STEP = re.compile(rf"^(?:and\s+)?(?:then\s+|first\s+|next\s+|after that\s+|finally\s+|now\s+)?(?:i|we|you)?\s*"
                          rf"(?:then\s+)?(?:just\s+)?(?P<verb>{STEP_VERBS})(?:s|es|ed)?\b(?P<rest>.*)$")
_NOT_FOOD = {"chunks", "pieces", "bits", "strips", "cubes", "slices", "half", "halves", "quarters", "bowl", "pan", "pot",
             "oven", "skillet", "tray", "sheet", "dish", "plate", "fridge", "freezer", "minutes", "minute", "hours",
             "hour", "water bath", "the oven", "it", "them", "that", "this", "a bowl", "a pan", "the pan", "the bowl",
             "batches", "half an hour", "the fridge", "foil", "parchment", "a baking sheet", "a sheet pan", "heat",
             "medium heat", "high heat", "low heat", "the middle", "the side", "salt water", "people", "persons",
             "servings", "portions", "guests", "kids", "adults", "of us", "family", "the family", "the whole family"}
_COUNTABLE = re.compile(r"\b(breasts?|thighs?|eggs?|onions?|cloves?|tortillas?|potatoes|potato|carrots?|lemons?|limes?|"
                        r"apples?|bananas?|tomatoes|tomato|peppers?|buns?|slices?|cans?|links?|sausages?|fillets?|chops?|"
                        r"wings?|drumsticks?|shallots?|jalape[nñ]os?|avocados?|zucchinis?|cucumbers?|leaves|leaf|"
                        r"stalks?|sprigs?|heads?|bunch(?:es)?|packages?|packets?|sticks?|bagels?|rolls?|pitas?|"
                        r"shrimp|scallops?|cookies?|crackers?|strips?|patties|patty)\b")
_TO_TASTE = re.compile(r"\b(?:to taste|don't measure|do not measure|never measure|just eyeball|eyeball it|a little|"
                       r"a bit|some|a pinch|a dash|as much as you like|however much)\b")


@dataclass
class VoiceResult:
    ops: list[dict] = field(default_factory=list)
    say: str = ""                        # spoken (and shown) reply; empty while the person is still talking
    voice: dict = field(default_factory=dict)
    ask: str = ""                        # the field a question is about (the page highlights it)
    done: bool = False


# ---- The form snapshot ----

def clean_form(raw: dict) -> dict:
    """The snapshot the page sent, kept to known fields and the form's own limits."""
    form = {k: str(raw.get(k) or "")[:limit] for k, limit in SCALAR_FIELDS.items()}
    form["ingredients"] = [{"amount": str(i.get("amount") or "")[:LIMITS["amount"]],
                            "name": str(i.get("name") or "")[:LIMITS["ingredient"]]}
                           for i in (raw.get("ingredients") or [])[:MAX_INGREDIENTS] if isinstance(i, dict)]
    form["steps"] = [{"title": str(s.get("title") or "")[:LIMITS["step_title"]], "body": str(s.get("body") or "")[:LIMITS["step_body"]]}
                     for s in (raw.get("steps") or [])[:MAX_STEPS] if isinstance(s, dict)]
    form["editing"] = bool(raw.get("editing"))
    return form


def clean_voice(raw: dict) -> dict:
    """The helper's own notes (what was asked, skipped, the pending question). Small and validated."""
    raw = raw if isinstance(raw, dict) else {}
    out = {
        "asked": [str(x)[:40] for x in (raw.get("asked") or [])][:40],
        "skipped": [str(x)[:40] for x in (raw.get("skipped") or [])][:40],
        "pending": raw.get("pending") if isinstance(raw.get("pending"), dict) else None,
        "last_ingredient": str(raw.get("last_ingredient") or "")[:200],
        "ideas": [str(x)[:80] for x in (raw.get("ideas") or [])][:3],
        "mode": raw.get("mode") if raw.get("mode") in ("document", "invent") else "document",
    }
    if out["pending"]:
        out["pending"] = {k: (str(v)[:200] if not isinstance(v, int) else v) for k, v in out["pending"].items()
                          if k in ("field", "index", "kind", "value")}
    return out


def _filled(form: dict, name: str) -> bool:
    return bool((form.get(name) or "").strip())


def _find_ingredient(form: dict, words: str) -> int | None:
    """The row a spoken ingredient refers to ("the chicken" -> "chicken breasts")."""
    key = name_key(words)
    if not key:
        return None
    rows = [(i, name_key(row["name"])) for i, row in enumerate(form["ingredients"]) if row["name"].strip()]
    for i, row_key in rows:
        if row_key == key:
            return i
    spoken = set(key.split())
    best = [(len(spoken & set(row_key.split())), i) for i, row_key in rows]
    best = [b for b in best if b[0] > 0]
    best.sort(reverse=True)
    if best and (len(best) == 1 or best[0][0] > best[1][0]):
        return best[0][1]
    return None


# ---- Reading what was said ----

_ITEM = re.compile(rf"^(?:(?P<num>{_SPOKEN_NUM})(?:\s+of)?(?:\s+(?:an?\s+)?(?P<unit>{_UNIT_WORDS}))?\s+(?:of\s+)?)?"
                   rf"(?P<name>[a-z][a-z' -]*)$")


def halves(text: str) -> str:
    """'a cup and a half' -> 'one and a half cup'; 'two teaspoons and a half' -> 'two and a half teaspoons'."""
    def swap(match):
        whole = {"a": "one", "an": "one"}.get(match["n"], match["n"])
        return f"{whole} and a half {match['u']}"
    return re.sub(rf"\b(?P<n>a|an|one|two|three|four|\d+)\s+(?P<u>{_UNIT_WORDS})\s+and\s+a\s+half\b", swap, text)


def parse_item(text: str) -> tuple[str, str] | None:
    """'two chicken breasts' -> ('2', 'chicken breasts'); 'a cup of flour' -> ('1 cup', 'flour'); 'salt' -> ('', 'salt')."""
    text = re.sub(r"^(?:some|about|around|roughly|like|maybe|the|my|our|a little(?: bit of)?|a bit of|a touch of|"
                  r"a splash of|a drizzle of|a handful of|a few|a couple of|lots of|plenty of)\s+", "",
                  halves(text.strip(" .,!?").lower()))
    text = re.sub(r"\s+(?:or so|ish|total|in all|altogether)$", "", text)
    match = _ITEM.match(text)
    if not match or not match["name"]:
        return None
    name = re.sub(r"^(?:of|the)\s+", "", match["name"]).strip(" -'")
    if not name or name in _NOT_FOOD or len(name) > 60 or re.fullmatch(rf"(?:{_UNIT_WORDS})", name):
        return None
    amount = spoken_amount(match["num"], match["unit"]) if match["num"] else ""
    if match["num"] and amount is None:
        return None
    if amount and not match["unit"] and match["num"] in ("a", "an") and not _COUNTABLE.search(name):
        amount = ""   # "a pinch of"? no: "a flour" isn't an amount
    return amount or "", name


def _split_list(text: str) -> list[str]:
    parts = re.split(r",\s*(?:and\s+)?|\s+and\s+|\s+plus\s+|\s+with\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.replace("’", "'")).strip()
    parts = re.split(r"(?<=[.!?])\s+|\s+(?:and then|then after that|after that)\s+", text)
    return [p.strip(" .!?").lower() for p in parts if p.strip(" .!?")]


def _clauses(sentence: str) -> list[str]:
    """'I use two chicken breasts, cut them into chunks, and coat them in breadcrumbs' -> three clauses."""
    parts = re.split(r",\s*(?:and\s+|then\s+|and then\s+)?|\s+and then\s+|\s+then\s+", sentence)
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # "...chunks and coat them..." : split on "and <step verb>"
        out.extend(p.strip() for p in re.split(rf"\s+and\s+(?=(?:{STEP_VERBS})\b)", part) if p.strip())
    return out


def _title(text: str) -> str:
    small = {"and", "or", "with", "of", "the", "a", "an", "in", "on", "for", "to"}
    words = text.split()
    return " ".join(w if (i and w in small) else (w[:1].upper() + w[1:]) for i, w in enumerate(words))


def _owner(word: str) -> str:
    """"grandma's" -> "Grandma"; "my mom's" -> "Mom"."""
    word = re.sub(r"'s$|s'$|'$", "", word.strip())
    return _title(word)


_NAME_PATTERNS = [
    re.compile(r"^(?:this is|these are|it's|it is|this recipe is(?: for)?|the recipe is(?: for)?|here's|here is|"
               r"we're making|i'm making|we are making|i am making|let's make|today (?:it's|we're making))\s+"
               r"(?:(?:my|our)\s+)?(?:(?P<who>(?:great[- ])?(?:grandma|grandpa|grandmother|grandfather|nana|nonna|mom|"
               r"mother|dad|father|aunt \w+|uncle \w+|auntie \w+|[a-z]+)(?:'s|s'))\s+)?(?:famous\s+|favorite\s+|"
               r"special\s+|homemade\s+|classic\s+|old\s+)?(?:recipe for\s+)?(?P<name>[a-z][a-z' -]{2,80})$"),
    re.compile(r"^(?:it's called|it is called|call it|we call (?:it|them)|the name is|name it|rename it(?: to)?)\s+"
               r"(?P<name>[a-z][a-z' -]{1,80})$"),
]
_CREDIT_PATTERNS = [
    re.compile(r"^(?:it's|it is|this is|the recipe is|this recipe is)\s+(?:my\s+|our\s+)?(?P<who>[a-z][a-z ]{1,40}?)(?:'s|s')"
               r"\s+recipe$"),
    re.compile(r"^(?:my\s+|our\s+)?(?P<who>[a-z][a-z ]{1,40}?)\s+(?:came up with it|created it|invented it|made it up|"
               r"came up with this|created this|invented this|wrote it|wrote this)$"),
    re.compile(r"^(?:the )?(?:recipe|it|this) (?:is |was )?(?:from|by|created by|credit(?:ed)? to)\s+(?:my\s+|our\s+)?"
               r"(?P<who>[a-z][a-z ]{1,40})$"),
    re.compile(r"^(?:credit|created by|from)\s+(?:my\s+|our\s+)?(?P<who>[a-z][a-z ]{1,40})$"),
]
_SERVINGS = re.compile(r"\b(?:feeds?|serves?|makes?|enough for|it's for|for)\s+(?:a family of\s+|about\s+|around\s+)?"
                       r"(?P<n>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
                       r"(?:\s+(?:people|servings|portions|of us|adults|kids))?|"
                       r"\b(?:enough for|feeds?|serves?)\b[^.]*?\b(?P<n2>\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|"
                       r"eleven|twelve)\s+(?:people|servings|portions|of us)\b")
_MINUTES = r"(?P<n>\d{1,4}|an?|one|two|three|four|five|ten|fifteen|twenty|thirty|forty|forty five|fifty|sixty|half an?)\s*(?P<u>minutes?|mins?|hours?|hrs?)"
_PREP = re.compile(rf"\b(?:prep(?:aration)?(?: time)?(?: is| takes| of)?|takes)\s+(?:about\s+|around\s+)?{_MINUTES}(?:\s+to prep| of prep| prep)?|"
                   rf"{_MINUTES.replace('?P<n>', '?P<n2>').replace('?P<u>', '?P<u2>')}\s+(?:of\s+)?prep")
_COOK = re.compile(rf"\b(?:cook(?:ing)? time(?: is)?|cooks? for|takes)\s+(?:about\s+|around\s+)?{_MINUTES}(?:\s+to cook)?|"
                   rf"{_MINUTES.replace('?P<n>', '?P<n2>').replace('?P<u>', '?P<u2>')}\s+(?:to|of)\s+cook")
_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
             "forty five": 45, "fifty": 50, "sixty": 60, "half a": 0.5, "half an": 0.5}


def _number(word: str) -> float | None:
    word = (word or "").strip().lower()
    if word.isdigit():
        return int(word)
    return _WORD_NUM.get(word)


def _minutes(match, n="n", u="u") -> int | None:
    number = _number(match[n]) if match[n] else None
    if number is None:
        return None
    unit = match[u] or "minutes"
    total = number * 60 if unit.startswith("h") else number
    return int(total) if 0 <= total <= 1440 else None


def _step_body(verb: str, rest: str, last_item: str) -> str:
    """'cut', 'them into chunks' -> 'Cut the chicken breasts into chunks.'"""
    verb = {"sauté": "sauté"}.get(verb, verb)
    rest = rest.strip(" ,")
    if last_item and re.match(r"^(?:them|it)\b", rest):
        rest = re.sub(r"^(?:them|it)\b", f"the {last_item}", rest, count=1)
    body = f"{verb} {rest}".strip()
    body = body[:1].upper() + body[1:]
    return body if body.endswith((".", "!", "?")) else body + "."


def _food_mentions(rest: str) -> list[str]:
    """Ingredients named inside a step: 'coat them in breadcrumbs' -> ['breadcrumbs']."""
    found = []
    for match in re.finditer(r"\b(?:in|with|into|add|of|on|over|sprinkle|top with)\s+(?:some\s+|the\s+|a little\s+|more\s+)?"
                             r"(?P<item>[a-z][a-z' -]{2,40}?)(?=$|,|\s+(?:and|until|for|then|before|after|so|to|at|on|in)\b)",
                             rest):
        item = match["item"].strip()
        if item in _NOT_FOOD or re.search(r"\b(?:minutes?|hours?|degrees|oven|pan|bowl|chunks|pieces|batches|heat|"
                                         r"half|side|time|layer|mixture|batter|sauce pan|water)\b", item):
            continue
        found.append(item)
    return found


# ---- Building edits ----

class Edits:
    """Collects ops against a working copy of the form, so later edits in the same turn see earlier ones."""

    def __init__(self, form: dict, voice: dict):
        self.form = json.loads(json.dumps(form))
        self.voice = voice
        self.ops: list[dict] = []

    def set(self, name: str, value, overwrite: bool = False) -> bool:
        if name not in SCALAR_FIELDS:
            return False
        value = str(value).strip()
        if name in NUMBER_FIELDS:
            low, high = NUMBER_FIELDS[name]
            if not value.isdigit() or not low <= int(value) <= high:
                return False
        if name == "category" and value not in CATEGORIES:
            return False
        value = value[:SCALAR_FIELDS[name]]
        if not value or (self.form.get(name, "").strip() and not overwrite) or self.form.get(name) == value:
            return False
        self.form[name] = value
        self.ops.append({"op": "set", "field": name, "value": value})
        return True

    def ingredient(self, amount: str, name: str, overwrite_amount: bool = False) -> int | None:
        """Add an ingredient, or fill in / correct the one already on the form (never twice)."""
        name = name.strip()[:LIMITS["ingredient"]]
        amount = (amount or "").strip()[:LIMITS["amount"]]
        if not name_key(name):
            return None
        index = _find_ingredient(self.form, name)
        if index is not None:
            row = self.form["ingredients"][index]
            if amount and (overwrite_amount or not row["amount"].strip()) and row["amount"] != amount:
                row["amount"] = amount
                self.ops.append({"op": "update_ingredient", "index": index, "match": row["name"], "amount": amount})
            self.voice["last_ingredient"] = row["name"]
            return index
        if len(self.form["ingredients"]) >= MAX_INGREDIENTS:
            return None
        self.form["ingredients"].append({"amount": amount, "name": name})
        self.ops.append({"op": "add_ingredient", "amount": amount, "name": name})
        self.voice["last_ingredient"] = name
        return len(self.form["ingredients"]) - 1

    def step(self, body: str) -> None:
        body = body.strip()[:LIMITS["step_body"]]
        key = re.sub(r"[^a-z0-9 ]", "", body.lower())
        if not body or any(re.sub(r"[^a-z0-9 ]", "", s["body"].lower()) == key for s in self.form["steps"]):
            return   # the same step said again (or heard twice) is entered once
        if len(self.form["steps"]) >= MAX_STEPS:
            return
        self.form["steps"].append({"title": "", "body": body})
        self.ops.append({"op": "add_step", "body": body})

    def amount(self, index: int, amount: str) -> None:
        row = self.form["ingredients"][index]
        if row["amount"] != amount:
            row["amount"] = amount
            self.ops.append({"op": "update_ingredient", "index": index, "match": row["name"], "amount": amount})


def understand(text: str, form: dict, voice: dict) -> Edits:
    """Edits for what was just said, using clear patterns only (no model)."""
    edits = Edits(form, voice)
    for sentence in _sentences(text):
        _sentence(edits, sentence)
    return edits


def _sentence(edits: Edits, s: str) -> None:
    s = re.sub(r"^(?:(?:um+|uh+|so|okay|ok|alright|well|and|oh|also)[, ]+)+", "", s).strip()
    if not s:
        return
    # Corrections: "actually, make that three chicken breasts"
    correction = re.match(r"^(?:actually|no|sorry|wait|oops)?[, ]*(?:make (?:that|it)|change (?:that|it) to|i meant|"
                          r"it should be|it's actually|correction)[, ]+(?P<rest>.+)$", s)
    if correction:
        item = parse_item(correction["rest"])
        if item and item[0]:
            amount, name = item
            index = _find_ingredient(edits.form, name)
            if index is None and edits.voice.get("last_ingredient"):
                index = _find_ingredient(edits.form, edits.voice["last_ingredient"])
            if index is not None:
                edits.amount(index, amount)
                edits.voice["last_ingredient"] = edits.form["ingredients"][index]["name"]
                return
        number = re.fullmatch(rf"(?P<num>{_SPOKEN_NUM})(?:\s+(?P<unit>{_UNIT_WORDS}))?", correction["rest"].strip())
        last = _find_ingredient(edits.form, edits.voice.get("last_ingredient", "")) if edits.voice.get("last_ingredient") else None
        if number and last is not None:
            amount = spoken_amount(number["num"], number["unit"])
            if amount:
                edits.amount(last, amount)
                return
    if _TO_TASTE.search(s) and re.search(r"\b(?:don't|do not|never|just|eyeball)\b", s):
        item = re.search(r"\b(?:measure|eyeball)\s+(?:the\s+)?(?P<name>[a-z][a-z ]{1,30}?)(?:$|,| either| really| at all)", s)
        if item:
            index = _find_ingredient(edits.form, item["name"])
            if index is not None:
                edits.voice["offer_to_taste"] = edits.form["ingredients"][index]["name"]
                return
            added = edits.ingredient("", item["name"])
            if added is not None:
                edits.voice["offer_to_taste"] = edits.form["ingredients"][added]["name"]
            return
    for pattern in _CREDIT_PATTERNS:
        match = pattern.match(s)
        if match and not re.search(r"\b(?:chicken|beef|pork|soup|cake|pie|pasta|salad|nuggets|chili)\b", match["who"]):
            edits.set("creator_credit", _owner(match["who"]), overwrite=True)
            return
    for pattern in _NAME_PATTERNS:
        match = pattern.match(s)
        if match:
            who = match.groupdict().get("who")
            name = re.sub(r"\s+(?:recipe|dish)$", "", match["name"].strip())
            if _SERVINGS.search(name) or re.search(rf"\b(?:{STEP_VERBS})\b", name.split(" ")[0]):
                break   # "it's for four" / "it's baked": not a name
            explicit = pattern is _NAME_PATTERNS[1]
            full = f"{_owner(who)}'s {_title(name)}" if who else _title(name)
            edits.set("name", full, overwrite=explicit)
            if who and not re.fullmatch(r"(?:my|our)", who):
                edits.set("creator_credit", _owner(who))
            return
    serving = _SERVINGS.search(s)
    if serving and not re.search(r"\bminutes?\b|\bhours?\b", s[serving.start():serving.end() + 12]):
        number = _number(serving["n"] or serving["n2"])
        if number:
            edits.set("servings", str(int(number)), overwrite=True)
            if serving.start() == 0 or re.match(r"^(?:it|they|this|that|the recipe)\s", s):
                return
    prep = _PREP.search(s)
    if prep and "prep" in s:
        value = _minutes(prep) if prep["n"] else _minutes(prep, "n2", "u2")
        if value is not None:
            edits.set("prep_minutes", str(value), overwrite=True)
    cook = _COOK.search(s)
    if cook and re.search(r"\bcook(?:ing)? time\b|\bto cook\b|\bof cook", s):
        value = _minutes(cook) if cook["n"] else _minutes(cook, "n2", "u2")
        if value is not None:
            edits.set("cook_minutes", str(value), overwrite=True)
        return
    if prep and "prep" in s:
        return
    note = re.match(r"^(?:add (?:a|this) note|a note|note|one note|tip|a tip)[:, ]+(?:that\s+)?(?P<body>.+)$", s)
    if note:
        if edits.form.get("editing"):
            edits.voice["note_on_edit"] = True
        else:
            body = note["body"].strip()
            edits.set("note_body", body[:1].upper() + body[1:] + ("" if body.endswith(".") else "."), overwrite=True)
        return
    listed = re.match(r"^(?:the )?ingredients (?:are|is)[: ]+(?P<list>.+)$|^(?:i|we|you)\s+(?:use|need|take|get|grab|buy|"
                      r"start with)\s+(?P<list2>.+)$|^(?:you'll need|it needs|it takes|there's|there is|there are|"
                      r"it has|it's got|plus|also)\s+(?:also\s+)?(?P<list3>.+?)(?:\s+in (?:the|it|there)\b.*|\s+too)?$", s)
    clauses = _clauses(s)
    if listed:
        first = listed["list"] or listed["list2"] or listed["list3"]
        clauses = [first] + clauses[1:]
        _ingredient_list(edits, _split_list_until_step(clauses[0]))
        rest = clauses[1:]
    else:
        rest = clauses
    for clause in rest:
        step = _CLAUSE_STEP.match(clause)
        if step:
            verb = step["verb"]
            body = _step_body(verb, step["rest"], edits.voice.get("last_ingredient", ""))
            edits.step(body)
            for item in _food_mentions(step["rest"]):
                parsed = parse_item(item)
                if parsed and _find_ingredient(edits.form, parsed[1]) is None:
                    edits.ingredient(parsed[0], parsed[1])
        elif listed is None:
            # "two chicken breasts" on its own, or "and a cup of flour"
            parsed = parse_item(re.sub(r"^(?:and|plus|also)\s+", "", clause))
            if parsed and (parsed[0] or _find_ingredient(edits.form, parsed[1]) is not None):
                edits.ingredient(parsed[0], parsed[1])


def _split_list_until_step(text: str) -> list[str]:
    return [p for p in _split_list(text) if not _CLAUSE_STEP.match(p)]


def _ingredient_list(edits: Edits, items: list[str]) -> None:
    for item in items:
        parsed = parse_item(item)
        if parsed:
            edits.ingredient(*parsed)


# ---- Answers to the helper's own questions ----

def answer(pending: dict, text: str, edits: Edits) -> bool:
    """Apply an answer to the question just asked. True if it was understood."""
    s = halves(" ".join(text.lower().replace("’", "'").split()).strip(" .!?"))
    fld = pending.get("field", "")
    if SKIP.match(s):
        if fld not in edits.voice["skipped"]:
            edits.voice["skipped"].append(fld if fld != "amount" else f"amount:{pending.get('value', '')}")
        return True
    if fld == "amount":
        index = _find_ingredient(edits.form, pending.get("value", ""))
        if index is None:
            return False
        if _TO_TASTE.search(s) or s in ("taste", "to taste", "yes", "yeah", "sure", "yep"):
            edits.amount(index, "to taste")
            return True
        item = parse_item(s if re.match(rf"^(?:{_SPOKEN_NUM})", s) else "")
        number = re.match(rf"^(?:about |around |like )?(?P<num>{_SPOKEN_NUM})(?:\s+of)?(?:\s+(?:an?\s+)?(?P<unit>{_UNIT_WORDS}))?",
                          s)
        if number:
            amount = spoken_amount(number["num"], number["unit"])
            if amount:
                edits.amount(index, amount)
                return True
        if item and item[0]:
            edits.amount(index, item[0])
            return True
        return False
    if fld == "unit":
        index = _find_ingredient(edits.form, pending.get("value", ""))
        if index is None:
            return False
        row = edits.form["ingredients"][index]
        if re.match(r"^(?:yes|yeah|yep|right|correct|that's right|exactly)", s):
            suggested = pending.get("kind", "")
            if suggested:
                edits.amount(index, suggested)
            return True
        unit = re.search(rf"\b(?P<unit>{_UNIT_WORDS})\b", s)
        number = re.search(rf"(?P<num>{_SPOKEN_NUM})", s)
        if unit:
            amount = spoken_amount(number["num"] if number else row["amount"].split()[0], unit["unit"])
            if amount:
                edits.amount(index, amount)
                return True
        if re.match(r"^(?:no|nope|just|whole|that many|count|pieces?)", s):
            return True
        return False
    if fld == "name":
        name = re.sub(r"^(?:it's called|it is called|call it|it's|the name is|we call it)\s+", "", s)
        return edits.set("name", _title(name.strip()), overwrite=True) if name else False
    if fld == "servings":
        match = re.search(r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", s)
        return bool(match) and edits.set("servings", str(int(_number(match.group(1)) or 0)), overwrite=True)
    if fld == "times":
        # "fifteen minutes prep and twenty to cook": a bare number next to prep/cook means minutes.
        s = re.sub(r"\b(\d{1,3}|five|ten|fifteen|twenty|thirty|forty|forty five|fifty|sixty)\s+(to cook|of cook|to prep|"
                   r"of prep|prep)\b", r"\1 minutes \2", s)
        prep = _PREP.search(s)
        cook = _COOK.search(s)
        ok = False
        if prep:
            value = _minutes(prep) if prep["n"] else _minutes(prep, "n2", "u2")
            ok = value is not None and edits.set("prep_minutes", str(value), overwrite=True) or ok
        if cook:
            value = _minutes(cook) if cook["n"] else _minutes(cook, "n2", "u2")
            ok = value is not None and edits.set("cook_minutes", str(value), overwrite=True) or ok
        if not ok:
            plain = re.search(_MINUTES, s)
            if plain and _minutes(plain) is not None:
                ok = edits.set("cook_minutes", str(_minutes(plain)), overwrite=True)
        return ok
    if fld == "category":
        for key, label in CATEGORIES.items():
            words = label.lower().replace("'", "").split()
            if any(w in s.replace("'", "") for w in words + [key.replace("-", " ")]):
                edits.set("category", key, overwrite=True)
                return True
        return False
    if fld == "creator_credit":
        who = re.sub(r"^(?:it was|it's|it is|that's|that was|from|by|my|our|credit)\s+", "", s)
        who = re.sub(r"^(?:my|our)\s+", "", who)
        who = re.sub(r"\s+(?:came up with it|created it|made it up|did)$", "", who)
        if re.fullmatch(r"(?:me|i did|myself|i made it up|mine)", who):
            return edits.set("creator_credit", "Me", overwrite=True) or True
        return edits.set("creator_credit", _owner(who), overwrite=True) if who else False
    if fld == "note_body":
        body = re.sub(r"^(?:yes|yeah|sure)[, ]+|^(?:the note is|note|add)[: ]+", "", text.strip())
        if not body or SKIP.match(body.lower().strip(" .")):
            return False
        return edits.set("note_body", body[:1].upper() + body[1:], overwrite=True)
    if fld == "steps":
        before = len(edits.ops)
        for sentence in _sentences(text):
            for clause in _clauses(sentence):
                step = _CLAUSE_STEP.match(clause)
                if step:
                    edits.step(_step_body(step["verb"], step["rest"], edits.voice.get("last_ingredient", "")))
                elif len(clause.split()) >= 3:
                    edits.step(clause[:1].upper() + clause[1:] + ".")
        return len(edits.ops) > before
    if fld == "ingredients":
        before = len(edits.ops)
        _ingredient_list(edits, _split_list(re.sub(r"^(?:i use|we use|you need|it's|there's)\s+", "", s)))
        return len(edits.ops) > before
    if fld == "to_taste":
        index = _find_ingredient(edits.form, pending.get("value", ""))
        if index is not None and re.match(r"^(?:yes|yeah|yep|sure|ok|okay|please|that's fine|to taste)", s):
            edits.amount(index, "to taste")
            return True
        return bool(re.match(r"^(?:no|nope)", s))
    return False


# ---- What to ask next (one thing at a time, after the person has finished) ----

def _amount_unclear(row: dict) -> str | None:
    """A bare number for something that's measured, e.g. '2 flour': suggest the likely unit."""
    amount = row["amount"].strip()
    if not re.fullmatch(r"\d+(?:\s+\d+/\d+)?|\d+/\d+", amount) or _COUNTABLE.search(row["name"].lower()):
        return None
    name = row["name"].lower()
    unit = "cups" if any(w in name for w in ("flour", "sugar", "rice", "milk", "broth", "stock", "water", "cream",
                                            "crumbs", "cheese", "oats", "pasta", "beans", "sauce")) else \
        "tablespoons" if any(w in name for w in ("oil", "butter", "vinegar", "soy", "honey", "syrup")) else \
        "teaspoons" if any(w in name for w in ("salt", "pepper", "powder", "paprika", "cumin", "spice", "extract",
                                              "baking soda", "oregano", "thyme")) else ""
    if not unit:
        return None
    if amount == "1":
        unit = unit[:-1]
    return f"{amount} {unit}"


def next_question(form: dict, voice: dict) -> tuple[str, dict | None]:
    """The single most useful question now, or a closing line when the draft looks complete."""
    asked, skipped = set(voice["asked"]), set(voice["skipped"])

    def ask(key: str, text: str, pending: dict):
        voice["asked"].append(key)
        voice["pending"] = pending
        return text, pending

    if voice.get("offer_to_taste"):
        name = voice.pop("offer_to_taste")
        return ask(f"to_taste:{name}", f"No problem. Should I write the {name} as “to taste”?",
                   {"field": "to_taste", "value": name})
    # Clarify uncertain amounts first.
    for row in form["ingredients"]:
        if not row["name"].strip():
            continue
        unclear = _amount_unclear(row)
        if unclear and f"unit:{row['name']}" not in asked:
            return ask(f"unit:{row['name']}", f"For the {row['name']}, is that {unclear}?",
                       {"field": "unit", "value": row["name"], "kind": unclear})
    if not form["name"].strip() and "name" not in skipped and "name" not in asked:
        return ask("name", "What's this recipe called?", {"field": "name"})
    if not any(r["name"].strip() for r in form["ingredients"]) and "ingredients" not in asked:
        return ask("ingredients", "What goes into it? Tell me the ingredients and how much of each.",
                   {"field": "ingredients"})
    for row in form["ingredients"]:
        if row["name"].strip() and not row["amount"].strip() and f"amount:{row['name']}" not in asked \
                and f"amount:{row['name']}" not in skipped:
            hint = " You can say “to taste”." if re.search(r"\b(salt|pepper|spice|seasoning|herbs?)\b",
                                                                  row["name"].lower()) else ""
            return ask(f"amount:{row['name']}", f"How much {row['name']} do you use?{hint}",
                       {"field": "amount", "value": row["name"]})
    if not any(s["body"].strip() for s in form["steps"]) and "steps" not in asked:
        return ask("steps", "How do you make it? Tell me the steps in order.", {"field": "steps"})
    if not form["servings"].strip() and "servings" not in asked:
        return ask("servings", "How many people does it serve?", {"field": "servings"})
    if not (form["prep_minutes"].strip() or form["cook_minutes"].strip()) and "times" not in asked:
        return ask("times", "About how long does it take to prep, and to cook? You can say skip.", {"field": "times"})
    if not form["creator_credit"].strip() and "creator_credit" not in asked:
        return ask("creator_credit", "Who created this recipe? You can say skip.", {"field": "creator_credit"})
    if not form.get("editing") and not form["note_body"].strip() and "note_body" not in asked:
        return ask("note_body", "Any note for the card, like a tip or a memory? You can say skip.", {"field": "note_body"})
    voice["pending"] = None
    return ("That's everything I need. Look over the recipe, change anything you like, and tap Save when it's right. "
            "Nothing is saved until you do."), None


# ---- The model, for speech the patterns don't cover ----

MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "ingredients": {"type": "array", "items": {"type": "object", "properties": {
            "amount": {"type": "string"}, "name": {"type": "string"}}, "required": ["amount", "name"]}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "servings": {"type": ["integer", "null"]},
        "cook_minutes": {"type": ["integer", "null"]},
    },
    "required": ["ingredients", "steps"],
}

# A flat shape with examples from unrelated dishes: small local models copy example answers
# that look like the real task (measured with qwen3:1.7b), so the examples are deliberately different.
MODEL_PROMPT = """Extract recipe details from one thing a cook said. Copy only what they said. Leave lists empty if they didn't mention any.

Example: "Toss in a handful of basil and simmer it for ten minutes"
{{"ingredients": [{{"amount": "1 handful", "name": "basil"}}], "steps": ["Simmer it for ten minutes."], "servings": null, "cook_minutes": null}}

Example: "It feeds six"
{{"ingredients": [], "steps": [], "servings": 6, "cook_minutes": null}}

Now: "{text}"
"""


def form_summary(form: dict) -> str:
    lines = [f"name: {form['name'] or '(empty)'}", f"servings: {form['servings'] or '(empty)'}",
             f"creator_credit: {form['creator_credit'] or '(empty)'}", "ingredients:"]
    lines += [f"  - {r['amount']} {r['name']}".rstrip() for r in form["ingredients"] if r["name"].strip()] or ["  (none)"]
    lines.append("steps:")
    lines += [f"  {i}. {s['body']}" for i, s in enumerate(form["steps"], 1) if s["body"].strip()] or ["  (none)"]
    return "\n".join(lines)


def model_edits(ask, text: str, form: dict, voice: dict) -> Edits:
    """Edits the model suggests, each checked against what was said. Malformed output changes nothing."""
    edits = Edits(form, voice)
    raw = ask([{"role": "user", "content": MODEL_PROMPT.format(text=text[:600].replace('"', "'"))}], MODEL_SCHEMA)
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.S)
        try:
            data = json.loads(match.group(0)) if match else {}
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        return edits
    spoken = text.lower()
    for item in (data.get("ingredients") if isinstance(data.get("ingredients"), list) else [])[:15]:
        if not isinstance(item, dict):
            continue
        name, amount = " ".join(str(item.get("name") or "").split()), " ".join(str(item.get("amount") or "").split())
        if not name or not _grounded(name, spoken):
            continue
        parsed = parse_item(f"{amount} {name}".strip()) if amount else None
        if parsed and parsed[0] and name_key(parsed[1]) == name_key(name):
            amount = parsed[0]          # "half a cup" -> "1/2 cup" only when the whole amount was understood
        elif amount and not _grounded(amount, spoken, "amount"):
            amount = ""
        edits.ingredient(amount[:LIMITS["amount"]], name)
    for step in (data.get("steps") if isinstance(data.get("steps"), list) else [])[:10]:
        body = " ".join(str(step or "").split())
        if body and _grounded(body, spoken, "step"):
            body = body[:1].upper() + body[1:]
            edits.step(body if body.endswith((".", "!", "?")) else body + ".")
    for name in ("servings", "cook_minutes"):
        value = data.get(name)
        if isinstance(value, int) and _grounded(str(value), spoken, name):
            edits.set(name, str(value), overwrite=True)
    return edits


def _grounded(value: str, spoken: str, kind: str = "") -> bool:
    """Only accept model values that come from what was said (no invented amounts or ingredients)."""
    value = value.lower().strip()
    if not value:
        return False
    if kind in ("servings", "prep_minutes", "cook_minutes", "amount"):
        digits = re.findall(r"\d+", value)
        words = [w for w, n in _AMOUNT_WORDS.items() if any(str(n).split("/")[0] == d for d in digits)]
        return any(d in spoken for d in digits) or any(re.search(rf"\b{re.escape(w)}\b", spoken) for w in words) \
            or (not digits and value in spoken)
    words = [w for w in re.findall(r"[a-z]{3,}", value) if w not in {"the", "and", "with", "into", "then"}]
    if not words:
        return False
    hits = sum(1 for w in words if w in spoken or w.rstrip("s") in spoken)
    return hits / len(words) >= (0.5 if kind == "step" else 0.6)


# ---- One turn ----

def turn(text: str, form: dict, voice: dict, mode: str, ask=None) -> VoiceResult:
    """mode "fill": the person said something (they may still be talking: no questions).
    mode "next": they've paused or finished: ask the next question, or close."""
    result = VoiceResult(voice=voice)
    plain = " ".join(text.lower().replace("’", "'").split()).strip(" .!?,")
    if mode == "next":
        result.say, pending = next_question(form, voice)
        result.ask = (pending or {}).get("field", "")
        result.done = pending is None
        return result
    if not plain:
        return result
    if UNDO.match(plain):
        result.ops = [{"op": "undo"}]
        result.say = "Undone."
        return result
    if DONE.match(plain):
        voice["pending"] = None
        result.say, pending = next_question(form, voice)
        result.ask = (pending or {}).get("field", "")
        result.done = pending is None
        return result
    pending = voice.get("pending")
    if pending:
        edits = Edits(form, voice)
        if answer(pending, text, edits):
            voice["pending"] = None
            result.ops = edits.ops
            result.voice = edits.voice
            result.say, nxt = next_question(edits.form, edits.voice)
            result.ask = (nxt or {}).get("field", "")
            result.done = nxt is None
            return result
        # Not an answer: treat it as something new they wanted to say (the question stays open).
    if CHATTER.match(plain):
        return result      # "that sounds good" never saves or changes anything
    if INVENT.search(plain) and ask is not None:
        voice["mode"] = "invent"
        result.ops, result.say = [], ""
        result.voice = voice
        result.done = False
        voice["pending"] = {"field": "invent", "value": plain[:200]}
        return result
    edits = understand(text, form, voice)
    if not edits.ops and ask is not None and len(plain.split()) >= 4:
        try:
            edits = model_edits(ask, text, form, voice)
        except AssistantUnavailable:
            result.say = ("I didn't catch how to fill that in, and the assistant on your computer isn't answering. "
                          "You can type it into the form.")
    result.ops, result.voice = edits.ops, edits.voice
    if edits.voice.get("note_on_edit"):
        edits.voice.pop("note_on_edit")
        result.say = "Notes are added from the recipe page after saving, under Notes from the kitchen."
    return result
