# Serving adjustments and unit conversions. Plain Python with no database access.
#
# Every scaled amount is calculated from the recipe's original text and the ratio
# (servings wanted / servings the recipe makes), never from an earlier scaled result,
# so changing servings back and forth can't build up rounding errors.

# re finds quantities in amounts and instructions.
import re

# dataclass describes a conversion answer.
from dataclasses import dataclass

# Fraction keeps quantities exact until they're shown.
from fractions import Fraction

# Shared ingredient helpers: units, number formatting, and name matching.
from app.ingredients import (
    _FRACTION_CHARS, _UNIT_TABLE, _unit_lookup, _volume_text, format_number, normalize_name, parse_amount,
)
from app.recipe_import import split_ingredient


MIN_SERVINGS = 1
MAX_SERVINGS = 100

# One number: "2", "1.5", "1 1/2", "3/4".
_NUM = r"\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+"
# A quantity at the start of an amount, optionally a range ("2-3", "1 to 2").
_LEAD_RE = re.compile(rf"^(?P<a>{_NUM})(?:\s*(?P<sep>-|–|to)\s*(?P<b>{_NUM}))?(?![\d/])(?P<rest>.*)$", re.S)
# A metric equivalent written right after the unit: "2 cups (240 g)".
_PAREN_METRIC_RE = re.compile(r"^(?P<lead>\s*[A-Za-z.]+\s*)\(\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<u>g|grams?|kg|ml|millilit(?:er|re)s?|l)\s*\)",
                              re.I)

# Unit words and their plural spellings, so "1 pound" becomes "2 pounds" and back.
_PLURALS = {
    "cup": "cups", "pound": "pounds", "lb": "lbs", "ounce": "ounces", "can": "cans", "clove": "cloves",
    "slice": "slices", "tablespoon": "tablespoons", "teaspoon": "teaspoons", "pinch": "pinches",
    "dash": "dashes", "package": "packages", "packet": "packets", "stick": "sticks", "bunch": "bunches",
    "quart": "quarts", "pint": "pints", "gallon": "gallons", "head": "heads", "piece": "pieces",
    "sprig": "sprigs", "stalk": "stalks", "jar": "jars", "bag": "bags", "box": "boxes", "fillet": "fillets",
    "bottle": "bottles", "container": "containers", "strip": "strips", "liter": "liters", "litre": "litres",
    "gram": "grams", "kilogram": "kilograms", "milliliter": "milliliters",
}
_SINGULARS = {plural: singular for singular, plural in _PLURALS.items()}

# US spoons and cups, in teaspoons.
_SPOON_TSP = {"tsp": 1, "tbsp": 3, "cup": 48}


# ---- Numbers ----

def parse_number(text: str) -> Fraction | None:
    """'1 1/2' -> 3/2; '½' -> 1/2; anything else -> None."""
    text = _normalize_fractions(text).strip()
    try:
        return sum((Fraction(part) for part in text.split()), Fraction(0)) if text else None
    except (ValueError, ZeroDivisionError):
        return None


def _normalize_fractions(text: str) -> str:
    for char, ascii_fraction in _FRACTION_CHARS.items():
        text = text.replace(char, f" {ascii_fraction}")
    return re.sub(r"[ \t]+", " ", text).strip()


def readable(value: Fraction) -> str:
    """A kitchen-friendly number: 3/2 -> '1 1/2'; 5/12 -> '3/8'; 12.6 -> '12 1/2'.

    Exact halves, thirds, quarters, and eighths are shown as they are. Anything else is
    rounded to the nearest eighth or third (or half, from 10 up) so nobody measures 0.4167.
    """
    if value <= 0:
        return "0"
    if value.denominator in (1, 2, 3, 4, 8):
        return format_number(value)
    if value >= 10:
        return format_number(Fraction(round(value * 2), 2))
    candidates = [Fraction(round(value * 8), 8), Fraction(round(value * 3), 3)]
    best = min(candidates, key=lambda c: (abs(c - value), c.denominator))
    return format_number(best if best > 0 else Fraction(1, 8))


# ---- Servings ----

