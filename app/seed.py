"""Add a few sample recipes for trying the app out (development only).

Never runs automatically. Run explicitly:  python -m app.seed
It refuses to touch a database that already has recipes unless --force is given.
"""

# sys reads command-line flags.
import sys

# Project modules.
from app.config import load_settings
from app.database import connect, init_db
from app.meals import RecipeData, count_recipes, save_recipe


SAMPLES = [
    RecipeData(
        name="Sheet-Pan Chicken Fajitas", category="weeknight", cuisine="Tex-Mex",
        description="Peppers, onions, and chili-lime chicken roasted together. One pan, twenty minutes.",
        prep_minutes=15, cook_minutes=25, servings=4, card_tag="Taco Tuesday", last_made=None,
        ingredients=[("1½ lb", "chicken breast, sliced thin"), ("3", "bell peppers, sliced"),
                     ("1", "red onion, sliced"), ("2 tbsp", "olive oil"), ("1 tbsp", "chili powder"),
                     ("1 tsp", "cumin"), ("1", "lime, juiced"), ("8", "flour tortillas")],
        steps=[("Heat the oven", "Preheat to 425°F and line a sheet pan with foil."),
               ("Season", "Toss chicken, peppers, and onion with oil and spices."),
               ("Roast", "Spread out in one layer and roast 20–25 minutes, stirring once."),
               ("Finish", "Squeeze lime over everything and serve in warm tortillas.")],
        note_author="Dad", note_body="Kids like theirs with extra cheese and no onions.",
    ),
    RecipeData(
        name="Sunday Pot Roast", category="sunday-dinner", cuisine="American",
        description="Slow-braised chuck roast with carrots and potatoes. The house smells amazing all afternoon.",
        prep_minutes=20, cook_minutes=210, servings=6, card_tag="Dad's favorite", last_made=None,
        ingredients=[("3 lb", "chuck roast"), ("1 lb", "carrots"), ("2 lb", "Yukon gold potatoes"),
                     ("1", "onion, quartered"), ("2 cups", "beef broth"), ("2 sprigs", "rosemary")],
        steps=[("Sear", "Brown the roast on all sides in a hot Dutch oven."),
               ("Braise", "Add broth, onion, and rosemary. Cover and bake at 300°F for 2½ hours."),
               ("Vegetables", "Add carrots and potatoes; cook one more hour until tender.")],
    ),
    RecipeData(
        name="Grandma's Chicken Noodle Soup", category="soups", cuisine="",
        description="The one we make whenever anyone has a cold.",
        prep_minutes=15, cook_minutes=40, servings=6, card_tag="", last_made=None,
        ingredients=[("8 cups", "chicken stock"), ("2 cups", "shredded chicken"), ("3", "carrots, diced"),
                     ("3 stalks", "celery, diced"), ("6 oz", "egg noodles")],
        steps=[("", "Simmer carrots and celery in the stock for 15 minutes."),
               ("", "Add noodles and cook until tender, about 8 minutes."),
               ("", "Stir in chicken, season with salt and pepper, and serve.")],
    ),
]


def main() -> None:
    settings = load_settings()
    init_db(settings.database_path)
    conn = connect(settings.database_path)
    try:
        if count_recipes(conn) and "--force" not in sys.argv:
            print("The database already has recipes. Nothing added (use --force to add samples anyway).")
            return
        for recipe in SAMPLES:
            save_recipe(conn, recipe, photo_filename=None)
        print(f"Added {len(SAMPLES)} sample recipes to {settings.database_path}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
