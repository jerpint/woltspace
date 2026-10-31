# Connect With a Code

Someone who runs Cloudflare for this lodge gave the owner one connection code, starting `woltspace-cloudflare-1.`. It holds the lodge's address, its tunnel token, and who may log in. There's nothing to set up on Cloudflare.

**The code is a secret.** Don't repeat it, write it to your notes or a file you keep, or put it on a command line.

## Steps

1. **Check the lodge is on 0.6 or newer:** `woltspace --version`. If it's older, update first (the update skill).
2. **Apply it.** Replace `THE_CODE` and run this. stdin keeps the code off the command line:
   ```bash
   woltspace connect cloudflare <<'WOLTSPACE_CODE_<16_RANDOM_HEX>'
   THE_CODE
   WOLTSPACE_CODE_<16_RANDOM_HEX>
   ```
   Running it twice changes nothing the second time. If the lodge already has another address, it stops and says so: moving it is the owner's decision, so ask them first, then add `--replace`.
3. **Tell the owner:** open the address it printed, from any device, and log in with the email it named. A new address can take a few minutes to start working.

If the owner would rather keep the code out of this session's transcript, they can run step 2 in their own terminal.

If the tunnel doesn't come up, the code may be old: ask for a new one. Don't look for the token in `.env` or print it.

**In a container whose env file already had `CLOUDFLARE_` values:** those win again the next time the container is created. The owner needs to update that file too.
