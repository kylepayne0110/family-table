"""Turn a recipe web page into pre-filled Add Recipe form values.

Pipeline: safe_fetch downloads the page -> extractors read structured recipe
data -> results are mapped onto the existing form fields. Nothing is saved
here; the user reviews the form and saves it the normal way.
"""

# html unescapes entities such as &amp; in scraped text.
import html

# json parses JSON-LD script blocks.
import json

# re handles durations, amounts, and text cleanup.
import re

# time ages out abandoned temporary photos.
import time

# Callable types the list of extractors.
from collections.abc import Callable

# dataclass describes an extracted recipe.
from dataclasses import dataclass, field, fields

# Path provides filesystem utilities.
from pathlib import Path

# urljoin makes relative image links absolute.
from urllib.parse import urljoin, urlsplit

# BeautifulSoup finds JSON-LD blocks in the page.
from bs4 import BeautifulSoup

# recipe-scrapers knows hundreds of recipe sites plus generic schema.org data.
from recipe_scrapers import scrape_html

# Project modules.
from app.images import ImageError, process_photo
from app.meals import LIMITS, MAX_INGREDIENTS, MAX_STEPS
from app.safe_fetch import FetchError, fetch


MAX_HTML_BYTES = 5 * 1024 * 1024
TEMP_PHOTO_MAX_AGE = 6 * 3600  # seconds before an unsaved imported photo is deleted


class RecipeImportError(Exception):
    """An import that failed; the message is safe to show to the user."""


@dataclass
class ExtractedRecipe:
    """Raw recipe data as found on the page (no guessing)."""

    name: str = ""
    description: str = ""
    cuisine: str = ""
    category: str = ""
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    yields: str = ""
    ingredients: list[str] = field(default_factory=list)
    steps: list[tuple[str, str]] = field(default_factory=list)  # (title, body)
    image_url: str = ""

    def has_recipe(self) -> bool:
        return bool(self.name and (self.ingredients or self.steps))


# ---- Small text helpers ----

def clean_text(value) -> str:
    """Strip HTML tags/entities and squeeze whitespace."""
    if value is None:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value)))
    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, limit: int) -> str:
    """Trim to a limit at a word boundary."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def parse_duration(value) -> int | None:
    """ISO 8601 duration ('PT1H30M') or a number -> whole minutes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:[\d.]+S)?)?", str(value).strip(), re.I)
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(g or 0) for g in match.groups())
    total = days * 1440 + hours * 60 + minutes
    return total or None


# Leading quantity: "2", "1 1/2", "2 and 1/4", "1½", "0.5", "2-3", "1 to 2".
_QTY = r"(?:\d+\s+(?:and\s+)?\d+/\d+|\d+(?:[.,]\d+)?(?:/\d+)?\s*[½⅓⅔¼¾⅛⅜⅝⅞]?|[½⅓⅔¼¾⅛⅜⅝⅞])"
_UNITS = (
    r"cups?|c\.|tablespoons?|tbsps?|tbs|teaspoons?|tsps?|pounds?|lbs?|ounces?|oz|"
    r"fl\.? oz|grams?|g|kilograms?|kg|milliliters?|millilitres?|ml|liters?|litres?|l|"
    r"pints?|quarts?|qts?|gallons?|cans?|jars?|cloves?|pinch(?:es)?|dash(?:es)?|sticks?|"
    r"slices?|packages?|pkgs?|bunch(?:es)?|sprigs?|heads?|handfuls?|large|medium|small"
)
_PAREN = r"(?:\s*\([^)]{1,30}\))?"  # optional "(15 oz)" or "(281g)"
_AMOUNT_RE = re.compile(
    rf"^\s*(?P<amount>{_QTY}(?:\s*(?:-|–|to)\s*{_QTY})?"   # quantity or range
    rf"{_PAREN}(?:\s*(?:{_UNITS})\b\.?)?{_PAREN})"         # optional unit, with weights either side
    rf"\s+(?:of\s+)?(?P<name>\S.*)$",                     # "8 tablespoons of butter" -> "butter"
    re.I,
)


