# Turning recipe ingredient lines into combined shopping list items.
# Everything here is plain Python with no database access, so it's easy to test.

# re matches amounts, units, and words to clean out of names.
import re

# unicodedata strips accents so "jalapeño" and "jalapeno" match.
import unicodedata

# dataclass holds a parsed ingredient and a combined item.
from dataclasses import dataclass, field

# Fraction keeps quantities exact (1/3 cup + 1/3 cup = 2/3 cup, never 0.6666).
from fractions import Fraction

# split_ingredient pulls an amount out of lines like "2 cups flour" typed in one box.
from app.recipe_import import split_ingredient


# Unit spellings -> (canonical unit, family, size in the family's base unit).
# Units only convert within a family, so volume and weight never mix.
_UNIT_TABLE = {
    "tsp": ("tsp", "volume", 1), "teaspoon": ("tsp", "volume", 1),
    "tbsp": ("tbsp", "volume", 3), "tbs": ("tbsp", "volume", 3), "tbl": ("tbsp", "volume", 3),
    "tablespoon": ("tbsp", "volume", 3),
    "cup": ("cup", "volume", 48),
    "pint": ("pint", "volume", 96), "pt": ("pint", "volume", 96),
    "quart": ("quart", "volume", 192), "qt": ("quart", "volume", 192),
    "gallon": ("gallon", "volume", 768), "gal": ("gallon", "volume", 768),
    "fl oz": ("fl oz", "volume", 6), "fluid ounce": ("fl oz", "volume", 6),
    "ml": ("ml", "metric volume", 1), "milliliter": ("ml", "metric volume", 1),
    "millilitre": ("ml", "metric volume", 1),
    "l": ("l", "metric volume", 1000), "liter": ("l", "metric volume", 1000),
    "litre": ("l", "metric volume", 1000),
    "oz": ("oz", "weight", 1), "ounce": ("oz", "weight", 1),
    "lb": ("lb", "weight", 16), "pound": ("lb", "weight", 16),
    "g": ("g", "metric weight", 1), "gram": ("g", "metric weight", 1),
    "kg": ("kg", "metric weight", 1000), "kilogram": ("kg", "metric weight", 1000),
}

# Countable packaging/units: each only adds to itself (2 cans + 1 can = 3 cans).
_COUNT_UNITS = {
    "clove", "can", "slice", "bunch", "stalk", "sprig", "package", "packet", "jar", "bottle",
    "head", "stick", "piece", "pinch", "dash", "bag", "box", "container", "fillet", "strip",
}

# Size words in the amount box ("1 large") don't change what you buy.
_SIZE_WORDS = {"small", "medium", "med", "large", "extra-large", "jumbo", "heaping", "level"}

# Plural unit labels for display.
_PLURAL_UNITS = {
    "cup": "cups", "pint": "pints", "quart": "quarts", "gallon": "gallons", "lb": "lbs",
    "bunch": "bunches", "box": "boxes", "pinch": "pinches", "dash": "dashes",
}

# Preparation and size words removed from names: "finely chopped onion" is still onion.
# Words that change the product (dried, ground, crushed, salted...) are deliberately kept.
_DROP_WORDS = {
    "chopped", "diced", "minced", "sliced", "cubed", "julienned", "shredded", "grated",
    "finely", "thinly", "roughly", "coarsely", "freshly", "fresh", "softened", "melted",
    "peeled", "deveined", "trimmed", "rinsed", "drained", "halved", "quartered", "divided",
    "small", "medium", "med", "large", "extra-large", "lean", "extra-lean", "optional",
}

# After a comma, these words start preparation notes ("garlic, minced"), not more ingredient.
_COMMA_NOTES = _DROP_WORDS | {
    "for", "to", "plus", "about", "see", "note", "from", "cut", "at", "such", "if", "or",
    "and", "room", "cold", "warm", "stems", "seeded", "patted", "crumbled", "torn",
}

# Cooking states that mean nothing to buy on their own ("cooked, halved squash").
_STATE_WORDS = frozenset({"cooked", "uncooked", "raw", "frozen", "thawed"})

