"""Docker configuration and the one-server-per-data-folder lock.

These read the files and run small processes; they don't need Docker itself. The container
was also built and run for real against a copy of the data (see docs/docker.md).
"""

# subprocess runs a second Python process that tries to take the lock.
import subprocess
import sys

# Path finds the project files.
from pathlib import Path

# pytest provides raises.
import pytest

# Project modules.
from app.database import DataInUse, data_folder_lock


ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text()


# ---- Only one server per data folder ----

TRY_LOCK = """
import sys
from pathlib import Path
from app.database import DataInUse, data_folder_lock
try:
    with data_folder_lock(Path(sys.argv[1])):
        print("got it")
except DataInUse as exc:
    print("refused:", exc)
"""


def try_lock_in_another_process(folder: Path) -> str:
    result = subprocess.run([sys.executable, "-c", TRY_LOCK, str(folder)], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def test_second_server_on_the_same_data_folder_is_refused(tmp_path):
    with data_folder_lock(tmp_path):
        answer = try_lock_in_another_process(tmp_path)
    assert answer.startswith("refused: Family Table is already running")
    assert "docker compose down" in answer


def test_lock_is_released_when_the_server_stops(tmp_path):
    with data_folder_lock(tmp_path):
        pass
    assert try_lock_in_another_process(tmp_path) == "got it"


def test_different_data_folders_dont_block_each_other(tmp_path):
    with data_folder_lock(tmp_path / "real"):
        assert try_lock_in_another_process(tmp_path / "copy") == "got it"


def test_app_startup_holds_the_lock(client, settings):
    # `client` is a running app on settings.data_dir: another process can't start on it.
    assert try_lock_in_another_process(settings.data_dir).startswith("refused")


def test_lock_error_type_is_clear():
    assert issubclass(DataInUse, RuntimeError)


# ---- Docker files ----

def test_compose_publishes_only_on_localhost_port_8000():
    compose = read("compose.yaml")
    assert '"127.0.0.1:${FAMILY_TABLE_PORT:-8000}:8000"' in compose
    assert "0.0.0.0:" not in compose and "network_mode: host" not in compose


def test_compose_runs_one_app_container_and_never_restarts_by_itself():
    compose = read("compose.yaml")
    services = compose.split("services:")[1].split("\nnetworks:")[0]
    top_level = [line for line in services.splitlines() if line.startswith("  ") and not line.startswith("   ")]
    assert [line.strip() for line in top_level] == ["app:"]
    assert 'restart: "no"' in compose


def test_compose_keeps_data_outside_the_container_and_secrets_outside_the_image():
    compose = read("compose.yaml")
    assert "source: ${FAMILY_TABLE_DATA:-./data}" in compose and "target: /data" in compose
    assert "env_file: ${FAMILY_TABLE_ENV_FILE:-.env}" in compose
    assert "PASSWORD_HASH" not in compose and "scrypt" not in compose
    assert 'COOKIE_SECURE: "true"' in compose


def test_forwarded_addresses_are_trusted_only_from_the_fixed_gateway():
    compose = read("compose.yaml")
    assert "FORWARDED_ALLOW_IPS: 10.89.42.1" in compose
    assert "gateway: 10.89.42.1" in compose and "subnet: 10.89.42.0/29" in compose
    assert "--forwarded-allow-ips '*'" not in read("scripts/container-start.sh")


def test_dockerfile_matches_dev_python_and_runs_as_a_normal_user():
    dockerfile = read("Dockerfile")
    assert f"FROM python:{sys.version_info.major}.{sys.version_info.minor}" in dockerfile
    assert "USER familytable" in dockerfile and "ARG APP_UID=1000" in dockerfile
    assert "COPY requirements.txt" in dockerfile and "pip install -r requirements.txt" in dockerfile
    for private in ("COPY . ", "COPY data", ".env", "family_table.db"):
        assert private not in dockerfile, private


def test_container_starts_in_production_mode():
    start = read("scripts/container-start.sh")
    assert "--reload" not in start and "--workers 1" in start and "--no-server-header" in start
    assert "family_table.db" in start and "FAMILY_TABLE_NEW_DATABASE" in start   # no accidental empty DB


def test_dockerignore_is_an_allow_list_that_keeps_private_files_out():
    lines = [l.strip() for l in read(".dockerignore").splitlines() if l.strip() and not l.startswith("#")]
    assert lines[0] == "*"   # everything excluded unless listed
    allowed = {l[1:] for l in lines if l.startswith("!")}
    assert allowed == {"requirements.txt", "app/", "templates/", "static/", "scripts/container-start.sh"}


def test_gitignore_still_protects_family_data_and_secrets():
    gitignore = read(".gitignore")
    for pattern in (".env", "data/*", "*.db", "*.db-wal", "*.db-shm"):
        assert pattern in gitignore, pattern


def test_original_server_script_is_kept():
    serve = read("scripts/serve.sh")
    assert "--host 127.0.0.1" in serve and "already in use" in serve


@pytest.mark.parametrize("name", ["Dockerfile", "compose.yaml", ".dockerignore", "scripts/container-start.sh"])
def test_docker_files_exist(name):
    assert (ROOT / name).is_file()
