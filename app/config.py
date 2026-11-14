# os reads environment variables.
import os

# dataclass gives us a simple settings container.
from dataclasses import dataclass

# Path provides filesystem utilities.
from pathlib import Path

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

    return Settings(
        data_dir=data_dir,
        username=os.getenv("FAMILY_USERNAME", "").strip(),
        password_hash=os.getenv("FAMILY_PASSWORD_HASH", "").strip(),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB") or 12),
        session_hours=int(os.getenv("SESSION_HOURS") or 12),
        remember_days=int(os.getenv("REMEMBER_DAYS") or 30),
    )
