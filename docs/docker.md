# Running Family Table with Docker

One container runs everything: Python 3.14.4, FastAPI/Uvicorn, the app code, templates, and
static files. There is **no database container**. SQLite is a file, and it lives in your
`data/` folder on this computer, not inside the container.

```text
iPhone ─HTTPS─▶ Tailscale Funnel (Windows) ─▶ 127.0.0.1:8000 (Windows) ─▶ Docker ─▶ FastAPI ─▶ data/family_table.db
```

Run every command below from the project folder in a WSL terminal in VS Code:
`cd ~/projects/family-table`. Docker Desktop must be open. It doesn't start with Windows, so
start it from the Start menu first.

## Starting

```bash
docker compose up --build
```

This builds the image if anything changed (a few seconds once built), starts the container, and
keeps showing Family Table's log in the terminal. The app is ready when you see
`Application startup complete`. Open **http://127.0.0.1:8000** on this computer.

It runs in production mode: no auto-reload, one worker, HTTPS-only sign-in cookies, and no
`server` header.

## Stopping

Press **Ctrl+C** in that terminal. Family Table finishes what it's doing and stops, usually in
under a second ("Gracefully Stopping… Container family-table Stopped"). Port 8000 is free again
straight away. Your data is not touched.

Optional clean-up afterwards. It removes the stopped container and its network, **not** your
data:

```bash
docker compose down
```

If the terminal was closed while Family Table was running, stop it with `docker compose down`
from the project folder.

To check that nothing is running:

```bash
docker compose ps        # empty list = stopped
```

## Viewing logs

The terminal running `docker compose up` shows every request
(`"GET /shopping HTTP/1.1" 200 OK`) and any errors. From a second terminal:

```bash
docker compose logs -f          # follow live (Ctrl+C stops following, not the app)
docker compose logs --tail 50   # the last 50 lines
```

## Where your data is

Nothing personal is inside the image or the container:

| What | Where on this computer | In the container |
|---|---|---|
| Recipes, notes, plans, lists, staples | `data/family_table.db` (plus `-wal`/`-shm` while running) | `/data/family_table.db` |
| Recipe photos | `data/uploads/` | `/data/uploads/` |
| Unsaved import photos (auto-cleaned) | `data/import-tmp/` | `/data/import-tmp/` |
| Login and settings | `.env` (read at start, never copied into the image) | environment variables |

The whole `data/` folder is mounted into the container, so the database and its WAL files always
stay together. Removing, rebuilding, or updating the container never touches it.

Safety checks:

- The container refuses to start if the data folder has no `family_table.db` (for example a
  mistyped path), so it can't quietly begin an empty database. For a genuinely new, empty
  Family Table, run it once with `FAMILY_TABLE_NEW_DATABASE=yes docker compose up --build`.
- Only one Family Table server can use a data folder at a time. A second one (Docker, or
  `scripts/serve.sh`) stops at startup with "Family Table is already running with the data
  folder…".
- The container runs as a normal user (id 1000, the same as your WSL user, who owns `data/`),
  with a read-only code folder and no extra privileges.

Back up exactly as before: copy `data/` and `.env`, with Family Table stopped.

## Tailscale (HTTPS for the phones)

Nothing about Tailscale changes. Docker publishes Family Table on **127.0.0.1:8000** of Windows,
the same address the WSL server used, and Funnel forwards the public `https://….ts.net` address
there. Start Funnel the same way as before, in a PowerShell terminal, after Family Table is
running:

```powershell
tailscale funnel 8000
```

Ctrl+C in that terminal turns the public address off. `tailscale funnel status` should then say
`No serve config`.

Port 8000 is published only on 127.0.0.1, so other devices on your Wi-Fi can't reach it, and
nothing is published to the internet except through Funnel. Inside the container the app listens
on 0.0.0.0:8000. That address exists only inside Docker's private network.

Sign-in throttling needs each visitor's real address. Requests reach the container through
Docker's gateway (fixed at `10.89.42.1` in `compose.yaml`), and only that gateway is trusted to
pass along the visitor's address. That's the same rule `scripts/serve.sh` uses with 127.0.0.1.

## Switching between Docker and the original server

`scripts/serve.sh` still works exactly as before. Use one or the other, never both:

```bash
# Docker → original: stop Docker first
#   Ctrl+C in the Docker terminal (or: docker compose down), then
./scripts/serve.sh

# Original → Docker: Ctrl+C in the serve.sh terminal, then
docker compose up --build
```

Both use port 8000 on 127.0.0.1, so Funnel works the same with either. If you forget, the second
one refuses to start: Docker reports `ports are not available`, `serve.sh` says
`Port 8000 is already in use`, and the data-folder lock catches anything else.

## Rebuilding and updating

After changing code or `requirements.txt`, `docker compose up --build` picks it up. For a
completely fresh image:

```bash
docker compose build --no-cache
docker compose up
```

Neither touches `data/` or `.env`. Database changes the app needs (new columns) are applied
automatically at startup without removing anything. Take a backup of `data/` before big updates
anyway.

## Testing with a copy of the data

The settings below point Docker at other folders and ports without editing any file. This is how
the container was tested without touching the real family database:

```bash
FAMILY_TABLE_DATA=/path/to/copy-of-data FAMILY_TABLE_PORT=8766 docker compose up --build
```

(`FAMILY_TABLE_ENV_FILE=/path/to/test.env` swaps in a different login file the same way.)

## Using it on another computer later

The image is built from the code on each computer, so a new machine needs only:

1. WSL2 with Docker Desktop (WSL integration on), Git, and Tailscale for Windows signed in to
   the same account.
2. The project (`git clone`), plus a copy of `data/` and `.env` from the old computer, taken
   while Family Table is stopped there.
3. `docker compose up --build`, then `tailscale funnel 8000`.

Run Family Table on only one computer at a time: two copies would mean two separate databases.
The `https://….ts.net` address comes from the computer's Tailscale name. A new computer gets its
own name, so the address will probably change unless Tailscale is set up to reuse the old name.
If it changes, remove the Family Table icon from each iPhone and add it again from the new
address. If the new WSL user's id isn't 1000 (`id -u`), build with
`docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)`.
