# Testing

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest --ignore=tests/browser     # unit and integration tests (about a minute)
python -m pytest tests/browser              # real-browser tests (Chromium, about two minutes)
ruff check app tests                        # lint (settings in ruff.toml)
pip-audit -r requirements.txt               # known vulnerabilities in dependencies
python tools/measure.py                     # response times with representative data
```

Every test uses a temporary data folder and test accounts. Nothing touches `data/` or your
real Ollama, and the recipe-import tests use a local stand-in instead of the internet.

## Browser tests

`tests/browser/` starts the real app with Uvicorn on a throwaway data folder and drives
Chromium with Playwright: real page loads, clicks, typing, and form submissions, at an
iPhone-sized viewport. A stand-in Ollama (`tests/browser/fake_ollama.py`) answers model
requests and can be made slow or offline.

**Speech is simulated.** Headless browsers have no microphone or voices, so
`tests/browser/speech_stub.js` replaces `SpeechRecognition` and `speechSynthesis` with
scripted versions: the test "says" things, can interrupt while the page is talking, can make
the microphone hear the page's own voice (echo), and can refuse microphone permission. This
checks Family Table's conversation handling, not Apple's or Google's speech engines. Check a
real iPhone with the voice section of [iphone-testing.md](iphone-testing.md).

First-time setup:

```bash
python -m playwright install chromium
```

On Ubuntu/WSL without `sudo`, Chromium may be missing a few system libraries
(`libnspr4`, `libnss3`, `libasound2`). Unpack them locally and point the loader at them:

```bash
mkdir -p ~/.local/share/ft-browser-libs/debs && cd ~/.local/share/ft-browser-libs/debs
apt-get download libnspr4 libnss3 libasound2t64
for f in *.deb; do dpkg -x "$f" ../root; done
export LD_LIBRARY_PATH=~/.local/share/ft-browser-libs/root/usr/lib/x86_64-linux-gnu
```

If Chromium can't start, the browser tests are skipped (not failed), with the reason.

## What the suites cover

| Area | Where |
|---|---|
| Accounts, sessions, reset links, Tables, invites, removal through every route, switching, privacy between households, the upgrade (and a failed one) | `test_accounts_tables.py` |
| Community publishing, snapshots, versions, saving copies, withdrawal, reports, search, ideas | `test_community.py` |
| Cook with me: steps, interruptions, whole recipe, scaling, permanent edits and spoken confirmation, sessions per dinner, packaged meals, offline model | `test_cook.py` |
| Recipe voice: understanding speech, corrections, duplicates, questions, "to taste", model edits checked against what was said, drafts, photos | `test_recipe_voice.py` |
| Log redaction, model output that tries to act on its own, simultaneous double taps | `test_hardening.py` |
| Everything from before (recipes, import and SSRF, planner, groceries, staples, packaged meals, assistant, PWA, hosting) | the other `test_*.py` files |
| Real browser: every screen on a phone, Cook with me, Tell me the recipe, invites/removal/rejoin, two Tables, publishing and ideas, Add ingredients from an open dinner, packaged meals, offline model, double-tap Save, upgrading an old install | `tests/browser/` |

## Live model check

The automated tests never call a real model. To see how your own Ollama behaves with the
Cooking Assistant and the recipe voice helper, run a few questions by hand on a planned dinner,
or use `OLLAMA_URL` with the dev server and try "How do I know when it's done?" in Cook with me.