# Same ingredient, different wording -> one canonical name.
SYNONYMS = {
    "pepper": "black pepper", "ground black pepper": "black pepper",
    "ground pepper": "black pepper", "black ground pepper": "black pepper",
    "kosher salt": "salt", "sea salt": "salt", "fine sea salt": "salt", "coarse salt": "salt",
    "table salt": "salt", "fine salt": "salt", "coarse kosher salt": "salt",
    "ground cumin": "cumin",
    "garlic clove": "garlic", "clove garlic": "garlic", "clove of garlic": "garlic",
    "cloves garlic": "garlic", "cloves of garlic": "garlic",
    "all-purpose flour": "flour", "all purpose flour": "flour",
    "corn starch": "cornstarch",
    "extra virgin olive oil": "extra-virgin olive oil",
    "scallion": "green onion",
    "egg large": "egg",
}

# Names whose wording says the unit: "4 garlic cloves" means 4 cloves of garlic.
_UNIT_IN_NAME = {"garlic clove", "clove garlic", "clove of garlic", "cloves garlic", "cloves of garlic"}

# A plain name merges into one qualified version: "butter" + "unsalted butter" -> unsalted butter.
# Only when exactly one qualified version is present, so salted and unsalted never merge.
QUALIFIERS = {"unsalted", "salted", "extra-virgin"}

# Words that end in "s" but aren't plurals.
_NOT_PLURAL = {
    "asparagus", "hummus", "molasses", "couscous", "swiss", "brussels", "grits", "citrus",
    "lemongrass", "bass", "glass", "watercress", "series", "species", "chives", "oats",
    "greens", "noodles", "peas", "beans", "lentils", "chips", "flakes", "sprouts",
}

_FRACTION_CHARS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8", "⅕": "1/5", "⅙": "1/6",
}

# A quantity at the start of the amount: "1", "1.5", "1 1/2", "3/4".
_QTY_RE = re.compile(r"^(?P<qty>\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)(?![\d/])\s*(?P<rest>.*)$")


@dataclass
class Parsed:
    """One ingredient line, understood as far as is safe."""

    key: str                  # normalized name used for matching ("ground beef")
    display: str              # friendly name ("Lean ground beef")
    qty: Fraction | None      # None when there's no usable number
    unit: str | None          # canonical unit, or None for plain counts
    raw: str                  # amount we couldn't interpret ("2-3 cups"), kept as typed
    amount: str               # original amount text, shown in the sources line
    synonym: bool = False     # True if the name was mapped to a canonical one


@dataclass
class Source:
    recipe_id: int
    recipe_name: str
    amount: str
    times: int = 1            # same recipe planned on more than one day


@dataclass
class Combined:
    """A shopping list line built from one or more ingredient lines."""

    key: str
    display: str
    quantity: str             # "3 lbs", "13 cloves + 2 tsp", or ""
    totals: dict              # machine-comparable totals (see quantity_totals)
    sources: list[Source] = field(default_factory=list)


# ---- Names ----

