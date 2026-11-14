"""Set the shared family username and password.

Run from the project folder:  python -m app.setup_login
"""

# getpass reads a password without echoing it to the screen.
from getpass import getpass

# Path provides filesystem utilities.
from pathlib import Path

# set_key safely updates a single value inside .env.
from dotenv import set_key

# Project helpers.
from app.auth import hash_password
from app.config import BASE_DIR


ENV_PATH: Path = BASE_DIR / ".env"
MIN_PASSWORD_LENGTH = 8


def main() -> None:
    print("Family Table: set the shared family login.\n")

    username = ""
    while not username:
        username = input("Username: ").strip()

    while True:
        password = getpass(f"Password (at least {MIN_PASSWORD_LENGTH} characters): ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print("That's too short. Try again.")
            continue
        if getpass("Type it again: ") != password:
            print("Those didn't match. Try again.")
            continue
        break

    ENV_PATH.touch(exist_ok=True)
    # Single quotes stop .env tools from expanding anything in the hash.
    set_key(ENV_PATH, "FAMILY_USERNAME", username, quote_mode="always")
    set_key(ENV_PATH, "FAMILY_PASSWORD_HASH", hash_password(password), quote_mode="always")

    print(f"\nSaved to {ENV_PATH}. Only a secure hash of the password was stored.")
    print("Restart the server if it's running, then sign in.")


if __name__ == "__main__":
    main()
