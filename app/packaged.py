# Store-bought ("packaged") meals planned for one dinner: barcode checks, the product
# lookup, and saving what the family confirmed. A packaged meal belongs to one planned
# dinner and never becomes a recipe in the recipe box.
#
# Cooking directions are only ever shown as they came: copied word for word from the
# product database, typed by the family, or read off a photo of the box. Nothing here
# (and no AI) fills in temperatures, times, or missing steps.

# json reads the product database's answer.
import json

# re checks barcodes and finds oven/microwave headings.
import re

# sqlite3 types the database connection.
import sqlite3

# threading guards the small lookup cache; time expires its entries.
import threading
import time

# dataclass describes a lookup result.
from dataclasses import asdict, dataclass

# date identifies the week of a planned dinner.
from datetime import date

# Project modules.
from app.database import write_transaction
from app.planner import _bump_plan, _check_day, ensure_week
from app.safe_fetch import FetchError, fetch


OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields={fields}"
OFF_PAGE_URL = "https://world.openfoodfacts.org/product/{code}"
OFF_FIELDS = "code,product_name,product_name_en,generic_name_en,brands,quantity,preparation_en,preparation"
# Open Food Facts asks API clients to name themselves; no personal details are sent.
USER_AGENT = "FamilyTable/1.0 (self-hosted household meal planner)"
MAX_RESPONSE = 2 * 1024 * 1024
CACHE_SECONDS = 24 * 60 * 60

LIMITS = {"name": 120, "brand": 80, "package_size": 40, "directions": 4000}
SOURCES = {"openfoodfacts", "typed", ""}


class PackagedError(Exception):
    """Something the family needs to fix (the message is safe to show)."""


class DayTaken(PackagedError):
    """Another dinner is already planned that day; a scan never replaces it silently."""

    def __init__(self, message: str, meal_id: int, name: str):
        super().__init__(message)
        self.meal_id = meal_id
        self.name = name


# ---- Barcodes ----

def clean_barcode(text: str) -> str:
    """Digits as printed under the barcode, spaces and dashes removed."""
    return re.sub(r"[\s-]", "", text or "")


def _check_digit_ok(code: str) -> bool:
    digits = [int(d) for d in code]
    # GTIN check digit: weights 3,1,3,1... from the right, excluding the check digit itself.
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def expand_upc_e(code: str) -> str | None:
    """An 8-digit UPC-E (small packages) as the 12-digit UPC-A it stands for, or None."""
    if len(code) != 8 or not code.isdigit() or code[0] not in "01":
        return None
    s, d, check = code[0], code[1:7], code[7]
    last = d[5]
    if last in "012":
        body = f"{d[0]}{d[1]}{last}0000{d[2]}{d[3]}{d[4]}"
    elif last == "3":
        body = f"{d[0]}{d[1]}{d[2]}00000{d[3]}{d[4]}"
    elif last == "4":
        body = f"{d[0]}{d[1]}{d[2]}{d[3]}00000{d[4]}"
    else:
        body = f"{d[0]}{d[1]}{d[2]}{d[3]}{d[4]}0000{last}"
    full = f"{s}{body}{check}"
    return full if _check_digit_ok(full) else None


def normalize_barcode(text: str) -> str:
    """Cleaned digits; a UPC-E is expanded to UPC-A so the lookup finds the product."""
    code = clean_barcode(text)
    if len(code) == 8 and code.isdigit() and not _check_digit_ok(code):
        return expand_upc_e(code) or code
    return code


