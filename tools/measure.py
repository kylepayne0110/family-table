"""Measure Family Table's response times with representative data (for maintainers).

    python tools/measure.py [--tables 300] [--recipes-per-table 50] [--listings 2000]

Builds a throwaway data folder (never your real one), starts the app with Uvicorn and a
stand-in model that takes 20 s to answer, and reports:
  - community search, the planner, the recipe book, and ideas, while idle;
  - the same pages while two people are waiting on slow model replies;
  - the web server's memory and CPU (the model's own use is separate: see the Ollama row).
Nothing here touches the internet or your Ollama.
"""

import argparse
import json
import os
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORDS = ["chicken", "beef", "pork", "tofu", "rice", "pasta", "beans", "potato", "broccoli", "carrot", "onion", "garlic",
         "tomato", "cheese", "spinach", "mushroom", "pepper", "lemon", "ginger", "curry", "taco", "soup", "salad",
         "chili", "lasagna", "stir fry", "casserole", "bake", "roast", "grill", "noodle", "dumpling", "pie", "stew"]


def seed(data: Path, tables: int, per_table: int, listings: int) -> None:
    from app.auth import hash_password
    from app.community import _index
    from app.database import connect, init_db
    from app.migrations import create_table_in_transaction
    init_db(data / "family_table.db")
    conn = connect(data / "family_table.db")
    password = hash_password("measure password 123")
    rng = random.Random(7)
    conn.execute("BEGIN")
    for t in range(tables):
        uid = conn.execute("INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
                           (f"cook{t}", f"Cook {t}", password)).lastrowid
        tid = create_table_in_transaction(conn, f"Table {t}", uid)
        conn.execute("UPDATE users SET last_table_id = ? WHERE id = ?", (tid, uid))
        for r in range(per_table):
            a, b = rng.sample(WORDS, 2)
            rid = conn.execute("INSERT INTO recipes (table_id, name, category, servings, added_by) VALUES (?, ?, 'weeknight', 4, ?)",
                               (tid, f"{a.title()} {b.title()} {r}", uid)).lastrowid
            conn.executemany("INSERT INTO ingredients (recipe_id, position, amount, name) VALUES (?, ?, '1 cup', ?)",
                             [(rid, i, w) for i, w in enumerate(rng.sample(WORDS, 8))])
            conn.executemany("INSERT INTO steps (recipe_id, position, body) VALUES (?, ?, ?)",
                             [(rid, i, f"Step {i} with {rng.choice(WORDS)}.") for i in range(6)])
    for n in range(listings):
        a, b = rng.sample(WORDS, 2)
        ings = [["1 cup", w] for w in rng.sample(WORDS, 8)]
        snap = {"name": f"{a.title()} {b.title()} Community {n}", "description": "", "category": "weeknight", "cuisine": "",
                "prep_minutes": 10, "cook_minutes": 20, "servings": 4, "creator_credit": "", "source_url": "",
                "ingredients": ings, "steps": [["", "Cook it."]]}
        lid = conn.execute("""INSERT INTO community_listings (publisher_name, name, ingredient_count, servings)
                              VALUES ('Someone', ?, 8, 4)""", (snap["name"],)).lastrowid
        conn.execute("INSERT INTO community_versions (listing_id, version, snapshot_json) VALUES (?, 1, ?)",
                     (lid, json.dumps(snap)))
        _index(conn, lid, snap)
    conn.commit()
    conn.close()


class SlowModel:
    """A stand-in Ollama that takes `delay` seconds per answer."""

    def __init__(self, delay: float):
        from tests.browser.fake_ollama import FakeOllama
        self.fake = FakeOllama().start()
        self.fake.delay = delay


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    def __init__(self, base: str):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.ProxyHandler({}))

    def get(self, path: str) -> tuple[int, str, float]:
        start = time.perf_counter()
        try:
            with self.opener.open(self.base + path, timeout=60) as r:
                body = r.read().decode()
                return r.status, body, time.perf_counter() - start
        except urllib.error.HTTPError as exc:
            return exc.code, "", time.perf_counter() - start

    def post(self, path: str, data: dict, fetch: bool = False) -> tuple[int, str, float]:
        start = time.perf_counter()
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(data, doseq=True).encode(),
                                     headers={"X-Requested-With": "fetch"} if fetch else {})
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.read().decode(), time.perf_counter() - start
        except urllib.error.HTTPError as exc:
            return exc.code, "", time.perf_counter() - start

    def login(self, username: str):
        _, page, _ = self.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        self.post("/login", {"username": username, "password": "measure password 123", "csrf_token": token})
        _, page, _ = self.get("/")
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


