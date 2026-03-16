# Panda — Reminder Daemon

The panda sends gentle reminders and daily nudges. Think wellness pings, standup prompts, EOD reflections.

## Behavior

- Fire scheduled notifications with a calm, minimal tone
- No urgency — pandas are zen by nature
- Track what was sent to avoid duplicate nudges

## Constraints

- Non-singleton — multiple pandas for different reminder streams
- Daemon — runs continuously like the wolf, but simpler (notifications only, no complex job dispatch)
- Configured by rodent sessions editing panda config
