"""Browser tests: the real app under Uvicorn, driven by Chromium with real clicks.

Each test module gets its own server and throwaway data folder. A stand-in Ollama answers
model requests. Speech recognition and speech synthesis are *simulated* in the page (see
speech_stub.js) because headless browsers have no microphone or voices; these tests check
the app's conversation logic, not Apple's or Google's speech engines.

Skipped automatically when Playwright or its Chromium can't start. On Linux without the
system libraries, see docs/testing.md (LD_LIBRARY_PATH with locally unpacked libnss3).
"""

# os/subprocess/socket/time run and wait for the server; pathlib finds the stub script.
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# pytest provides fixtures.
import pytest

# Project helpers.
from app.auth import hash_password
from tests.browser.fake_ollama import FakeOllama

try:
    from playwright.sync_api import sync_playwright
except ImportError:   # pragma: no cover - dev dependency missing
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]
SPEECH_STUB = (Path(__file__).parent / "speech_stub.js").read_text() if (Path(__file__).parent / "speech_stub.js").exists() else ""
OWNER, OWNER_PASSWORD = "kyle", "correct horse battery"
PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True,
         "user_agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_ollama():
    fake = FakeOllama().start()
    yield fake
    fake.stop()


@pytest.fixture(scope="module")
def server(tmp_path_factory, fake_ollama):
    data = tmp_path_factory.mktemp("browser-data")
    port = _free_port()
    env = {**os.environ, "FAMILY_TABLE_DATA_DIR": str(data), "FAMILY_USERNAME": OWNER,
           "FAMILY_PASSWORD_HASH": hash_password(OWNER_PASSWORD), "FAMILY_DISPLAY_NAME": "Kyle",
           "FAMILY_TABLE_NAME": "Payne's Table", "COOKIE_SECURE": "false", "OLLAMA_URL": fake_ollama.url,
           "OLLAMA_TIMEOUT_SECONDS": "20", "HOUSEHOLD_TIMEZONE": "America/Chicago"}
    log = open(data / "server.log", "w")  # noqa: SIM115 - closed below
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
                             str(port), "--workers", "1"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError((data / "server.log").read_text())
            time.sleep(0.1)
    yield {"url": base, "data": data, "proc": proc}
    proc.terminate()
    proc.wait(10)
    log.close()


@pytest.fixture(scope="module")
def browser_engine():
    if sync_playwright is None:
        pytest.skip("Playwright isn't installed (pip install -r requirements-dev.txt)")
    with sync_playwright() as p:
        try:
            engine = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch problem means "skip"
            pytest.skip(f"Chromium can't start here: {str(exc).splitlines()[0]}")
        yield engine
        engine.close()


@pytest.fixture
def new_phone(browser_engine, server):
    """Make a phone-sized browser context (its own cookies), with simulated speech."""
    contexts = []

    def make(speech: bool = True):
        context = browser_engine.new_context(base_url=server["url"], **PHONE)
        context.set_default_timeout(10_000)
        if speech and SPEECH_STUB:
            context.add_init_script(SPEECH_STUB)
        contexts.append(context)
        return context.new_page()

    yield make
    for context in contexts:
        context.close()


def sign_in(page, username=OWNER, password=OWNER_PASSWORD):
    page.goto("/login")
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_load_state()


def sign_up(page, username, name, password="a long kitchen password", invite_url=None):
    page.goto(invite_url or "/signup")
    if invite_url:
        page.click("text=Create an account and join")
    page.fill("#display_name", name)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("button:has-text('Create account')")
    page.wait_for_load_state()
