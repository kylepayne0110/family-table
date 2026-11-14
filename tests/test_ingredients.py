# Fraction makes expected quantities exact.
from fractions import Fraction

# pytest provides parametrize.
import pytest

# The module under test (pure functions, no database).
from app.ingredients import combine, format_quantity, matches_staple, name_key, needs_more, parse_amount, parse_line


def items(*recipes):
    """combine() input from (recipe_id, name, [(amount, ingredient), ...]) tuples."""
    return combine([(rid, rname, amount, ing) for rid, rname, lines in recipes for amount, ing in lines])


# ---- Amounts, fractions, and units ----

@pytest.mark.parametrize("text, qty, unit", [
    ("1 lb", 1, "lb"), ("2 lbs", 2, "lb"), ("1 lb.", 1, "lb"), ("2 pounds", 2, "lb"),
    ("1 ½ lbs", Fraction(3, 2), "lb"), ("1½ pounds", Fraction(3, 2), "lb"),
    ("3/4 cup", Fraction(3, 4), "cup"), ("1 1/4 Cups", Fraction(5, 4), "cup"),
    ("1.5 teaspoon", Fraction(3, 2), "tsp"), ("2 Tbsp", 2, "tbsp"), ("1 Tablespoon", 1, "tbsp"),
    ("16 oz", 16, "oz"), ("6 ounces", 6, "oz"), ("2 fl oz", 2, "fl oz"), ("500 g", 500, "g"),
    ("3 cloves", 3, "clove"), ("1 (10.5 ounce) can", 1, "can"), ("8 slices", 8, "slice"),
    ("1/3 cup (73 grams/79 ml)", Fraction(1, 3), "cup"), ("2 cups (485 g)", 2, "cup"),
    ("1 large", 1, None), ("16 small", 16, None), ("4", 4, None), ("⅔ cup", Fraction(2, 3), "cup"),
])
def test_parse_amount(text, qty, unit):
    assert parse_amount(text) == (Fraction(qty), unit, "")


@pytest.mark.parametrize("text", ["2-3 cups", "1 to 2 tablespoons", "1-2", "a handful", "some", "2 heaping scoops"])
def test_unclear_amounts_are_kept_as_typed(text):
    qty, unit, raw = parse_amount(text)
    assert (qty, unit, raw) == (None, None, text)


def test_blank_amount_is_unquantified():
    assert parse_amount("") == (None, None, "")


# ---- Names ----

@pytest.mark.parametrize("a, b", [
    ("onion", "Onions"), ("Ground beef", "lean ground beef"), ("ground beef (80% lean)", "Ground Beef"),
    ("butter", "Butter, melted"), ("mozzarella", "shredded mozzarella"), ("Tomatoes", "diced fresh tomatoes"),
    ("garlic", "garlic, minced"), ("salt", "kosher salt"), ("black pepper", "freshly ground black pepper"),
    ("black pepper", "pepper"), ("cumin", "ground cumin"), ("jalapeño", "Jalapeno"),
    ("cilantro", "Cilantro (chopped, plus more to garnish)"), ("corn tortillas", "corn tortillas for serving"),
    ("chicken", "cooked diced or shredded chicken".replace("cooked ", "")),
])
def test_same_ingredient_matches(a, b):
    assert name_key(a) == name_key(b)


@pytest.mark.parametrize("a, b", [
    ("unsalted butter", "salted butter"), ("garlic", "garlic powder"), ("oregano", "dried oregano"),
    ("tomatoes", "crushed tomatoes"), ("onion", "onion powder"), ("ground beef", "ground turkey"),
    ("red onion", "onion"), ("chicken breasts", "chicken thighs"), ("salt", "seasoned salt"),
    ("butter", "peanut butter"), ("milk", "coconut milk"), ("basil", "dried basil"),
])
def test_different_ingredients_never_match(a, b):
    assert name_key(a) != name_key(b)


# ---- Combining (the example from the spec) ----

def test_taco_bowls_and_burgers_combine():
    result = items(
        (1, "Taco Bowls", [("1 lb", "ground beef"), ("1", "onion"), ("2", "tomatoes")]),
        (2, "Burgers", [("2 lbs", "ground beef"), ("1", "onion")]),
    )
    beef = result["ground beef"]
    assert beef.display == "Ground beef" and beef.quantity == "3 lbs"
    assert [(s.recipe_name, s.amount) for s in beef.sources] == [("Taco Bowls", "1 lb"), ("Burgers", "2 lbs")]
    assert result["onion"].quantity == "2"
    assert [(s.recipe_name, s.amount) for s in result["onion"].sources] == [("Taco Bowls", "1"), ("Burgers", "1")]
    assert result["tomato"].quantity == "2"
    assert len(result) == 3  # no duplicates