def split_ingredient(text: str) -> tuple[str, str]:
    """'2 cups flour' -> ('2 cups', 'flour'); unparseable lines stay whole."""
    # Drop leading bullets/checkbox glyphs some sites include ("▢ 1 cup...").
    text = re.sub(r"^[^\w(½⅓⅔¼¾⅛⅜⅝⅞]+", "", clean_text(text))
    match = _AMOUNT_RE.match(text)
    if match and len(match["amount"]) <= LIMITS["amount"]:
        return match["amount"].strip(), match["name"].strip()
    return "", text


# ---- Extractor 1: recipe-scrapers ----

def _safe(call, default=None):
    """recipe-scrapers raises when a field is missing; treat that as blank."""
    try:
        value = call()
    except Exception:  # library raises many exception types for missing data
        return default
    return default if value in (None, "") else value


def extract_with_recipe_scrapers(page_html: str, url: str) -> ExtractedRecipe | None:
    """Use recipe-scrapers on HTML we already downloaded (it never fetches by itself)."""
    try:
        scraper = scrape_html(page_html, org_url=url, supported_only=False)
    except Exception:
        return None

    image = _safe(scraper.image, "")
    return ExtractedRecipe(
        name=clean_text(_safe(scraper.title, "")),
        description=clean_text(_safe(scraper.description, "")),
        cuisine=clean_text(_safe(scraper.cuisine, "")),
        category=clean_text(_safe(scraper.category, "")),
        prep_minutes=_safe(scraper.prep_time),
        cook_minutes=_safe(scraper.cook_time),
        total_minutes=_safe(scraper.total_time),
        yields=clean_text(_safe(scraper.yields, "")),
        ingredients=[clean_text(i) for i in _safe(scraper.ingredients, []) if clean_text(i)],
        steps=[("", clean_text(s)) for s in _safe(scraper.instructions_list, []) if clean_text(s)],
        image_url=image if isinstance(image, str) else "",
    )


# ---- Extractor 2: our own schema.org JSON-LD reader ----

def _is_recipe(node) -> bool:
    kind = node.get("@type") if isinstance(node, dict) else None
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.split("/")[-1].lower() == "recipe" for k in kinds)


def _find_recipe(node):
    """Search a JSON-LD tree (lists, @graph, mainEntity...) for a Recipe object."""
    if isinstance(node, list):
        for item in node:
            found = _find_recipe(item)
            if found:
                return found
    elif isinstance(node, dict):
        if _is_recipe(node):
            return node
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item"):
            if key in node:
                found = _find_recipe(node[key])
                if found:
                    return found
    return None


def _image_url(value) -> str:
    """schema.org image may be a string, an ImageObject, or a list of either."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return value if isinstance(value, str) else ""


def _steps(value, section: str = "") -> list[tuple[str, str]]:
    """Flatten recipeInstructions: text, HowToStep, HowToSection, or nested lists."""
    steps: list[tuple[str, str]] = []
    if isinstance(value, str):
        # One block of text: split into lines/paragraphs.
        parts = [clean_text(p) for p in re.split(r"\n+|<br\s*/?>|</p>", value) if clean_text(p)]
        steps = [("", p) for p in parts]
    elif isinstance(value, list):
        for item in value:
            steps.extend(_steps(item))
    elif isinstance(value, dict):
        kind = str(value.get("@type", ""))
        if kind.endswith("HowToSection"):
            inner = _steps(value.get("itemListElement", []))
            name = clean_text(value.get("name"))
            # The section name becomes the title of its first step.
            if inner and name and not inner[0][0]:
                inner[0] = (name, inner[0][1])
            steps.extend(inner)
        else:
            body = clean_text(value.get("text") or value.get("description") or "")
            title = clean_text(value.get("name"))
            if title and body.lower().startswith(title.rstrip(".").lower()):
                title = ""  # many sites repeat the text as the name
            if body:
                steps.append((title, body))
    return steps


def _as_text(value) -> str:
    if isinstance(value, list):
        value = ", ".join(clean_text(v) for v in value if isinstance(v, (str, int)))
    return clean_text(value)


def extract_from_json_ld(page_html: str, url: str) -> ExtractedRecipe | None:
    """Read the first schema.org Recipe found in the page's JSON-LD blocks."""
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        try:
            data = json.loads(script.string or script.get_text() or "", strict=False)
        except (ValueError, TypeError):
            continue
        recipe = _find_recipe(data)
        if recipe is None:
            continue

        yields = recipe.get("recipeYield")
        if isinstance(yields, list):
            yields = yields[0] if yields else ""
        ingredients = recipe.get("recipeIngredient") or recipe.get("ingredients") or []
        if isinstance(ingredients, str):
            ingredients = [ingredients]
        return ExtractedRecipe(
            name=clean_text(recipe.get("name")),
            description=clean_text(recipe.get("description")),
            cuisine=_as_text(recipe.get("recipeCuisine")),
            category=_as_text(recipe.get("recipeCategory")),
            prep_minutes=parse_duration(recipe.get("prepTime")),
            cook_minutes=parse_duration(recipe.get("cookTime")),
            total_minutes=parse_duration(recipe.get("totalTime")),
            yields=clean_text(yields),
            ingredients=[clean_text(i) for i in ingredients if clean_text(i)],
            steps=_steps(recipe.get("recipeInstructions")),
            image_url=_image_url(recipe.get("image")),
        )
    return None


