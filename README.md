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