@pytest.mark.parametrize("lines, expected", [
    ([("16 oz", "ground beef"), ("1 lb", "ground beef")], "2 lbs"),          # 16 oz = 1 lb
    ([("8 oz", "cheddar"), ("1 lb", "cheddar")], "1 1/2 lbs"),
    ([("1 tsp", "cumin"), ("2 teaspoons", "cumin")], "1 tbsp"),               # 3 tsp = 1 tbsp
    ([("2 tbsp", "flour"), ("2 tablespoons", "flour")], "1/4 cup"),           # 4 tbsp = 1/4 cup
    ([("1/2 cup", "milk"), ("1/4 cup", "milk")], "3/4 cup"),
    ([("1 1/2 cups", "rice"), ("1 cup", "rice")], "2 1/2 cups"),
    ([("1/2 tsp", "paprika"), ("1/4 tsp", "paprika")], "3/4 tsp"),
    ([("1 tbsp", "honey"), ("1 tsp", "honey")], "1 tbsp + 1 tsp"),
    ([("3 cloves", "garlic"), ("4", "garlic cloves (minced)"), ("6 large", "cloves garlic, minced")], "13 cloves"),
    ([("1 can", "black beans"), ("2 cans", "black beans")], "3 cans"),
    ([("1 pint", "cherry tomatoes"), ("1 pint", "cherry tomatoes")], "2 pints"),
])
def test_quantities_add_up_with_safe_conversions(lines, expected):
    result = items((1, "A", lines[:1]), (2, "B", lines[1:]))
    assert len(result) == 1
    assert next(iter(result.values())).quantity == expected


def test_volume_and_weight_are_never_mixed():
    result = items((1, "A", [("1 cup", "tomato sauce")]), (2, "B", [("15 oz", "tomato sauce")]))
    assert result["tomato sauce"].quantity == "1 cup + 15 oz"


def test_unknown_quantities_are_not_invented():
    result = items(
        (1, "A", [("", "Salt to taste"), ("2-3 cups", "oil for frying")]),
        (2, "B", [("2 tbsp", "oil"), ("", "salt")]),
    )
    assert result["salt"].quantity == ""
    assert result["oil"].quantity == "2 tbsp + 2-3 cups"
    assert [s.amount for s in result["oil"].sources] == ["2-3 cups", "2 tbsp"]


def test_salt_and_pepper_line_becomes_two_staples():
    result = items((1, "A", [("", "Salt and freshly ground black pepper (to taste)")]))
    assert set(result) == {"salt", "black pepper"}


def test_juice_of_limes_counts_limes():
    result = items((1, "Tacos", [("", "Juice of 2 limes"), ("1", "lime (cut into wedges)")]))
    assert result["lime"].quantity == "3"
    assert [s.amount for s in result["lime"].sources] == ["2 + 1"]


def test_butter_merges_into_the_only_qualified_kind():
    result = items((1, "A", [("1 tbsp", "butter")]), (2, "B", [("2 tbsp", "unsalted butter")]))
    assert list(result) == ["unsalted butter"]
    assert result["unsalted butter"].quantity == "3 tbsp"


def test_salted_and_unsalted_butter_stay_separate():
    result = items((1, "A", [("1 tbsp", "butter"), ("1 tbsp", "salted butter")]),
                   (2, "B", [("2 tbsp", "unsalted butter")]))
    assert set(result) == {"butter", "salted butter", "unsalted butter"}


def test_recipe_planned_twice_doubles_and_says_so():
    result = combine([(1, "Tacos", "1 lb", "ground beef", 2), (2, "Burgers", "2 lbs", "ground beef", 1)])
    beef = result["ground beef"]
    assert beef.quantity == "4 lbs"
    assert [(s.recipe_name, s.times) for s in beef.sources] == [("Tacos", 2), ("Burgers", 1)]


def test_same_recipe_listing_an_ingredient_twice_is_one_source():
    result = items((1, "Meatballs", [("1/2 cup", "onion"), ("1/2 cup", "minced onion")]))
    assert result["onion"].quantity == "1 cup"
    assert [(s.recipe_name, s.amount, s.times) for s in result["onion"].sources] == [("Meatballs", "1/2 cup + 1/2 cup", 1)]


def test_empty_plan_combines_to_nothing():
    assert combine([]) == {}
    assert parse_line("1 cup", "") == []


def test_staple_matching():
    staples = {name_key(n) for n in ("Butter", "Black pepper", "Olive oil", "Garlic powder")}
    assert matches_staple(name_key("unsalted butter"), staples)
    assert matches_staple(name_key("extra-virgin olive oil"), staples)
    assert matches_staple(name_key("freshly ground black pepper"), staples)
    assert not matches_staple(name_key("garlic"), staples)
    assert not matches_staple(name_key("peanut butter"), staples)


def test_needs_more():
    old = combine([(1, "A", "1 lb", "beef")])["beef"].totals
    more = combine([(1, "A", "3 lb", "beef")])["beef"].totals
    less = combine([(1, "A", "8 oz", "beef")])["beef"].totals
    assert needs_more(old, more) and not needs_more(old, less) and not needs_more(old, old)


def test_format_quantity_of_nothing():
    assert format_quantity([]) == ""
