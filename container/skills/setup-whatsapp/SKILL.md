---
name: setup-whatsapp
description: Help the lodge owner set up or check the WhatsApp Agent Platform connector from the Connectors page or terminal.
---

# WhatsApp setup

1. Check `woltspace paths` and `woltspace status` for this lodge. If WhatsApp is
   running, ask the owner to send `/help` in their test agent chat. If it replies,
   the connector is working. If it does not, check the connector status and the
   private log for an authentication error or a competing poller.
2. Have the owner open the WhatsApp agent chat and find its Agent Platform API
   key. Give them `woltspace whatsapp setup` to run in a private terminal on the
   lodge host or in its container shell. On a machine with more than one lodge,
   give a ready-to-paste command with this session's `WOLTSPACE_WOLTS_DIR`,
   `WOLTSPACE_PORT`, and `WOLTSPACE_PYTHON` followed by
   `-m woltspace whatsapp setup`. Pass the same config override if this lodge
   uses one. The command hides the key and discovers the creator ID from a
   fresh marker if needed. Keep the key out of chat, tool arguments, and logs.
3. Once the command confirms it saved the config, have the owner restart this
   lodge using its normal lifecycle. On a native lodge use `woltspace restart`
   with the same environment. For a container use the host's container tooling.
   The command prints this handoff because enabling a connector takes effect at
   boot. Verify WhatsApp is running on the Connectors page after reload.
4. Ask the owner to send `/help` from that agent chat. A command list proves
   delivery both ways. If the owner wants to verify session replies, have them
   send a short message, choose a wolt if prompted, and check the reply arrives.
