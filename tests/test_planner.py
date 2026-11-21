# re pulls pieces out of rendered pages.
import re

# timedelta checks week navigation.
from datetime import timedelta

# pytest provides parametrize.
import pytest

# Helpers shared with the shopping list tests.
from tests.plan_helpers import FETCH, TACOS, BURGERS, W, WEEK, add_recipe, plan, post, query, token


def day_card(html: str, day: int) -> str:
    match = re.search(rf'id="day-{day}" data-day="{day}">(.*?)</li>', html, re.S)
    assert match, f"no card for day {day}"
    return match.group(1)


@pytest.fixture
def recipes(auth_client, settings):
    return {
        "tacos": add_recipe(settings, "Taco Bowls", TACOS),
        "burgers": add_recipe(settings, "Burgers", BURGERS),
        "soup": add_recipe(settings, "Tomato Soup", [("2 cans", "tomatoes")]),
    }


def test_add_recipe_to_a_day(auth_client, recipes):
    response = plan(auth_client, 0, recipes["tacos"])
    assert response.status_code == 303
    assert response.headers["location"] == f"/plan?week={W}#day-0"
    page = auth_client.get(f"/plan?week={W}").text
    assert "Taco Bowls" in day_card(page, 0)
    mid = query(auth_client.app.state.settings, "SELECT id FROM planned_meals")[0][0]
    assert f'href="/plan/meals/{mid}"' in day_card(page, 0)               # opens the planned recipe
    assert "1 of 7 dinners planned" in page


def test_remove_recipe_from_a_day(auth_client, recipes):
    plan(auth_client, 2, recipes["soup"])
    assert post(auth_client, f"/plan/{W}/day/2/clear").status_code == 303
    assert "Add dinner" in day_card(auth_client.get(f"/plan?week={W}").text, 2)
    assert query(auth_client.app.state.settings, "SELECT COUNT(*) FROM planned_meals") == [(0,)]


def test_moving_onto_a_planned_day_swaps(auth_client, recipes, settings):
    plan(auth_client, 0, recipes["tacos"])
    plan(auth_client, 3, recipes["burgers"])
    assert post(auth_client, f"/plan/{W}/move", {"from_day": 0, "to_day": 3}).status_code == 303
    assert query(settings, "SELECT day, recipe_id FROM planned_meals ORDER BY day") == [
        (0, recipes["burgers"]), (3, recipes["tacos"])]   # swapped, nothing deleted


def test_moving_onto_an_empty_day_moves(auth_client, recipes, settings):
    plan(auth_client, 0, recipes["tacos"])
    post(auth_client, f"/plan/{W}/move", {"from_day": 0, "to_day": 4})
    assert query(settings, "SELECT day, recipe_id FROM planned_meals") == [(4, recipes["tacos"])]


def test_drag_and_drop_endpoint_returns_fresh_board(auth_client, recipes):
    """Drag-and-drop posts a move with the fetch header and gets the new board back."""
    plan(auth_client, 0, recipes["tacos"])
    plan(auth_client, 6, recipes["burgers"])
    page = auth_client.get(f"/plan?week={W}").text
    assert page.count("data-drag-handle") == 2           # only planned days can be dragged
    version = int(re.search(r'data-version="(\d+)"', page).group(1))

    response = post(auth_client, f"/plan/{W}/move", {"from_day": 6, "to_day": 0}, fetch=True)
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == version + 1
    assert "Burgers" in day_card(data["html"], 0) and "Taco Bowls" in day_card(data["html"], 6)


def test_move_menu_offers_swaps(auth_client, recipes):
    plan(auth_client, 0, recipes["tacos"])
    plan(auth_client, 1, recipes["burgers"])
    card = day_card(auth_client.get(f"/plan?week={W}").text, 0)
    assert '<option value="1">Tuesday (swap)</option>' in card
    assert '<option value="2">Wednesday</option>' in card


def test_moving_an_empty_day_is_refused(auth_client, recipes):
    response = post(auth_client, f"/plan/{W}/move", {"from_day": 1, "to_day": 2}, fetch=True)
    assert response.status_code == 422 and "no dinner" in response.json()["error"]


def test_days_can_stay_blank(auth_client, recipes):
    plan(auth_client, 2, recipes["soup"])
    page = auth_client.get(f"/plan?week={W}").text
    for day in (0, 1, 3, 4, 5, 6):
        assert "Add dinner" in day_card(page, day)
        assert "Leftovers, a night out, or nothing planned" in day_card(page, day)


def test_week_navigation(auth_client, recipes):
    page = auth_client.get(f"/plan?week={W}").text
    assert "Nov 9 – 15, 2026" in page
    assert f'href="/plan?week={(WEEK - timedelta(days=7)).isoformat()}"' in page
    assert f'href="/plan?week={(WEEK + timedelta(days=7)).isoformat()}"' in page
    # Default is the household's current Monday-to-Sunday week (tests freeze it at Mon Nov 9).
    assert f'data-week="{W}"' in auth_client.get("/plan").text
    # A week across the new year.
    assert "Dec 28, 2026 – Jan 3, 2027" in auth_client.get("/plan?week=2026-12-28").text


