# Custom creatures

The lodge has three sizes: 🦦 otter (quick), 🦫 beaver (builder) and 🦝
raccoon (thinker). A custom creature is a new name and emoji on one of them,
with its own model per engine and a default role if you want them. It
behaves exactly like its size: the same site, model tier and lore. Only
what people see changes, everywhere a wolt is shown: Telegram, Slack, the
lodge, the TUI, and the wolt's site.

There is no fixed list of creatures to grow. A new model gets a creature
when you register one.

## Register a creature, then give it to a wolt

```bash
woltspace creature add star --emoji ⭐ --size raccoon --model codex=gpt-6-astra
woltspace creature assign alpha star
woltspace creature list
woltspace creature rm star        # only once no wolt is a star
```

Or ask a wolt: the `creatures` skill lets a wolt register a creature or
change a wolt's creature, its own included, and it always asks you first.

- **Name:** lowercase letters, digits and hyphens, up to 20. Built-in names
  (raccoon, wolf, dog, ...) are taken.
- **`--size`:** `otter`, `beaver` or `raccoon`: what it behaves like.
- **`--model HARNESS=MODEL`:** optional, repeatable. The model must be one
  the engine offers (`GET /harnesses`).
- **`--role`:** optional, the role a new wolt of this creature starts with.
  It is only that default: assigning the creature to an existing wolt never
  changes the wolt's own role.
- `add` never overwrites, and `rm` refuses while a wolt is that creature.

New wolts can be created as a custom creature too (`POST /sessions/new/create`
with `"type": "star"`).

## Which model a session gets

1. The wolt's own pinned model (`wolt.json` `"model"`), when the engine
   offers it.
2. The creature's own model for that engine.
3. The size's default for that engine (see `switching-models.md`).

## When a change applies

`assign` changes only the wolt's `"type"`. Its own engine, model and role
stay, and the command lists them. A running session keeps the model it was
started with, including after it rests and wakes. The wolt's next session
gets the new creature.

A starter `site.json` that still holds the old creature's emoji follows the
new one. An emoji the wolt picked for its site stays.

## Where they live

In the lodge's `woltspace.json`, next to the singleton bookkeeping:

```json
"creatures": {
  "active_wolf": "clockwork",
  "custom": {
    "star": {"emoji": "⭐", "size": "raccoon", "models": {"codex": "gpt-6-astra"}}
  }
}
```

A lodge without `"custom"` has only the built-ins, exactly as before. Write
it with the command, not by hand: an entry that doesn't validate is ignored.

The API behind the command: `GET /creatures`, `POST /creatures`,
`DELETE /creatures/{id}`, `POST /wolts/{name}/creature`. `/wolts` now also
sends each wolt's `emoji` and `base_type` (its size, for a custom creature).

## Not yet

Custom pixel art (with the emoji as fallback), and wolf, dog and the other
special creatures as entries of the same kind.
