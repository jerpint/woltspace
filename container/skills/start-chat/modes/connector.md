# Chat Connector Session

You were dispatched from a chat connector (a channel such as Discord or Matrix,
named in your boot context: "This session was started from …"). The owner is
reading your updates in that chat. They are not watching this terminal, so
your `notify` messages are how they see your work.

## Notifications

Your boot context and every incoming message include a complete reply heredoc
with the right route, in the general form:

```bash
notify --to CONNECTOR:ROUTE <<'WOLTSPACE_NOTIFY_7F3A91C2'
your message here
WOLTSPACE_NOTIFY_7F3A91C2
```

Replace only `YOUR_REPLY` and run it. Never change the route: the connector
checks it, and a route it does not accept is refused. Use a fresh random
delimiter if you write one yourself.

What a connector can carry depends on the connector. If `--file`, `--voice`
or `--speak` is refused, send the same message as text.

**When you start**: a one-line acknowledgement.

**When you're done**: a complete summary via notify, written for chat: short
paragraphs, no code blocks or logs. The reader should not need to open the
session. Print your full detailed output to this terminal too.
