"""Every main screen renders on a phone, with the Table in the header and four destinations."""

from tests.browser.conftest import sign_in


def test_main_screens_render_on_a_phone(new_phone, server):
    page = new_phone()
    sign_in(page)
    shots = server["data"] / "shots"
    shots.mkdir(exist_ok=True)
    for path, name in [("/plan", "plan"), ("/", "recipes"), ("/shopping", "groceries"), ("/table", "table"),
                       ("/account", "account"), ("/recipes/new", "new-recipe")]:
        page.goto(path)
        assert "Payne's Table" in page.inner_text(".table-bar")
        assert page.locator(".app-nav__link").count() == 4
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), f"{path} scrolls sideways"
        page.screenshot(path=str(shots / f"{name}.png"), full_page=False)
