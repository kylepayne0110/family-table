# io wraps uploaded bytes so Pillow can read them.
import io

# re validates stored photo filenames.
import re

# secrets builds random, unguessable filenames.
import secrets

# warnings lets us treat oversized images as errors.
import warnings

# Path provides filesystem utilities.
from pathlib import Path

# Pillow opens, rotates, resizes, and re-encodes images.
from PIL import Image, ImageOps, UnidentifiedImageError


# Longest edge of a stored photo, in pixels.
MAX_EDGE = 1800

# Refuse absurdly large images (protects a small server's memory).
MAX_PIXELS = 50_000_000

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}  # MPO = some phone JPEGs

# Only names we generated are ever served or deleted.
FILENAME_PATTERN = re.compile(r"^[a-f0-9]{32}\.webp$")


class ImageError(ValueError):
    """An upload that isn't a usable photo."""


def process_photo(data: bytes, upload_dir: Path, max_bytes: int) -> str:
    """Validate, orient, resize, and save a photo; return its new filename."""
    if not data:
        raise ImageError("The photo file was empty.")
    if len(data) > max_bytes:
        raise ImageError(f"Photos must be under {max_bytes // (1024 * 1024)} MB.")

    try:
        with warnings.catch_warnings():
            # Pillow warns (not errors) on very large images; make it an error.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format not in ALLOWED_FORMATS:
                    raise ImageError("Please upload a JPEG, PNG, or WebP photo.")
                if probe.width * probe.height > MAX_PIXELS:
                    raise ImageError("That photo's dimensions are too large.")
                probe.verify()  # catches truncated or corrupt files

            # verify() consumes the file, so reopen to actually decode it.
            with Image.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img)  # fix phone rotation
                img = img.convert("RGB")
                img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    except ImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, SyntaxError) as exc:
        raise ImageError("That file doesn't look like a valid photo.") from exc

    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}.webp"
    final_path = upload_dir / filename
    temp_path = upload_dir / f".{filename}.tmp"

    # Write to a temp file, then rename, so a crash never leaves half a photo.
    img.save(temp_path, "WEBP", quality=82, method=4)
    temp_path.replace(final_path)
    return filename


def photo_path(upload_dir: Path, filename: str) -> Path | None:
    """Return the path for a stored photo, or None if the name is unsafe."""
    if not FILENAME_PATTERN.fullmatch(filename or ""):
        return None
    return upload_dir / filename


def delete_photo(upload_dir: Path, filename: str | None) -> None:
    """Remove a stored photo if it exists."""
    path = photo_path(upload_dir, filename or "")
    if path is not None:
        path.unlink(missing_ok=True)
