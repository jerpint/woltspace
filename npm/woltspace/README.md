# woltspace

Give your wolt space to build: a home for the coding agents you already use.

```bash
npx woltspace
```

- With a lodge running, this opens the terminal cockpit (`@woltspace/tui`).
- Without one, it lists what is missing and shows the line to paste into your
  agent so it installs Woltspace for you. It installs nothing itself.

Publish this only after an `@woltspace/tui` release that has the no-lodge
checklist (0.5.3 or later): with an older tui, `npx woltspace` would open the
cockpit instead.

Woltspace installs with uv. The steps live in one place, the README:
https://github.com/jerpint/woltspace
