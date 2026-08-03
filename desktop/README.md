# Woltspace Desktop MVP

The desktop app is a thin Tauri 2 shell around the existing lodge. Its bundled UI only handles startup and recovery; once the local engine is healthy, the same web lodge served at `http://127.0.0.1:7777` fills the window.

## Prerequisites

- macOS 12 or newer
- Docker Desktop
- Node.js 22+
- Rust stable and the [Tauri 2 macOS prerequisites](https://v2.tauri.app/start/prerequisites/)

## Browser preview

The preview uses the same controller and a mock engine, so it needs neither Docker nor macOS:

```sh
cd desktop
npm install
npm run dev
```

Open the printed `127.0.0.1` URL. To produce the static preview, run `npm run build` and `npm run preview`.

## Run and build the macOS app

```sh
cd desktop
npm install
npm run tauri dev
npm run tauri build
```

The release build produces an `.app` and `.dmg`. Distribution outside local development still requires Apple signing/notarization credentials; configure those through the standard Tauri signing environment variables in CI.

Set `WOLTSPACE_IMAGE` before launching to override `woltspace/woltspace:latest`. The app owns a container named `woltspace`, persists all user state under `~/.woltspace/wolts`, reads `~/.woltspace/wolts/.env` when present, and publishes only `127.0.0.1:7777`.

Finder-launched apps resolve Docker from Docker Desktop, `~/.docker/bin`, Intel/Homebrew locations, and finally `PATH`. When Docker Desktop is installed but stopped, Woltspace launches it and waits for the engine automatically. Unix host UID/GID values are passed to the container so mounted state retains the expected ownership.

## Desktop behavior

- Closing the main window hides it; click the tray icon to restore it, or choose Quit from its menu.
- External HTTP and HTTPS links open in the system default browser instead of creating an embedded WebView window.
- “Try a notification” requests native permission and sends a demonstration alert when granted.
- `woltspace://session/<session-name>` opens the corresponding existing `/tui?session=…` route on launch.
- “Show data folder” reveals only `~/.woltspace/wolts`.

## Security and test boundary

The frontend cannot execute arbitrary programs. Rust exposes only Docker detection, container status/start, image pull, fixed container creation, bounded logs, and data-folder reveal. Docker is invoked without a shell; the fixed run shape mounts only the Woltspace state folder and binds the lodge to localhost.

```sh
cd desktop
npm test
npm run lint
npm run build
cargo test --manifest-path src-tauri/Cargo.toml
cargo check --manifest-path src-tauri/Cargo.toml
```

Frontend tests use a mock adapter. Rust unit tests inject a command runner, so neither test suite needs Docker. macOS-specific packaging should run on a macOS CI runner; `cargo test` and `cargo check` can run on Linux when the Tauri system packages are installed.