# Extractors are tried in order; missing fields are filled from later ones.
# An optional LLM-based extractor could be appended here in the future.
EXTRACTORS: list[Callable[[str, str], ExtractedRecipe | None]] = [
    extract_with_recipe_scrapers,
    extract_from_json_ld,
]


def extract_recipe(page_html: str, url: str) -> ExtractedRecipe | None:
    """Run every extractor and merge their results, first one winning."""
    merged: ExtractedRecipe | None = None
    json_ld_steps: list[tuple[str, str]] = []
    for extractor in EXTRACTORS:
        result = extractor(page_html, url)
        if result is None:
            continue
        if extractor is extract_from_json_ld:
            json_ld_steps = result.steps
        if merged is None:
            merged = result
            continue
        for f in fields(ExtractedRecipe):
            if getattr(merged, f.name) in ("", None, []):
                setattr(merged, f.name, getattr(result, f.name))

    # recipe-scrapers flattens section and step names into extra "steps";
    # our JSON-LD reader keeps them as titles, so its steps win when present.
    if merged and json_ld_steps:
        merged.steps = json_ld_steps
    return merged if merged and merged.has_recipe() else None


# ---- Downloading ----

def _decode(result) -> str:
    """Bytes -> text using the declared or <meta> charset, falling back to UTF-8."""
    charset = result.charset
    if not charset:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", result.body[:4096], re.I)
        charset = meta.group(1).decode() if meta else "utf-8"
    try:
        return result.body.decode(charset, errors="replace")
    except LookupError:
        return result.body.decode("utf-8", errors="replace")


FETCH_MESSAGES = {
    "invalid_url": "That doesn't look like a web address. Paste the full link, starting with https://.",
    "blocked": "For safety, Family Table can only import from public websites.",
    "dns": "We couldn't find that website. Check the link for typos.",
    "timeout": "That website took too long to respond. Try again in a moment.",
    "too_large": "That page is too large to import. You can enter the recipe manually.",
    "redirects": "That link redirected too many times. Try the recipe page's own address.",
}


def friendly_fetch_error(error: FetchError) -> str:
    if error.reason == "http":
        if error.status in (401, 403, 429):
            return ("That website wouldn't let us read the page. It may block automated "
                    "access or need a login. You can enter the recipe manually.")
        if error.status == 404:
            return "That page doesn't exist anymore (error 404). Check the link."
        return f"That website returned an error ({error.status}). Try again later or enter it manually."
    return FETCH_MESSAGES.get(error.reason, "We couldn't load that page. Check the link or try again later.")


def download_page(url: str) -> tuple[str, str]:
    """Fetch a recipe page safely; return (final URL, HTML text)."""
    try:
        result = fetch(url, MAX_HTML_BYTES)
    except FetchError as exc:
        raise RecipeImportError(friendly_fetch_error(exc)) from exc
    if result.content_type and "html" not in result.content_type:
        raise RecipeImportError("That link isn't a web page. Paste the link to the recipe page itself.")
    return result.url, _decode(result)


def download_photo(image_url: str, page_url: str, temp_dir: Path, max_bytes: int) -> str | None:
    """Fetch and process the recipe photo into the temporary folder, or None."""
    if not image_url:
        return None
    try:
        result = fetch(urljoin(page_url, image_url), max_bytes, accept="image/webp,image/jpeg,image/png,image/*")
        return process_photo(result.body, temp_dir, max_bytes)
    except (FetchError, ImageError):
        return None


