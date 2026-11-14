
# Path helps us work with file and folder locations.
from pathlib import Path

# FastAPI creates our web application and handles HTTP requests.
# Request represents an incoming request from the browser.
from fastapi import FastAPI, Request

# StaticFiles allows FastAPI to serve CSS, JavaScript, and images.
from fastapi.staticfiles import StaticFiles

# Jinja2Templates allows FastAPI to render HTML templates.
from fastapi.templating import Jinja2Templates


# Create the FastAPI application that handles incoming web requests.
app = FastAPI()

# Get the root directory of our project (family-table).
# This allows us to locate files regardless of where the app runs.
BASE_DIR = Path(__file__).resolve().parent.parent

# Make our static files accessible to the browser.
# For example, /static/css/styles.css loads our stylesheet.
app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static"
)

# Configure Jinja2 to load HTML files from our templates folder.
# This allows FastAPI to display HTML pages instead of returning JSON.
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# Define the homepage route.
# When a user visits "/", FastAPI calls the home() function.
@app.get("/")
def home(request: Request):

    # Render meals.html and return it to the user's browser.
    # The context dictionary will eventually contain meals from our database.
    return templates.TemplateResponse(
        request=request,
        name="meals.html",
        context={}
    )
