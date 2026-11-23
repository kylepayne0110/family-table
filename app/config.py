# os reads environment variables.
import os

# dataclass gives us a simple settings container.
from dataclasses import dataclass

# Path provides filesystem utilities.
from pathlib import Path

# ZoneInfo turns the household time zone name into dates ("is it Saturday yet?").
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# load_dotenv copies values from .env into the environment.
from dotenv import load_dotenv


# Project root (the family-table folder).
BASE_DIR = Path(__file__).resolve().parent.parent

# Recipe categories: stored key -> display label.
CATEGORIES: dict[str, str] = {
    "weeknight": "Weeknight",
    "sunday-dinner": "Sunday dinner",
    "kids-pick": "Kids' pick",
    "soups": "Soups",
}

# Used when .env doesn't name the household's time zone.
DEFAULT_TIMEZONE = "America/Chicago"


def _env_bool(name: str, default: bool) -> bool:
    """Read a true/false environment variable."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """All runtime settings for one app instance."""

    data_dir: Path
    username: str
    password_hash: str
    cookie_secure: bool = False
    max_upload_mb: int = 12
    session_hours: int = 12
    remember_days: int = 30
    # The household's time zone decides when a day (and its planned dinner) is over.
    timezone: str = DEFAULT_TIMEZONE
    # Local Ollama server for the Cooking Assistant (never exposed to browsers).
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:1.7b"
    ollama_timeout: float = 90.0
    # Upgrade only: the name shown for the first account and the family's first Table.
    display_name: str = ""
    table_name: str = ""
    # How many model requests may run at once (the rest get "busy, try again" right away).
    model_concurrency: int = 2

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "family_table.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def import_dir(self) -> Path:
        """Photos found by URL import, held here until the recipe is saved."""
        return self.data_dir / "import-tmp"

    @property
    def login_configured(self) -> bool:
        return bool(self.username and self.password_hash)


def load_settings() -> Settings:
    """Build settings from .env and the environment."""
    load_dotenv(BASE_DIR / ".env")

    # Relative data paths are resolved from the project root.
    data_dir = Path(os.getenv("FAMILY_TABLE_DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir

    timezone = (os.getenv("HOUSEHOLD_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"HOUSEHOLD_TIMEZONE={timezone!r} isn't a time zone name like America/Chicago.") from None

    return Settings(
        data_dir=data_dir,
        username=os.getenv("FAMILY_USERNAME", "").strip(),
        password_hash=os.getenv("FAMILY_PASSWORD_HASH", "").strip(),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB") or 12),
        session_hours=int(os.getenv("SESSION_HOURS") or 12),
        remember_days=int(os.getenv("REMEMBER_DAYS") or 30),
        timezone=timezone,
        ollama_url=(os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").strip().rstrip("/"),
        ollama_model=(os.getenv("OLLAMA_MODEL") or "qwen3:1.7b").strip(),
        ollama_timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS") or 90),
        display_name=(os.getenv("FAMILY_DISPLAY_NAME") or "").strip()[:60],
        table_name=(os.getenv("FAMILY_TABLE_NAME") or "").strip()[:60],
        model_concurrency=max(1, int(os.getenv("OLLAMA_MAX_CONCURRENT") or 2)),
    )