def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _singular(word: str) -> str:
    """Conservative singular form of one word: onions -> onion, tomatoes -> tomato."""
    if len(word) <= 3 or word in _NOT_PLURAL or not word.endswith("s"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"           # berries -> berry
    if word.endswith("oes"):
        return word[:-2]                 # tomatoes -> tomato
    if word.endswith("ves") and word[:-3] in {"lea", "hal", "loa"}:
        return word[:-3] + "f"           # leaves -> leaf
    if re.search(r"(ches|shes|sses|xes|zes)$", word):
        return word[:-2]                 # peaches -> peach
    if word.endswith(("ss", "us", "is")):
        return word                      # not plurals
    return word[:-1]                     # onions -> onion


def _light_clean(name: str) -> str:
    """Remove notes from a name: parentheses, ', minced', 'for serving', footnote marks."""
    text = re.sub(r"[¹²³⁴⁵*†‡]", "", name)
    # Remove (nested (parentheses)) from the inside out.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\([^()]*\)", " ", text)
    text = text.replace("(", " ").replace(")", " ")
    text = re.sub(r",?\s*\bnote \d+\b", "", text, flags=re.I)

    # Keep the part before a comma only if what follows is a preparation note.
    if "," in text:
        head, tail = text.split(",", 1)
        first = (tail.strip().split() or [""])[0].lower().strip(",.")
        if not _drop_words(head, extra=_STATE_WORDS):
            text = tail                       # "cooked, halved spaghetti squash"
        elif first in _COMMA_NOTES or not tail.strip():
            text = head

    text = re.sub(r"\s+plus\s+.*$", "", text, flags=re.I)                     # "juice plus zest"
    text = re.sub(r"\s+for\s+.*$", "", text, flags=re.I)                      # "for serving"
    text = re.sub(r"\s+(?:or\s+)?(?:more\s+|less\s+)?to\s+(?:taste|season)\b.*$", "", text, flags=re.I)
    text = re.sub(r"^(?:a\s+)?(?:pinch|dash|splash|handful)\s+of\s+", "", text.strip(), flags=re.I)
    text = re.sub(r"^(?:cans?|jars?|of)\s+", "", text.strip(), flags=re.I)  # "15 oz" + "can black beans"
    text = re.sub(r"\s+", " ", text).strip(" ,.-;:")
    return text


def _drop_words(text: str, extra: frozenset = frozenset()) -> list[str]:
    """Remove preparation words, plus an 'or'/'and' joining two of them ('diced or shredded')."""
    words = text.split()
    dropped = [w.lower().strip(",.") in _DROP_WORDS or w.lower().strip(",.") in extra for w in words]
    kept = []
    for i, word in enumerate(words):
        if dropped[i]:
            continue
        if word.lower() in ("or", "and") and 0 < i < len(words) - 1 and dropped[i - 1] and dropped[i + 1]:
            continue
        kept.append(word)
    # Never leave a dangling conjunction at either end.
    while kept and kept[0].lower() in ("or", "and"):
        kept.pop(0)
    while kept and kept[-1].lower() in ("or", "and"):
        kept.pop()
    return kept


def _plain_key(name: str) -> tuple[str, str]:
    """(key before synonyms, display name) for an ingredient name."""
    cleaned = _light_clean(name)
    display = " ".join(_drop_words(cleaned) or cleaned.split())
    key_words = [w.strip(",.") for w in _strip_accents(display).lower().split()]
    key_words = [w for w in key_words if w]
    if key_words:
        key_words[-1] = _singular(key_words[-1])   # the last word is usually the noun
    return " ".join(key_words), display


def normalize_name(name: str) -> tuple[str, str, bool]:
    """Return (match key, display name, synonym used) for an ingredient name."""
    key, display = _plain_key(name)
    if key in SYNONYMS:
        return SYNONYMS[key], SYNONYMS[key], True
    return key, display, False


def name_key(name: str) -> str:
    """Matching key for a typed name (staples and manual items use this too)."""
    return normalize_name(name)[0]


def _base_key(key: str) -> str | None:
    """'unsalted butter' -> 'butter' when the first word is a known qualifier."""
    first, _, rest = key.partition(" ")
    return rest if first in QUALIFIERS and rest else None


def matches_staple(key: str, staple_keys: set[str]) -> bool:
    """Is this item one of the family's staples? ('unsalted butter' counts as Butter.)"""
    return key in staple_keys or _base_key(key) in staple_keys


# ---- Amounts ----

def _parse_number(text: str) -> Fraction | None:
    """'1 1/2' -> 3/2, '1.5' -> 3/2, '3/4' -> 3/4."""
    try:
        return sum((Fraction(part) for part in text.split()), Fraction(0))
    except (ValueError, ZeroDivisionError):
        return None


def _unit_lookup(word: str) -> str | None:
    """Canonical unit for a unit word, or None."""
    word = word.lower().strip(".")
    for candidate in (word, word[:-1] if word.endswith("s") else word, word[:-2] if word.endswith("es") else word):
        if candidate in _UNIT_TABLE:
            return _UNIT_TABLE[candidate][0]
        if candidate in _COUNT_UNITS:
            return candidate
    return None


def parse_amount(amount: str) -> tuple[Fraction | None, str | None, str]:
    """Return (quantity, unit, raw). raw is set when the amount can't be used safely."""
    text = amount.strip()
    if not text:
        return None, None, ""
    for char, ascii_fraction in _FRACTION_CHARS.items():
        text = text.replace(char, f" {ascii_fraction}")
    text = re.sub(r"\([^()]*\)", " ", text.lower())          # "1/3 cup (73 g)" -> "1/3 cup"
    text = re.sub(r"\s+", " ", text).strip()

    match = _QTY_RE.match(text)
    if not match:
        return None, None, amount.strip()
    rest = match["rest"].strip()
    # Ranges like "2-3 cups" or "1 to 2 tbsp" can't be added up honestly.
    if re.match(r"^(?:-|–|to\s+|or\s+)\s*\d", rest):
        return None, None, amount.strip()
    qty = _parse_number(match["qty"])
    if qty is None or qty <= 0:
        return None, None, amount.strip()

    words = [w for w in rest.replace("-", " ").split() if w.strip(".") not in _SIZE_WORDS]
    if not words:
        return qty, None, ""
    if len(words) >= 2 and _unit_lookup(" ".join(words[:2])) == "fl oz":
        unit, leftover = "fl oz", words[2:]
    else:
        unit, leftover = _unit_lookup(words[0]), words[1:]
    # Unknown words in the amount box mean we're not sure what it says; keep it as typed.
    if unit is None or leftover:
        return None, None, amount.strip()
    return qty, unit, ""


def parse_line(amount: str, name: str) -> list[Parsed]:
    """Understand one recipe ingredient line (may return 0, 1, or 2 items)."""
    amount, name = (amount or "").strip(), (name or "").strip()
    if not name:
        return []
    if not amount:
        # "Juice of 2 limes" -> 2 limes; "2 cups flour" typed in the name box -> split it.
        juice = re.match(r"^(?:the\s+)?(?:juice|zest)\s+(?:of\s+)?(\d+(?:/\d+)?|one|two|three)\s+(.+)$", name, re.I)
        if juice:
            words = {"one": "1", "two": "2", "three": "3"}
            amount, name = words.get(juice[1].lower(), juice[1]), juice[2]
        else:
            amount, name = split_ingredient(name)

    key, display, synonym = normalize_name(name)
    if not key:
        return []

    # "Salt and pepper" is really two staples.
    if " and " in key and not amount:
        halves = {normalize_name(half)[0] for half in key.split(" and ", 1)}
        if halves == {"salt", "black pepper"}:
            return [Parsed("salt", "salt", None, None, "", "", True),
                    Parsed("black pepper", "black pepper", None, None, "", "", True)]

    qty, unit, raw = parse_amount(amount)
    if qty is not None and unit is None and _plain_key(name)[0] in _UNIT_IN_NAME:
        unit = "clove"
    return [Parsed(key, display, qty, unit, raw, amount, synonym)]


# ---- Combining ----

def _family(unit: str | None) -> tuple[str, int]:
    """(family, size in base units) for a canonical unit; counts are their own family."""
    if unit is None:
        return "count", 1
    if unit in _UNIT_TABLE:
        _, family, size = _UNIT_TABLE[unit]
        return family, size
    return unit, 1


def format_number(value: Fraction) -> str:
    """3/2 -> '1 1/2'; odd fractions fall back to a short decimal."""
    whole, part = divmod(value.numerator, value.denominator)
    part = Fraction(part, value.denominator)
    if part == 0:
        return str(whole)
    if part.denominator in (2, 3, 4, 8):
        return f"{whole} {part}" if whole else str(part)
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _unit_label(unit: str | None, value: Fraction) -> str:
    if unit is None:
        return ""
    if value > 1:
        if unit in _PLURAL_UNITS:
            return _PLURAL_UNITS[unit]
        if unit in _COUNT_UNITS:
            return unit + "s"
    return unit


def _volume_text(total: Fraction) -> str:
    """US volume in tsp -> '1/4 cup', '1 1/2 cups + 2 tsp', '1 tbsp' (3 tsp = 1 tbsp, 4 tbsp = 1/4 cup)."""
    cups = total / 48
    if total >= 12 and cups.denominator in (1, 2, 3, 4):
        return _format_part(cups, "cup")
    pieces = []
    if total >= 12:
        quarters = Fraction(int(total // 12), 4)       # whole quarter cups
        pieces.append(_format_part(quarters, "cup"))
        total -= quarters * 48
    tbsp = total / 3
    if tbsp >= 1 and tbsp.denominator in (1, 2):
        return " + ".join(pieces + [_format_part(tbsp, "tbsp")])
    if total >= 3:
        whole = int(total // 3)
        pieces.append(_format_part(Fraction(whole), "tbsp"))
        total -= whole * 3
    if total:
        pieces.append(_format_part(total, "tsp"))
    return " + ".join(pieces)


def _family_text(family: str, group: list[Parsed]) -> str:
    """Total for one unit family, in the most readable unit."""
    units = {p.unit for p in group}
    total = sum((p.qty * _family(p.unit)[1] for p in group), Fraction(0))
    if family == "volume" and units <= {"tsp", "tbsp", "cup"}:
        return _volume_text(total)
    if family == "weight":
        if units == {"lb"} or (total >= 16 and (total / 16).denominator in (1, 2, 4)):
            return _format_part(total / 16, "lb")
        if units == {"oz"} or total < 16:
            return _format_part(total, "oz")
        pounds = int(total // 16)
        return f"{_format_part(Fraction(pounds), 'lb')} + {_format_part(total - pounds * 16, 'oz')}"
    if len(units) == 1:
        # Everyone used the same unit (pints, cans, plain counts): keep it.
        unit = next(iter(units))
        return _format_part(total / _family(unit)[1], unit)
    if family in ("metric volume", "metric weight"):
        big, small = ("l", "ml") if family == "metric volume" else ("kg", "g")
        if total >= 1000:
            return _format_part(total / 1000, big)
        return _format_part(total, small)
    return _volume_text(total)   # mixed pints/cups etc.: express in cups and spoons


def _format_part(value: Fraction, unit: str | None) -> str:
    label = _unit_label(unit, value)
    return f"{format_number(value)} {label}".strip()


def quantity_totals(parts: list[Parsed]) -> dict:
    """Machine-readable totals: {family: base-unit total as text, '_raw': [...]}."""
    totals: dict = {}
    raw: list[str] = []
    for p in parts:
        if p.qty is not None:
            family, size = _family(p.unit)
            totals[family] = totals.get(family, Fraction(0)) + p.qty * size
        elif p.raw:
            raw.append(p.raw)
    result = {family: str(value) for family, value in totals.items()}
    if raw:
        result["_raw"] = sorted(raw)
    return result


def format_quantity(parts: list[Parsed]) -> str:
    """Add up quantities that can be added; keep the rest exactly as typed."""
    by_family: dict[str, list[Parsed]] = {}
    raw: list[str] = []
    for p in parts:
        if p.qty is not None:
            by_family.setdefault(_family(p.unit)[0], []).append(p)
        elif p.raw:
            raw.append(p.raw)

    pieces = []
    # Plain counts first ("2"), then everything else in a stable order.
    for family in sorted(by_family, key=lambda f: (f != "count", f)):
        pieces.append(_family_text(family, by_family[family]))
    pieces.extend(raw)
    return " + ".join(pieces)


def combine(lines: list[tuple]) -> dict[str, Combined]:
    """Combine (recipe_id, recipe_name, amount, name[, times]) lines into shopping items by key.

    `times` is how many dinners use that recipe this week (defaults to 1).
    """
    groups: dict[str, list[tuple[Parsed, int, str, int]]] = {}
    for recipe_id, recipe_name, amount, name, *rest in lines:
        times = rest[0] if rest else 1
        for parsed in parse_line(amount, name):
            groups.setdefault(parsed.key, []).append((parsed, recipe_id, recipe_name, times))

    # "butter" joins "unsalted butter" when that's the only qualified kind on the list.
    for key in list(groups):
        variants = [k for k in groups if _base_key(k) == key]
        if len(variants) == 1:
            groups[variants[0]].extend(groups.pop(key))

    combined = {}
    for key, entries in groups.items():
        parsed_lines = [p for p, _, _, _ in entries]
        # A recipe planned for two dinners needs everything twice.
        parts = [p for p, _, _, times in entries for _ in range(times)]

        # Prefer the shortest wording as the display name ("Onion" over "Large yellow onion").
        canonical = next((p for p in parsed_lines if p.synonym), None)
        display = canonical.display if canonical else min((p.display for p in parsed_lines), key=len)
        if _base_key(key) and _base_key(key) in {p.key for p in parsed_lines}:
            display = next(p.display for p in parsed_lines if p.key == key)

        # One source per recipe; a recipe that lists it twice shows "1/2 cup + 1/2 cup".
        sources: dict[int, Source] = {}
        for parsed, recipe_id, recipe_name, times in entries:
            if recipe_id in sources:
                if parsed.amount:
                    joined = " + ".join(a for a in (sources[recipe_id].amount, parsed.amount) if a)
                    sources[recipe_id].amount = joined
            else:
                sources[recipe_id] = Source(recipe_id, recipe_name, parsed.amount, times)
        combined[key] = Combined(key, display[:1].upper() + display[1:], format_quantity(parts),
                                 quantity_totals(parts), list(sources.values()))
    return combined


def needs_more(old: dict, new: dict) -> bool:
    """True if the new totals ask for more of anything than the old ones did."""
    for family, value in new.items():
        if family == "_raw":
            if set(value) - set(old.get("_raw", [])):
                return True
        elif Fraction(value) > Fraction(old.get(family, "0")):
            return True
    return False
