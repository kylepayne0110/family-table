"""Serving math, readable fractions, instruction quantities, unit conversion, and request parsing."""

# Fraction gives exact scale factors.
from fractions import Fraction

# pytest provides parametrize.
import pytest

# Project modules.
from app import assistant
from app.scaling import convert, readable, scale_amount, scale_ingredients, scale_step, servings_factor


@pytest.mark.parametrize("amount, factor, expected", [
    ("1 lb", 2, "2 lbs"),
    ("2 pounds", Fraction(1, 2), "1 pound"),
    ("1 1/2 lbs", 2, "3 lbs"),
    ("1½ cups", 2, "3 cups"),
    ("1.5 cups", 2, "3 cups"),
    ("2 cups (240g)", 2, "4 cups (480 g)"),
    ("1 tbsp", 8, "1/2 cup"),
    ("1/8 tsp", Fraction(3, 2), "1/4 tsp"),
    ("2-3 cups", 2, "4-6 cups"),
    ("1 (15 oz) can", 2, "2 (15 oz) cans"),
    ("2 cloves", Fraction(1, 2), "1 clove"),
    ("3", Fraction(5, 4), "3 3/4"),
    ("to taste", 2, "to taste"),
    ("a pinch", 3, "a pinch"),
    ("", 2, ""),
    ("1 lb", 1, "1 lb"),
])
def test_scale_amount(amount, factor, expected):
    assert scale_amount(amount, Fraction(factor)) == expected


def test_readable_fractions():
    assert readable(Fraction(3, 2)) == "1 1/2"
    assert readable(Fraction(5, 12)) == "3/8"          # nearest eighth or third, never 0.42
    assert readable(Fraction(126, 10)) == "12 1/2"
    assert readable(Fraction(1, 100)) == "1/8"


def test_factor_needs_a_known_yield():
    assert servings_factor(8, 4) == 2
    assert servings_factor(8, None) == 1
    assert servings_factor(None, 4) == 1


def test_amount_typed_in_the_name_box_is_scaled():
    line = scale_ingredients([{"amount": "", "name": "2 cups flour"}], Fraction(2))[0]
    assert (line["amount"], line["name"]) == ("4 cups", "flour")


def test_steps_follow_the_ingredient_list_only_where_clear():
    ingredients = [{"amount": "2 cups", "name": "breadcrumbs"}, {"amount": "2", "name": "eggs"},
                   {"amount": "1 cup", "name": "milk"}, {"amount": "1 cup", "name": "water"}]
    step = ("Heat to 350°F. Beat 2 eggs, add 2 cups breadcrumbs and 1 cup milk. "
            "Bake 2 hours in a 9-inch pan, then rest 2 minutes. Add 1 cup of the water.")
    assert scale_step(step, ingredients, Fraction(2)) == (
        "Heat to 350°F. Beat 4 eggs, add 4 cups breadcrumbs and 2 cups milk. "
        "Bake 2 hours in a 9-inch pan, then rest 2 minutes. Add 2 cups of the water.")


def test_step_quantity_that_differs_from_the_list_is_left_alone():
    ingredients = [{"amount": "2 cups", "name": "flour"}]
    assert scale_step("Add 1 cup flour, then the rest.", ingredients, Fraction(2)) == "Add 1 cup flour, then the rest."


def test_conversions():
    assert convert(Fraction(1), "quart", "cup").text == "1 US quart = 4 cups."
    assert convert(Fraction(3), "tsp", "tbsp").text == "3 teaspoons = 1 tablespoon."
    assert convert(Fraction(2), "cup", "ml").text == "2 cups is about 473 milliliters."
    assert convert(Fraction(1), "lb", "g").text == "1 pound is about 454 grams."
    assert convert(Fraction(500), "g", "cup").kind == "needs_ingredient"
    assert convert(Fraction(500), "g", "cup", "kosher salt").kind == "no_reliable"
    sugar = convert(Fraction(1), "cup", "g", "granulated sugar")
    assert sugar.kind == "ok" and "about 200 grams" in sugar.text


@pytest.mark.parametrize("text, expected", [
    ("How many cups are in one US quart?", (Fraction(1), "quart", "cup", "")),
    ("How many cups is 500 grams?", (Fraction(500), "g", "cup", "")),
    ("how many grams in a cup of flour", (Fraction(1), "cup", "g", "flour")),
    ("convert 2 tablespoons to teaspoons", (Fraction(2), "tbsp", "tsp", "")),
    ("How much flour do I need tonight?", None),
])
def test_parse_conversion(text, expected):
    assert assistant.parse_conversion(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Make this for eight tonight.", 8),
    ("Can you scale it to 6 servings?", 6),
    ("We're cooking for 10", 10),
    ("Double it", 8),
    ("Reset to the original servings", "reset"),
    ("Bake for 20 minutes?", None),
    ("How much flour for 8 people?", None),      # a question, not a change
    ("Cook for 20 minutes", None),
])
def test_parse_servings_request(text, expected):
    assert assistant.parse_servings_request(text, current=4) == expected


@pytest.mark.parametrize("text, expected", [
    ("From now on, use panko instead of regular breadcrumbs.", ("regular breadcrumbs", "panko")),
    ("Replace the butter with olive oil", ("the butter", "olive oil")),
    ("substitute honey for sugar", ("sugar", "honey")),
    ("What do I do after coating the chicken?", None),
])
def test_parse_substitution(text, expected):
    assert assistant.parse_substitution(text) == expected


def test_substitution_changes_only_wording():
    recipe = {"servings": 4, "ingredients": [{"id": 1, "amount": "2 cups", "name": "Breadcrumbs"}],
              "steps": [{"id": 9, "title": "", "body": "Roll in breadcrumbs. Breadcrumbs should coat well."}]}
    changes = assistant.substitution_changes(recipe, "regular breadcrumbs", "panko")
    assert changes == [
        {"kind": "ingredient", "id": 1, "amount": "2 cups", "before": "Breadcrumbs", "after": "Panko"},
        {"kind": "step_body", "id": 9, "step": 1, "before": "Roll in breadcrumbs. Breadcrumbs should coat well.",
         "after": "Roll in panko. Panko should coat well."},
    ]