def timings(browser: Browser, paths: list[str], rounds: int = 15) -> dict[str, list[float]]:
    out = {p: [] for p in paths}
    for _ in range(rounds):
        for p in paths:
            status, _, seconds = browser.get(p)
            assert status == 200, (p, status)
            out[p].append(seconds * 1000)
    return out


def report(title: str, results: dict[str, list[float]]) -> None:
    print(f"\n{title}")
    for path, values in results.items():
        values.sort()
        p95 = values[int(len(values) * 0.95) - 1]
        print(f"  {path:<48} median {statistics.median(values):6.1f} ms   p95 {p95:6.1f} ms")


def process_stats(pid: int) -> str:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        rss = int(re.search(r"VmRSS:\s+(\d+)", status).group(1)) / 1024
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        cpu = (int(stat[13]) + int(stat[14])) / os.sysconf("SC_CLK_TCK")
        return f"{rss:.0f} MB resident, {cpu:.1f} s CPU so far"
    except OSError:
        return "unavailable"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", type=int, default=300)
    parser.add_argument("--recipes-per-table", type=int, default=50)
    parser.add_argument("--listings", type=int, default=2000)
    args = parser.parse_args()

    data = Path(tempfile.mkdtemp(prefix="ft-measure-"))
    try:
        t = time.perf_counter()
        seed(data, args.tables, args.recipes_per_table, args.listings)
        size = (data / "family_table.db").stat().st_size / 1e6
        print(f"Seeded {args.tables} Tables, {args.tables * args.recipes_per_table} recipes, {args.listings} community "
              f"listings ({size:.0f} MB) in {time.perf_counter() - t:.0f} s")
        model = SlowModel(20)
        port = free_port()
        env = {**os.environ, "FAMILY_TABLE_DATA_DIR": str(data), "FAMILY_USERNAME": "", "FAMILY_PASSWORD_HASH": "",
               "OLLAMA_URL": model.fake.url, "OLLAMA_TIMEOUT_SECONDS": "60", "COOKIE_SECURE": "false"}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--workers", "1",
                                 "--log-level", "warning"], cwd=ROOT, env=env)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/login", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        cook = Browser(base)
        cook.login("cook1")
        from datetime import date, timedelta
        monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        # A planned dinner for the two talkers.
        talkers = []
        for n in (2, 3):
            b = Browser(base)
            b.login(f"cook{n}")
            _, page, _ = b.get(f"/plan/{monday}/day/6/choose")
            rid = re.search(r'name="recipe_id" value="(\d+)"', page).group(1)
            b.post(f"/plan/{monday}/day/6", {"csrf_token": b.csrf, "recipe_id": rid})
            _, page, _ = b.get(f"/plan?week={monday}")
            meal = re.search(r'href="/plan/meals/(\d+)"', page).group(1)
            talkers.append((b, meal))
        paths = ["/community?q=chicken", "/community?q=chicken+broccoli+garlic", "/community?q=chicken&page=5",
                 "/community", f"/plan?week={monday}", "/", "/?q=garlic", "/ideas?have=chicken,+rice&source=familiar",
                 "/ideas?have=chicken,+rice&source=community", "/shopping"]
        timings(cook, paths, 2)    # warm up
        report("Idle (one person browsing)", timings(cook, paths))
        print(f"  web server: {process_stats(proc.pid)}")

        # Two people waiting on slow model answers while someone else uses the planner.
        threads = []
        model_seconds = []
        for b, meal in talkers:
            def ask(b=b, meal=meal):
                _, _, s = b.post(f"/plan/meals/{meal}/chat", {"csrf_token": b.csrf, "message": "Can I make this ahead?",
                                                               "request_key": f"k{random.random()}"[2:20]}, fetch=True)
                model_seconds.append(s)
            threads.append(threading.Thread(target=ask))
        for th in threads:
            th.start()
        time.sleep(1)
        report("While 2 people wait on 20-second model replies", timings(cook, paths, 6))
        third = Browser(base)
        third.login("cook4")
        _, _, s = third.post("/ideas/new", {"csrf_token": third.csrf, "have": "chicken"})
        print(f"  a third model request during that time answered in {s * 1000:.0f} ms (told 'busy', not queued)")
        for th in threads:
            th.join()
        print(f"  the two model replies took {', '.join(f'{x:.1f} s' for x in model_seconds)}")
        print(f"  web server: {process_stats(proc.pid)}")
        proc.terminate()
        proc.wait(10)
        model.fake.stop()
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