def clean_servings(value) -> int | None:
    """A serving count from a form or the assistant, or None if it isn't 1-100."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if MIN_SERVINGS <= number <= MAX_SERVINGS else None


def servings_factor(servings: int | None, recipe_servings: int | None) -> Fraction:
    """How much to multiply the original amounts by (1 when the yield isn't known)."""
    if not servings or not recipe_servings:
        return Fraction(1)
    return Fraction(servings, recipe_servings)


def _unit_word(word: str, value: Fraction) -> str:
    """Singular or plural spelling of a unit word to suit the new amount."""
    bare = word.lower()
    singular = _SINGULARS.get(bare, bare if bare in _PLURALS else None)
    if singular is None:
        return word
    new = _PLURALS[singular] if value > 1 else singular
    return new.capitalize() if word[:1].isupper() else new


def _scale_rest(rest: str, value: Fraction, factor: Fraction) -> str:
    """Fix the unit's plural and scale a metric equivalent right after it ('cups (240 g)')."""
    # The unit may follow a package size: "1 (15 oz) can" -> "2 (15 oz) cans".
    match = re.match(r"^(\s*(?:\([^()]*\)\s*)?)([A-Za-z]+)(.*)$", rest, re.S)
    if match and _unit_lookup(match[2]):
        rest = f"{match[1]}{_unit_word(match[2], value)}{match[3]}"
    paren = _PAREN_METRIC_RE.match(rest)
    if paren:
        number = Fraction(paren["n"]) * factor
        shown = str(round(number)) if number >= 10 else f"{float(number):.1f}".rstrip("0").rstrip(".")
        rest = f"{paren['lead']}({shown} {paren['u']}){rest[paren.end():]}"
    return rest


def _spoon_text(total_tsp: Fraction) -> str:
    """US spoons/cups in the most readable form: 24 tsp -> '1/2 cup'; 3/8 tsp -> '3/8 tsp'."""
    if total_tsp < 3:
        return f"{readable(total_tsp)} tsp"
    return _volume_text(Fraction(round(total_tsp * 4), 4))


def scale_amount(amount: str, factor: Fraction) -> str:
    """Scale an amount as written: '1 lb' x2 -> '2 lbs'; 'to taste' and '' stay as they are."""
    if factor == 1 or not amount or not amount.strip():
        return amount
    text = _normalize_fractions(amount)
    match = _LEAD_RE.match(text)
    if not match:
        return amount   # "a pinch", "to taste", "some": nothing to multiply
    low = parse_number(match["a"])
    high = parse_number(match["b"]) if match["b"] else None
    if low is None or low <= 0:
        return amount
    rest = match["rest"]

    # Plain spoon and cup amounts are re-expressed: 8 x 1 tbsp reads better as 1/2 cup.
    if high is None and re.fullmatch(r"\s*[A-Za-z.]+\s*", rest):
        qty, unit, raw = parse_amount(text)
        if qty is not None and unit in _SPOON_TSP and not raw:
            return _spoon_text(qty * _SPOON_TSP[unit] * factor)

    new_low = low * factor
    if high is not None:
        new_high = high * factor
        sep = " to " if match["sep"] == "to" else match["sep"]
        return f"{readable(new_low)}{sep}{readable(new_high)}{_scale_rest(rest, new_high, factor)}"
    return f"{readable(new_low)}{_scale_rest(rest, new_low, factor)}"


def split_line(amount: str, name: str) -> tuple[str, str]:
    """(amount, name), pulling a quantity out of the name when the amount box is empty."""
    amount, name = (amount or "").strip(), (name or "").strip()
    if not amount:
        found, rest = split_ingredient(name)
        if found:
            return found, rest
    return amount, name


def scale_ingredients(ingredients, factor: Fraction) -> list[dict]:
    """Each ingredient with its original and scaled amount, in recipe order.

    `ingredients` are rows/dicts with amount and name (and optionally id).
    """
    lines = []
    for index, ing in enumerate(ingredients):
        amount, name = split_line(ing["amount"], ing["name"])
        scaled = scale_amount(amount, factor)
        lines.append({
            "index": index, "id": _get(ing, "id"), "original_amount": ing["amount"], "original_name": ing["name"],
            "amount": scaled, "name": name, "scaled": scaled != amount,
        })
    return lines


def _get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


# ---- Instructions ----

_STEP_NUM_RE = re.compile(rf"(?<![\w/.])(?P<qty>{_NUM}|[½⅓⅔¼¾⅛⅜⅝⅞]|\d+[½⅓⅔¼¾⅛⅜⅝⅞])(?![\d/])")
_FILLER = {"of", "the", "your", "reserved", "remaining", "a", "an"}


def _noun(name: str) -> str:
    """The word that names an ingredient: 'chicken breasts, cubed' -> 'breast'."""
    key = normalize_name(name)[0]
    return key.split()[-1] if key else ""


def _word_matches(word: str, noun: str) -> bool:
    word = re.sub(r"[^a-z-]", "", word.lower())
    if not word or not noun:
        return False
    return word == noun or normalize_name(word)[0].split()[-1:] == [noun]


def scale_step(body: str, ingredients, factor: Fraction) -> str:
    """Keep quantities that an instruction repeats in line with the scaled ingredient list.

    '2 cups breadcrumbs' in a step becomes '4 cups breadcrumbs' when the ingredient list
    says 2 cups breadcrumbs and the recipe is doubled. A number changes only when it equals
    one ingredient's amount, uses the same unit, and is followed by that ingredient's name,
    so oven temperatures, cooking times, pan sizes, and anything unclear stay as written.
    """
    if factor == 1 or not body:
        return body
    candidates = []
    for ing in ingredients:
        amount, name = split_line(ing["amount"], ing["name"])
        qty, unit, raw = parse_amount(amount)
        noun = _noun(name)
        if qty is not None and not raw and noun:
            candidates.append((qty, unit, noun, scale_amount(amount, factor)))
    if not candidates:
        return body

    pieces, last = [], 0
    for match in _STEP_NUM_RE.finditer(body):
        qty = parse_number(match["qty"])
        if qty is None:
            continue
        after = body[match.end():]
        unit_match = re.match(r"\s*([A-Za-z]+\.?(?:\s+oz)?)", after)
        step_unit = _unit_lookup(unit_match[1]) if unit_match else None
        tail = after[unit_match.end():] if (unit_match and step_unit) else after
        words = [w for w in re.findall(r"[A-Za-z-]+", tail[:60])][:5]
        following = [w for w in words if w.lower() not in _FILLER][:3]
        hits = {scaled for (q, u, noun, scaled) in candidates
                if q == qty and u == step_unit and any(_word_matches(w, noun) for w in following)}
        if len(hits) != 1:
            continue
        end = match.end() + (unit_match.end() if (unit_match and step_unit) else 0)
        pieces.append(body[last:match.start()])
        pieces.append(re.sub(r"\s*\([^()]*\)", "", hits.pop()))
        last = end
    pieces.append(body[last:])
    return "".join(pieces)


# ---- Unit conversion ----

@dataclass
class Conversion:
    kind: str     # "ok", "needs_ingredient", "no_reliable", "unknown"
    text: str


# Millilitres per unit (US customary volume) and grams per unit (weight).
_ML = {"tsp": Fraction("4.92892159375"), "tbsp": Fraction("14.78676478125"), "cup": Fraction("236.5882365"),
       "pint": Fraction("473.176473"), "quart": Fraction("946.352946"), "gallon": Fraction("3785.411784"),
       "fl oz": Fraction("29.5735295625"), "ml": Fraction(1), "l": Fraction(1000)}
_GRAMS = {"g": Fraction(1), "kg": Fraction(1000), "oz": Fraction("28.349523125"), "lb": Fraction("453.59237")}

# Grams in one US cup, for ingredients with a dependable standard figure
# (King Arthur Baking's ingredient weight chart and USDA values). Salt is left out on
# purpose: a cup of one kosher salt weighs nearly twice as much as another.
_GRAMS_PER_CUP = {
    "water": 237, "milk": 242, "heavy cream": 238, "buttermilk": 242,
    "flour": 120, "bread flour": 120, "whole wheat flour": 113, "cake flour": 113,
    "sugar": 200, "granulated sugar": 200, "white sugar": 200,
    "brown sugar": 213, "light brown sugar": 213, "dark brown sugar": 213,
    "powdered sugar": 113, "confectioner sugar": 113, "confectioners sugar": 113,
    "butter": 227, "unsalted butter": 227, "salted butter": 227,
    "honey": 336, "maple syrup": 312, "vegetable oil": 198, "olive oil": 200, "canola oil": 198,
    "rolled oat": 89, "oat": 89, "white rice": 198, "rice": 198, "cocoa powder": 84,
    "breadcrumb": 113, "dry breadcrumb": 113, "panko": 50, "panko breadcrumb": 50,
    "sour cream": 227, "plain yogurt": 227, "yogurt": 227,
}

UNIT_NAMES = {"tsp": "teaspoon", "tbsp": "tablespoon", "cup": "cup", "pint": "pint", "quart": "quart",
              "gallon": "gallon", "fl oz": "fluid ounce", "ml": "milliliter", "l": "liter",
              "g": "gram", "kg": "kilogram", "oz": "ounce", "lb": "pound"}


def conversion_unit(text: str) -> str | None:
    """'US quarts' -> 'quart'; 'grams' -> 'g'; 'fluid ounces' -> 'fl oz'; unknown -> None."""
    words = re.sub(r"[^a-z ]", " ", (text or "").lower()).split()
    words = [w for w in words if w not in {"us", "u", "s", "a", "an", "one", "the", "liquid", "american", "of"}]
    if not words:
        return None
    joined = " ".join(words)
    if joined in ("fluid ounce", "fluid ounces", "fl oz"):
        return "fl oz"
    if joined in ("c",):
        return "cup"
    unit = _unit_lookup(joined) or (_unit_lookup(words[-1]) if len(words) == 1 else None)
    return unit if unit in _ML or unit in _GRAMS else None


def _plural_name(unit: str, value: Fraction | float) -> str:
    name = UNIT_NAMES[unit]
    return name if value == 1 else name + "s"


def _approx(value: Fraction, unit: str) -> str:
    if unit in ("ml", "g") or value >= 100:
        return str(round(value))
    if unit in ("l", "kg"):
        return f"{float(value):.2f}".rstrip("0").rstrip(".")
    return readable(value)


def _density(ingredient: str) -> Fraction | None:
    key = normalize_name(ingredient)[0] if ingredient else ""
    for candidate in (key, " ".join(key.split()[-2:]), key.split()[-1] if key else ""):
        if candidate in _GRAMS_PER_CUP:
            return Fraction(_GRAMS_PER_CUP[candidate]) / _ML["cup"]   # grams per ml
    return None


def convert(qty: Fraction, from_unit: str, to_unit: str, ingredient: str = "") -> Conversion:
    """Convert a kitchen quantity. Weight and volume only mix for a known ingredient."""
    source = f"{readable(qty)} {_plural_name(from_unit, qty)}"
    from_us = from_unit in _UNIT_TABLE and _UNIT_TABLE[from_unit][1] == "volume"
    to_us = to_unit in _UNIT_TABLE and _UNIT_TABLE[to_unit][1] == "volume"
    if from_us and to_us:
        # US customary volumes are exact multiples of a teaspoon.
        result = qty * _UNIT_TABLE[from_unit][2] / _UNIT_TABLE[to_unit][2]
        return Conversion("ok", f"{_us(source, from_unit)} = {readable(result)} {_us_name(to_unit, result)}.")
    if from_unit in _ML and to_unit in _ML:
        result = qty * _ML[from_unit] / _ML[to_unit]
        exact = result.denominator in (1, 2, 4, 8)
        return Conversion("ok", f"{_us(source, from_unit)} is {'' if exact else 'about '}"
                                f"{_approx(result, to_unit)} {_us_name(to_unit, result)}.")
    if from_unit in _GRAMS and to_unit in _GRAMS:
        result = qty * _GRAMS[from_unit] / _GRAMS[to_unit]
        exact = result.denominator == 1
        return Conversion("ok", f"{source} is {'' if exact else 'about '}{_approx(result, to_unit)} "
                                f"{_plural_name(to_unit, result)}.")
    if (from_unit in _ML) == (to_unit in _ML):
        return Conversion("unknown", "I can't convert between those units.")

    # Weight <-> volume depends on the ingredient.
    if not ingredient.strip():
        return Conversion("needs_ingredient",
                          f"Which ingredient is it? {source.capitalize()} of flour and of sugar fill different "
                          "amounts of space, so I need to know before converting between weight and volume.")
    density = _density(ingredient)
    if density is None:
        return Conversion("no_reliable",
                          f"I don't have a reliable weight-to-volume figure for {ingredient.strip()}, so I'd "
                          "rather not guess. A kitchen scale gives the exact amount.")
    if from_unit in _GRAMS:
        ml = qty * _GRAMS[from_unit] / density
        result = ml / _ML[to_unit]
    else:
        grams = qty * _ML[from_unit] * density
        result = grams / _GRAMS[to_unit]
    return Conversion("ok", f"{source.capitalize()} of {ingredient.strip()} is about {_approx(result, to_unit)} "
                            f"{_us_name(to_unit, result)} (an estimate; measure by weight for accuracy).")


def _us_name(unit: str, value) -> str:
    name = _plural_name(unit, value)
    return f"US {name}" if unit in ("quart", "pint", "gallon", "fl oz") else name


def _us(text: str, unit: str) -> str:
    """'1 quart' -> '1 US quart' for units that differ between US and imperial measures."""
    if unit in ("quart", "pint", "gallon", "fl oz"):
        number, _, rest = text.partition(" ")
        return f"{number} US {rest}"
    return text
