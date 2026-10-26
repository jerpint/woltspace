# Woltspace 0.5.23

Python **0.5.23** is published by itself. **@woltspace/tui** stays at
**0.5.3**: nothing in the TUI changed.

## Thank you

- [@asharakeh](https://github.com/asharakeh) moved the lodge's state into one
  database and rebuilt backups around it (#598, from #593 to #597).

## What's new

- **Lodge state lives in one database.** Sessions, auto-grants and dig grants,
  cron run records, chat routing, the Slack queue and running apps move from
  hundreds of small JSON files and lock files into `.space/lodge.db`
  (SQLite). Writes no longer race, and the state can be backed up as one
  consistent piece. Memory, manifests, sites, drafts and skills stay plain
  files. The CLI and API wolts use are unchanged (#598).
- **`woltspace db show <table>`** prints any table of the lodge database,
  read only, with `--where COL=VALUE` and `--json` (#598).
- **Backups take a consistent database snapshot** and can encrypt the whole
  archive. `docs/backup.md` has a recipe for an off-machine bucket
  (Cloudflare R2) and a new setup-backup skill walks through it (#598).

## Changes to know about

- **The first boot imports the old state files** into `lodge.db` and renames
  each one it read to `<name>.imported`. A store that fails to import keeps
  its files and is tried again at the next boot. Read `/health`: `db.error`
  and `db.import_error` must be absent and `db.imported` lists every store.
- **A `lodge.db` this build can't open stops the boot** with one line naming
  the file and the reason, before anything is imported. The file is left
  untouched.
- **Going back to 0.5.22 needs the steps in `docs/backup.md`** ("First boot on
  the lodge DB, and how to go back"): put the old files back and set
  `lodge.db` aside. State written on 0.5.23 doesn't carry back, and crons
  that ran in the last 24 hours run once more on the older release.
- **Plaintext backups no longer include `.space/platform/config.json`**, which
  holds bot tokens. Encrypted backups still do. After restoring a plaintext
  backup, enter the tokens again.

## Upgrade notes

Take a backup first, with the release you are leaving:

```sh
woltspace backup --tag pre-upgrade
woltspace update
```

Native upgrades stop and restart the control plane while preserving tmux
sessions. Container lodges update from the host as before. On Docker Desktop,
don't open `lodge.db` from the host; `docs/backup.md` has the commands to run
inside the container.

The TUI is unchanged at 0.5.3.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
