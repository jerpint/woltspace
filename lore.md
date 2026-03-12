# Woltspace Lore

*Starts simple. Grows with the colony.*

---

## The Shape of Things

**Woltspace** is the whole thing. Where wolts live and work.

**The lodge** is home base. The kit lives here. This is where the human and wolt talk day-to-day — Telegram, Slack, casual conversation, quick tasks. Warm, always-on.

**The den** is where work happens. Wolts go to dens to build — Claude Code sessions, real tasks, focused execution. A den is spawned when something serious needs doing, dissolved when the work is done.

**Wolts** are the builders. Each wolt has a lodge — memory, site, history, identity. They do the real work in dens.

**The kit** is the lodge presence — the always-on, lightweight agent that handles the lodge. Fast, helpful, not a builder. The kit knows the wolt's memory (same file structure) but operates at a different level: it can't evolve the way a wolt can.

---

## In Practice

```
woltspace         — the whole thing
  lodge           — home base. kit lives here.
    kit           — always-on lodge presence. handles day-to-day.
    wolt          — the builder. has memory, site, identity.
  dens            — where wolts work. spawned per task, dissolved when done.
```

- Kit and wolt share the same memory file structure — swappable in principle
- Kit is fundamentally lighter: same home, different capacity
- Wolts go to dens. Kits stay in the lodge.

---

## What This Means for Code

- **Kit = the bot process.** Always running. Lightweight (Haiku). Lives in the lodge.
- **Den = a Claude Code session.** Carries `WOLT_NAME` + `WOLT_DIR`. Spawned by kit when real work arrives.
- **A wolt = a directory.** `wolts/{name}/` — lodge lives here.
- **Memory is shared format.** Kit and wolt read the same files. This is intentional.

---

*Lore v1. March 2026. Colony of one: nw 🌲.*
