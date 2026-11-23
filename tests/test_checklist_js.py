"""Regression: "Add N items" must post to the checklist's own action, not the page it's shown on.

Browsers report a submit button's `formAction` as the page's URL when the button has no
formaction attribute. checklist.js once trusted that value, so on the planned-meal page the
add went to /plan/meals/<id> (405) and the sheet said "Couldn't add those yet. Try again."
"""

# json passes cases to Node; shutil finds Node; subprocess runs it.
import json
import shutil
import subprocess

# pytest skips when Node isn't installed.
import pytest

# Project paths and helpers.
from app.config import BASE_DIR
from tests.plan_helpers import FETCH, add_ingredients, plan, query, token
from tests.test_meal_features import NUGGETS_INGREDIENTS, NUGGETS_STEPS, save

CHECKLIST_JS = BASE_DIR / "static" / "js" / "checklist.js"

# A stand-in for a browser <button>: formAction falls back to the page URL, as browsers do.
NODE_SCRIPT = """
const { submitUrl } = require(process.argv[1]);
const button = (attrs, pageUrl) => ({
  hasAttribute: (name) => name in attrs,
  get formAction() { return attrs.formaction ? new URL(attrs.formaction, pageUrl).href : pageUrl; },
});
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(([action, attrs, pageUrl]) =>
  submitUrl({ action }, attrs === null ? null : button(attrs, pageUrl)))));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")
def test_add_button_posts_to_the_form_action_not_the_page():
    page = "http://192.168.1.20:8000/plan/meals/4"
    add = "http://192.168.1.20:8000/plan/meals/4/ingredients"
    cases = [
        [add, {}, page],                                                      # "Add 1 item" (no formaction)
        [add, None, page],                                                    # submitted without a button
        [add, {"formaction": "/plan/meals/4/ingredients/update"}, page],      # "Update list to N servings"
        [add, {}, "http://192.168.1.20:8000/plan?week=2026-11-16"],          # the planner board's sheet
    ]
    result = subprocess.run(["node", "-e", NODE_SCRIPT, str(CHECKLIST_JS), json.dumps(cases)],
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout) == [add, add, "http://192.168.1.20:8000/plan/meals/4/ingredients/update", add]


def test_only_the_update_button_overrides_the_checklist_action(auth_client, settings):
    """The page JavaScript relies on this: every other submit goes to the form's action."""
    rid = save(settings, "Homemade Chicken Nuggets", NUGGETS_INGREDIENTS, NUGGETS_STEPS)
    plan(auth_client, 4, rid)
    mid = query(settings, "SELECT id FROM planned_meals")[0][0]
    auth_client.post(f"/plan/meals/{mid}/servings", data={"servings": "8", "csrf_token": token(auth_client)})
    add_ingredients(auth_client, settings, 4, indexes=[0])
    auth_client.post(f"/plan/meals/{mid}/servings", data={"servings": "6", "csrf_token": token(auth_client)})
    html = auth_client.get(f"/plan/meals/{mid}/ingredients", headers=FETCH).json()["html"]
    assert f'action="/plan/meals/{mid}/ingredients"' in html
    assert html.count("formaction=") == 1                     # the Update button is shown, and is the only one
    assert f'formaction="/plan/meals/{mid}/ingredients/update"' in html
    assert "formaction" not in html.split("data-checklist-submit")[0].rsplit("<button", 1)[-1]