@pytest.mark.parametrize("bad", ["2026-11-10", "not-a-date", "1800-01-06", "2026-13-01"])
def test_invalid_weeks_are_not_found(auth_client, bad):
    assert auth_client.get(f"/plan?week={bad}").status_code == 404
    assert auth_client.get(f"/plan/{bad}/live").status_code == 404


def test_plans_are_saved_per_week(auth_client, recipes, settings):
    next_week = (WEEK + timedelta(days=7)).isoformat()
    plan(auth_client, 0, recipes["tacos"])
    plan(auth_client, 0, recipes["burgers"], week=next_week)
    # Reload both weeks: each keeps its own dinner.
    assert "Taco Bowls" in day_card(auth_client.get(f"/plan?week={W}").text, 0)
    assert "Burgers" in day_card(auth_client.get(f"/plan?week={next_week}").text, 0)
    assert query(settings, "SELECT week_start, recipe_id FROM planned_meals ORDER BY week_start") == [
        (W, recipes["tacos"]), (next_week, recipes["burgers"])]


def test_same_recipe_on_several_days(auth_client, recipes):
    for day in (0, 2, 4):
        plan(auth_client, day, recipes["tacos"])
    page = auth_client.get(f"/plan?week={W}").text
    assert all("Taco Bowls" in day_card(page, d) for d in (0, 2, 4))


def test_replacing_a_days_recipe(auth_client, recipes, settings):
    plan(auth_client, 1, recipes["tacos"])
    plan(auth_client, 1, recipes["soup"])
    assert query(settings, "SELECT day, recipe_id FROM planned_meals") == [(1, recipes["soup"])]


def test_recipe_picker_lists_and_searches(auth_client, recipes):
    page = auth_client.get(f"/plan/{W}/day/3/choose").text
    assert "What's for <em>Thursday?</em>" in page
    assert page.count('pick-tile__name') == 3
    page = auth_client.get(f"/plan/{W}/day/3/choose?q=taco").text
    assert page.count('pick-tile__name') == 1 and "Taco Bowls" in page
    assert auth_client.get(f"/plan/{W}/day/7/choose").status_code == 404


def test_unknown_recipe_or_day_is_refused(auth_client, recipes):
    assert plan(auth_client, 0, 9999).status_code == 422
    assert plan(auth_client, 7, recipes["tacos"]).status_code == 422
    assert post(auth_client, f"/plan/{W}/day/0", {"recipe_id": "abc"}).status_code == 422


def test_deleting_a_recipe_removes_it_from_plans(auth_client, recipes, settings):
    plan(auth_client, 0, recipes["tacos"])
    post(auth_client, f"/recipes/{recipes['tacos']}/delete")
    assert query(settings, "SELECT COUNT(*) FROM planned_meals") == [(0,)]
    assert auth_client.get(f"/plan?week={W}").status_code == 200


def test_plan_live_endpoint(auth_client, recipes):
    version = auth_client.get(f"/plan/{W}/live?since=-1", headers=FETCH).json()["version"]
    assert auth_client.get(f"/plan/{W}/live?since={version}").status_code == 204
    plan(auth_client, 5, recipes["soup"])
    data = auth_client.get(f"/plan/{W}/live?since={version}").json()
    assert data["version"] == version + 1 and "Tomato Soup" in data["html"]


def test_each_planned_dinner_has_its_own_add_ingredients_button(auth_client, recipes, settings):
    page = auth_client.get(f"/plan?week={W}").text
    assert "Generate Shopping List" not in page and "Update Shopping List" not in page
    plan(auth_client, 0, recipes["tacos"])
    plan(auth_client, 4, recipes["burgers"])
    page = auth_client.get(f"/plan?week={W}").text
    meals = query(settings, "SELECT day, id FROM planned_meals ORDER BY day")
    for day, mid in meals:
        card = day_card(page, day)
        assert f'href="/plan/meals/{mid}/ingredients" data-checklist-open' in card
        assert f'href="/plan/meals/{mid}"' in card        # the title opens the planned recipe view
    assert "Add ingredients" not in day_card(page, 1)     # blank days have nothing to add


def test_moving_a_dinner_keeps_its_identity(auth_client, recipes, settings):
    plan(auth_client, 0, recipes["tacos"])
    plan(auth_client, 3, recipes["burgers"])
    ids = dict(query(settings, "SELECT recipe_id, id FROM planned_meals"))
    post(auth_client, f"/plan/{W}/move", {"from_day": 0, "to_day": 3})
    assert dict(query(settings, "SELECT recipe_id, id FROM planned_meals")) == ids   # swapped days, same dinners
    assert query(settings, "SELECT day FROM planned_meals WHERE id = ?", (ids[recipes["tacos"]],)) == [(3,)]
