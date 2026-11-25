---
name: usage
description: Answer questions about the lodge's LLM token usage (how many tokens, which wolt, session, provider or model, this week or any period, plan limits) with fresh numbers from `woltspace usage`, as a table, a summary, JSON or CSV.
---

# Lodge usage

`woltspace usage` counts the lodge's Claude and Codex token usage from the
engines' own transcripts, fresh on every call, and prints it. It only reads:
nothing is saved by a query. The lodge saves its usage history once a night
(the wolf's built-in `usage-save` at 23:55), so days stay countable after the
engines delete old transcripts.

```bash
woltspace usage summary --since 7d                      # totals and plan limits
woltspace usage table --by wolt --since 30d             # one row per wolt
woltspace usage table --by day,provider --since 14d
woltspace usage table --by session --wolt alpha --top 10
woltspace usage table --by model --provider codex --format json
woltspace usage limits                                  # latest plan-limit readings
```

Each call takes a few seconds (it reads every transcript). Run it once per
question and work from its output; never loop it or poll it.

## Options

- `--by` (table only): any comma-separated mix of `day,wolt,session,provider,model`.
  Default `wolt`.
- `--since`: `Nd` (the last N days, today included) or a date `YYYY-MM-DD`.
  `--until`: a date, inclusive. Days are calendar days in the lodge's own time
  zone (the output names it). Without dates: all saved history.
- `--wolt`: a wolt's full name or the first part of it before a hyphen
  (`alpha` matches `alpha-the-builder`). A part that matches two wolts is refused.
- `--provider`: `claude` or `codex`. `--model`: an exact model name.
- `--metric`: show and rank by one of `input_fresh`, `cache_read`, `cache_write`,
  `output`, `sessions`. Without it, tables rank by `output` and show them all.
- `--top N` (table only): at most N rows.
- `--format`: `table` (default), `json` or `csv` (for a spreadsheet).

A bad option is refused with a short reason (exit 2). If the count itself fails
the command says so (exit 1); report that, don't guess numbers.

## Reading the numbers

- `input_fresh`: input tokens that were not served from a cache.
- `cache_read`: input served from the provider's cache.
- `cache_write`: input written to the cache (Claude only).
- `output`: generated tokens. Codex output includes its reasoning.
- `sessions`: distinct engine sessions.

Every answer starts with `last_collected` (when it was counted),
`history_starts` (the earliest day on record, not a promise that every day
since is complete) and `timezone`. Mention `coverage_gap` rows that touch the
period you report: they mean some transcripts were unreadable, expired before
a save, or the provider's own totals disagreed; treat that part as unknown.
Plan limits are the latest readings with their observation and reset times; a
missing reading prints as unavailable. Claude limits appear only when the lodge
has set up its optional status-line feed.

The usage page in the lodge (`/usage`) shows the same numbers with charts and
an activity calendar.

## Keep them in the lodge

Usage numbers describe your human's lodge and spending. Use them inside the
lodge (messages to your human, lodge pages, other wolts). Never put them in
anything public (an issue, a pull request, a public page or shared room)
unless your human has cleared it.