def barcode_problem(code: str) -> str | None:
    """Why a barcode can't be right, or None. UPC-A (12), EAN-13, EAN-8, and GTIN-14 are accepted."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return "Enter the 8, 12, or 13 digits printed under the barcode."
    if not _check_digit_ok(code):
        return "Those digits don't add up to a valid barcode. Check them against the package."
    return None


# ---- Directions ----

_HEADING = re.compile(
    r"(?im)(?:^|(?<=[.;:\n]))\s*(?P<head>(?:conventional\s+|regular\s+)?oven|microwave(?:\s+oven)?|air\s*fryer|stovetop)"
    r"\s*(?:cooking\s+)?(?:instructions|directions|method)?\s*(?:\([^)]*\))?\s*[:\-–]")


def split_directions(text: str) -> dict[str, str]:
    """Separate oven and microwave directions by the headings written on the package.

    The words are kept exactly; only the sections are told apart. Text without clear
    "Oven:" / "Microwave:" headings stays together under "other".
    """
    text = (text or "").strip()
    found = {"oven": [], "microwave": [], "other": []}
    if not text:
        return {k: "" for k in found}
    marks = list(_HEADING.finditer(text))
    if not marks:
        return {"oven": "", "microwave": "", "other": text}
    lead = text[:marks[0].start()].strip()
    if lead:
        found["other"].append(lead)
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()
        head = mark["head"].lower()
        section = "microwave" if "microwave" in head else "oven" if "oven" in head else "other"
        if section == "other":
            body = f"{mark['head'].strip()}: {body}"
        if body:
            found[section].append(body)
    return {k: "\n\n".join(v) for k, v in found.items()}


# ---- Looking a product up ----

@dataclass
class Product:
    found: bool
    barcode: str
    name: str = ""
    brand: str = ""
    package_size: str = ""
    oven: str = ""
    microwave: str = ""
    other: str = ""
    source: str = ""          # "openfoodfacts" when found
    source_url: str = ""
    error: str = ""           # set when the lookup itself failed (offline, timeout)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["has_directions"] = bool(self.oven or self.microwave or self.other)
        return data


_cache: dict[str, tuple[float, Product]] = {}
_cache_lock = threading.Lock()


def cached_product(code: str) -> Product | None:
    """A recent successful lookup for this barcode (used to tell copied directions from typed ones)."""
    with _cache_lock:
        hit = _cache.get(code)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _remember(code: str, product: Product) -> None:
    with _cache_lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[code] = (time.monotonic(), product)


def parse_open_food_facts(code: str, payload: dict) -> Product:
    """Turn an Open Food Facts product answer into the fields the family reviews."""
    if payload.get("status") != 1 or not isinstance(payload.get("product"), dict):
        return Product(found=False, barcode=code)
    p = payload["product"]
    text = lambda key: " ".join(str(p.get(key) or "").split())
    name = text("product_name_en") or text("product_name") or text("generic_name_en")
    brand = text("brands").split(",")[0].strip()
    directions = split_directions(str(p.get("preparation_en") or p.get("preparation") or ""))
    return Product(
        found=True, barcode=code, name=name[:LIMITS["name"]], brand=brand[:LIMITS["brand"]],
        package_size=text("quantity")[:LIMITS["package_size"]],
        oven=directions["oven"], microwave=directions["microwave"], other=directions["other"],
        source="openfoodfacts", source_url=OFF_PAGE_URL.format(code=str(p.get("code") or code)),
    )


def lookup(code: str, fetcher=None) -> Product:
    """Look a barcode up on Open Food Facts (free, no key). Never raises for network trouble."""
    cached = cached_product(code)
    if cached:
        return cached
    try:
        result = (fetcher or fetch)(OFF_PRODUCT_URL.format(code=code, fields=OFF_FIELDS), MAX_RESPONSE,
                         accept="application/json", user_agent=USER_AGENT)
        payload = json.loads(result.body)
    except FetchError as exc:
        if exc.status == 404:   # Open Food Facts answers 404 for unknown barcodes
            return Product(found=False, barcode=code)
        return Product(found=False, barcode=code, error="The product lookup didn't answer. You can still add it by hand.")
    except ValueError:
        return Product(found=False, barcode=code, error="The product lookup sent something unreadable. You can still add it by hand.")
    product = parse_open_food_facts(code, payload)
    if product.found:
        _remember(code, product)
    return product


# ---- Saving ----

def _clean(value, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    return text[:limit]


def _one_line(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def details_from_form(form) -> dict:
    """The reviewed product details and directions from a submitted form."""
    barcode = normalize_barcode(str(form.get("barcode") or ""))
    details = {
        "barcode": barcode,
        "name": _one_line(form.get("name"), LIMITS["name"]),
        "brand": _one_line(form.get("brand"), LIMITS["brand"]),
        "package_size": _one_line(form.get("package_size"), LIMITS["package_size"]),
        "oven_directions": _clean(form.get("oven"), LIMITS["directions"]),
        "microwave_directions": _clean(form.get("microwave"), LIMITS["directions"]),
        "other_directions": _clean(form.get("other"), LIMITS["directions"]),
    }
    if not details["name"]:
        raise PackagedError("Give the meal a name (it's on the front of the box).")
    if barcode and barcode_problem(barcode):
        raise PackagedError(barcode_problem(barcode))
    # Where the words came from: copied from the lookup only if they match it exactly.
    product = cached_product(barcode) if barcode else None
    details["product_source"] = "openfoodfacts" if product and product.name and \
        details["name"] == product.name else "manual"
    for section, key in (("oven", "oven_directions"), ("microwave", "microwave_directions"),
                         ("other", "other_directions")):
        text = details[key]
        copied = bool(product) and text and text == _clean(getattr(product, section), LIMITS["directions"])
        details[f"{section}_source"] = "" if not text else "openfoodfacts" if copied else "typed"
    return details


def add_packaged_meal(conn: sqlite3.Connection, week: date, day: int, details: dict, label_photo: str | None,
                      replace_meal_id: int | None, today: date | None) -> tuple[int, list[str]]:
    """Plan a packaged meal for a day. Returns (planned dinner id, label photos no longer used).

    If another dinner is already planned that day it's replaced only when the family
    confirmed replacing that exact dinner (replace_meal_id); otherwise DayTaken is raised.
    The shopping list is never touched.
    """
    _check_day(day, week, today)
    w = week.isoformat()
    with write_transaction(conn):
        ensure_week(conn, week)
        current = conn.execute(
            """SELECT p.id, COALESCE(r.name, pk.name) AS name FROM planned_meals p
               LEFT JOIN recipes r ON r.id = p.recipe_id LEFT JOIN packaged_meals pk ON pk.meal_id = p.id
               WHERE p.week_start = ? AND p.day = ?""",
            (w, day),
        ).fetchone()
        unused: list[str] = []
        if current is not None:
            if current["id"] != replace_meal_id:
                raise DayTaken(f"{current['name']} is already planned that day. Choose to replace it, "
                               "or pick another day.", current["id"], current["name"])
            unused = [r[0] for r in conn.execute(
                "SELECT label_photo FROM packaged_meals WHERE meal_id = ? AND label_photo IS NOT NULL", (current["id"],))]
            conn.execute("DELETE FROM planned_meals WHERE id = ?", (current["id"],))
        meal_id = conn.execute("INSERT INTO planned_meals (week_start, day, recipe_id) VALUES (?, ?, NULL)",
                               (w, day)).lastrowid
        conn.execute(
            """INSERT INTO packaged_meals (meal_id, barcode, name, brand, package_size, product_source,
                   oven_directions, oven_source, microwave_directions, microwave_source,
                   other_directions, other_source, label_photo)
               VALUES (:meal_id, :barcode, :name, :brand, :package_size, :product_source, :oven_directions,
                   :oven_source, :microwave_directions, :microwave_source, :other_directions, :other_source,
                   :label_photo)""",
            {**details, "meal_id": meal_id, "label_photo": label_photo},
        )
        _bump_plan(conn, week)
    return meal_id, unused


def get_packaged(conn: sqlite3.Connection, meal_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM packaged_meals WHERE meal_id = ?", (meal_id,)).fetchone()
    return dict(row) if row else None


def update_directions(conn: sqlite3.Connection, meal_id: int, form, new_photo: str | None,
                      remove_photo: bool) -> list[str]:
    """Add or fix directions (and the label photo) later. Returns label photos no longer used."""
    with write_transaction(conn):
        current = get_packaged(conn, meal_id)
        if current is None:
            raise PackagedError("That packaged meal isn't on the plan anymore.")
        values = {}
        for section in ("oven", "microwave", "other"):
            text = _clean(form.get(section), LIMITS["directions"])
            before = current[f"{section}_directions"]
            values[f"{section}_directions"] = text
            # Unchanged text keeps its source; anything edited counts as typed by the family.
            values[f"{section}_source"] = current[f"{section}_source"] if text == before else ("typed" if text else "")
        unused = []
        photo = current["label_photo"]
        if (new_photo or remove_photo) and photo:
            unused.append(photo)
            photo = None
        if new_photo:
            photo = new_photo
        conn.execute(
            """UPDATE packaged_meals SET oven_directions = :oven_directions, oven_source = :oven_source,
                   microwave_directions = :microwave_directions, microwave_source = :microwave_source,
                   other_directions = :other_directions, other_source = :other_source,
                   label_photo = :photo, updated_at = datetime('now') WHERE meal_id = :meal_id""",
            {**values, "photo": photo, "meal_id": meal_id},
        )
        row = conn.execute("SELECT week_start FROM planned_meals WHERE id = ?", (meal_id,)).fetchone()
        _bump_plan(conn, date.fromisoformat(row["week_start"]))
    return unused


def photos_for_day(conn: sqlite3.Connection, week: date, day: int) -> list[str]:
    """Label photos of a packaged meal planned that day (to delete after it's removed or replaced)."""
    return [r[0] for r in conn.execute(
        """SELECT pk.label_photo FROM packaged_meals pk JOIN planned_meals p ON p.id = pk.meal_id
           WHERE p.week_start = ? AND p.day = ? AND pk.label_photo IS NOT NULL""",
        (week.isoformat(), day),
    )]

