# Switching a wolt's engine and model

A wolt runs on a **harness** (the engine — Claude Code, Codex, …) and a **model**
(the specific model that engine uses). Both are chosen when a session spawns and are
**frozen for that session's life** — switching changes the *next* session, never a
running one.

This is a per-wolt setting: change it and every new session for that wolt picks it up.

## How to switch

Edit the wolt's `wolt/wolt.json`:

```jsonc
{
  "type": "raccoon",        // the creature tier — lore/identity, do not change to switch models
  "harness": "codex",       // OPTIONAL engine override; omit to follow the lodge default
  "model": "gpt-5.6-sol"    // OPTIONAL model pin; omit to use the tier's default model
}
```

- **Change the engine** — set `"harness"` to a valid id (`claude`, `codex`, …). Omit the
  field entirely to follow the lodge-wide default.
- **Change the model** — set `"model"` to a valid id for that engine. Omit it to fall back
  to the engine's default model for the wolt's tier.

The change takes effect on the wolt's **next** session. A wolt can do this to itself, or
do it on a user's behalf — it's just a config edit.

## Which ids are valid?

Never hardcode a model list — it drifts. Ask the platform:

```
GET /harnesses
```

Returns, per engine: its `label`, the default `models` per tier, and the full selectable
`catalog` of `{id, label}`. The `model` you pin must be an `id` from that engine's catalog.

## The one rule worth knowing

A model pin is **engine-scoped** — `opus` means nothing to Codex, `gpt-5.6-sol` means
nothing to Claude. If a wolt's pinned model isn't valid for its resolved engine (e.g. you
switched engines but left an old model pin), the platform silently falls back to the tier
default at spawn. So switching engines never strands a wolt on an invalid model, and
removing a model from the catalog can never leave a wolt pinned to something that's gone.

## Where the catalog comes from

The selectable models live as data, not code:

- **Built-in seed** — `container/lib/harnesses.py`, each harness's `model_catalog`. Ships
  sensible defaults so a fresh install works with no config.
- **Overlay** — `woltspace.json` `harness.models.<engine>` can override or extend it
  without touching code (survives rebuilds, UI-writable):
  ```jsonc
  "harness": {
    "models": {
      "claude": { "catalog": ["opus", "sonnet", "haiku", "fable"],  // add / hide models
                  "tiers":   { "otter": "fable" } }                 // override a tier default
    }
  }
  ```
  The overlay `catalog` replaces the seed list, so hiding a model is just leaving it out.
  Adding one only needs its id; the label falls back to the seed's (then to the id).

See also `adding-a-harness.md` for adding a whole new engine.
