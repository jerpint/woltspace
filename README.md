# 🦫 woltspace

give your wolt space to build.

| Audience | File |
|----------|------|
| Humans | [HUMANS.md](HUMANS.md) |
| Developers / Agents | [CLAUDE.md](CLAUDE.md) |
| Running it natively or in Docker | [docs/native-and-container.md](docs/native-and-container.md) |
| First native run on a Mac | [docs/native-first-run.md](docs/native-first-run.md) |

The Tauri 2 desktop shell, browser preview, and build instructions live in [desktop/](desktop/README.md).

## Updates

Ask your wolt to update a native lodge, or use the standard uv upgrade workflow.
See [the update instructions](docs/updates.md).

Since 0.5.4, `uv tool install woltspace` includes all Telegram and Slack connector
dependencies. Channels still run only when enabled in your configuration.

For local workflows shared by every wolt, see [Shared lodge skills](docs/shared-skills.md).

To publish a tiny starter team rather than private lodge history, see
[Public colonies](docs/public-colonies.md). Public colonies are ordinary Git
repositories containing selected wolt identities, authored rules, explicit
skills, and app source or pinned public Git references.

For focused CI coverage and checkout test setup, see [Testing](docs/testing.md).

`woltspace backup` creates a verified data archive; `woltspace restore` extracts
into a new directory. When moving backups across filesystems, an archive with
names differing only by case (for example `Notes.md` and `notes.md`) restores
both on a case-sensitive target. On a case-insensitive target, restore refuses
those collisions with an error before extraction. This includes typical macOS
volumes; the actual target filesystem determines the behavior.