def cleanup_temp_photos(temp_dir: Path, max_age: int = TEMP_PHOTO_MAX_AGE) -> None:
    """Delete imported photos that were never saved with a recipe."""
    if not temp_dir.is_dir():
        return
    cutoff = time.time() - max_age
    for path in temp_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


# ---- Mapping onto the Add Recipe form ----

def _servings(yields: str) -> int | None:
    """'4 servings' / 'Serves 4-6' / '4' -> 4; '24 cookies' -> None (not servings)."""
    match = re.search(r"\d+", yields)
    if not match or not (1 <= int(match.group()) <= 100):
        return None
    if re.fullmatch(r"\s*\d+\s*", yields) or re.search(r"serv|people|person|portion", yields, re.I):
        return int(match.group())
    return None


def to_form_values(recipe: ExtractedRecipe, source_url: str) -> tuple[dict, list[str]]:
    """Map extracted data onto form fields; return (values, notes for the user)."""
    notes: list[str] = []

    prep, cook = recipe.prep_minutes, recipe.cook_minutes
    if cook is None and recipe.total_minutes:
        if prep is not None and recipe.total_minutes > prep:
            cook = recipe.total_minutes - prep
        elif prep is None:
            cook = recipe.total_minutes
            notes.append("Only a total time was listed, so we put it under Cook.")
    prep = prep if prep is None or prep <= 1440 else None
    cook = cook if cook is None or cook <= 1440 else None

    servings = _servings(recipe.yields)
    if recipe.yields and servings is None:
        notes.append(f"The recipe makes “{shorten(recipe.yields, 40)}”; add servings if you like.")

    ingredients = [split_ingredient(line) for line in recipe.ingredients[:MAX_INGREDIENTS]]
    ingredients = [{"amount": a, "name": shorten(n, LIMITS["ingredient"])} for a, n in ingredients if n]
    steps = [{"title": shorten(t, LIMITS["step_title"]), "body": shorten(b, LIMITS["step_body"])}
             for t, b in recipe.steps[:MAX_STEPS]]
    if not ingredients:
        notes.append("We didn't find an ingredient list. Add the ingredients below.")
    if not steps:
        notes.append("We didn't find any instructions. Add the steps below.")

    cuisine = recipe.cuisine.split(",")[0].strip()
    # Only map a category when the page clearly says so; otherwise keep the default.
    is_soup = "soup" in f"{recipe.category} {recipe.name}".lower()

    values = {
        "name": shorten(recipe.name, LIMITS["name"]),
        "description": shorten(recipe.description, LIMITS["description"]),
        "category": "soups" if is_soup else "weeknight",
        "cuisine": cuisine if len(cuisine) <= LIMITS["cuisine"] else "",
        "prep_minutes": "" if prep is None else str(prep),
        "cook_minutes": "" if cook is None else str(cook),
        "servings": "" if servings is None else str(servings),
        "card_tag": "", "last_made": "", "note_author": "", "note_body": "",
        "source_url": source_url if len(source_url) <= LIMITS["source_url"] else "",
        "imported_photo": "",
        "ingredients": ingredients,
        "steps": steps,
    }
    return values, notes


def import_recipe(url: str, temp_dir: Path, max_photo_bytes: int) -> tuple[dict, list[str]]:
    """Full import: download, extract, fetch photo. Raises RecipeImportError."""
    url = url.strip()
    if not urlsplit(url).scheme and "." in url:
        url = "https://" + url  # allow "example.com/recipe"

    final_url, page_html = download_page(url)
    recipe = extract_recipe(page_html, final_url)
    del page_html  # don't keep large pages in memory longer than needed
    if recipe is None:
        raise RecipeImportError(
            "We couldn't find a recipe on that page. You can try another link or enter it manually."
        )

    values, notes = to_form_values(recipe, final_url)
    cleanup_temp_photos(temp_dir)
    photo = download_photo(recipe.image_url, final_url, temp_dir, max_photo_bytes)
    if photo:
        values["imported_photo"] = photo
    elif recipe.image_url:
        notes.append("We couldn't download the recipe's photo. You can upload one yourself.")
    return values, notes
