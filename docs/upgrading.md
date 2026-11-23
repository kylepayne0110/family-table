# Upgrading to accounts and Tables (and recovering if needed)

This release changes the database from one shared login to individual accounts and shared
**Tables**. The upgrade runs by itself the first time the new version starts, makes a backup
first, and either finishes completely or changes nothing.

## What happens

1. A full copy of the database is written to `data/backups/family_table-before-accounts-<time>.db`.
   Photos in `data/uploads/` aren't touched (every file stays where it is).
2. Everything moves into one Table: recipes (same numbers, so old links like `/recipes/12`
   still work), photos, notes, planned dinners and history, packaged meals and label photos,
   serving changes, grocery items and their purchased state, Kitchen Staples, and Cooking
   Assistant conversations.
3. The shared login in `.env` (`FAMILY_USERNAME` / `FAMILY_PASSWORD_HASH`) becomes the first
   account, with the **same username and password**, and is that Table's member.
4. Row counts and every reference between tables are checked before anything is saved.
5. Everyone is signed out once (the old shared sessions don't belong to an account).

## Steps on the home computer

```bash
# 1. Stop the running server (Ctrl+C in its terminal).
# 2. Optional, but recommended: an extra copy of everything.
cp -a data ~/family-table-data-before-accounts
# 3. Optional: your name, shown in invites ("Kyle invited you to Payne's Table").
#    Without it the account is named after the username ("Payne"); you can change it later in
#    Account. The Table is named after the username: "Payne's Table" (FAMILY_TABLE_NAME overrides).
echo "FAMILY_DISPLAY_NAME=Kyle" >> .env
# 4. Get the new code and start as usual.
git pull            # or check out the release branch
./scripts/serve.sh
```

The terminal prints `Upgraded the database to accounts and Tables. Backup of the original: …`.
Sign in with the same username and password as before.

Then, for Nat: **Table name in the header → Make an invite link**, and send her the link. She
sees "Kyle invited you to Payne's Table", taps **Create an account and join**, and lands
straight in the Table. Each link works for one person, for 7 days.

## Checking it

- Recipes, photos, this week's plan, the grocery list, and past dinners look the same.
- **Account** shows your name. If it says "Payne", change it there (invites show this name).
- `python -m app.accounts list` shows the account and its Table.

## Recovering

If anything looks wrong, put the original back. Nothing is merged or lost:

```bash
# Stop the server first (Ctrl+C).
mv data/family_table.db data/family_table.db.after-upgrade
rm -f data/family_table.db-wal data/family_table.db-shm
cp data/backups/family_table-before-accounts-*.db data/family_table.db
git checkout feature/scan-packaged-meal    # the previous version of the code
./scripts/serve.sh
```

The previous version runs normally on the restored file. Starting the new version again simply
repeats the upgrade (with a new backup).

If the upgrade itself fails, the server stops with a message and the database is exactly as it
was; the backup is there as well.

## Passwords and accounts later

- **Forgot password**: Family Table doesn't send email. Whoever runs the server makes a one-time
  link: `python -m app.accounts reset-link USERNAME --base https://<your address>` (works once,
  for an hour, and signs that account out everywhere).
- **From the server itself**: `python -m app.setup_login` sets a new password for an existing
  username, or creates an account.
- `FAMILY_USERNAME` / `FAMILY_PASSWORD_HASH` in `.env` are only read for the upgrade (or to create
  the first account on a brand-new install). Later password changes happen in the app.

## Docker

The same upgrade runs inside the container on first start; the backup lands in the mounted data
folder (`data/backups/`). Add `FAMILY_DISPLAY_NAME` / `FAMILY_TABLE_NAME` to `.env` beforehand if
you like.
