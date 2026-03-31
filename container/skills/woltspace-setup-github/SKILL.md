---
name: woltspace-setup-github
description: Set up a GitHub App for authenticated GitHub API access — issue creation, PRs, and more.
user_invocable: true
---

# GitHub App Setup

Guide the human through creating and configuring a GitHub App for their wolt. Step by step, one at a time.

**This skill is idempotent** — safe to run again. If things are already configured, validate and skip.

## Step 0: Check existing config

Check if GitHub App credentials are already configured:

```bash
grep -c "GITHUB_APP_ID" "$WOLT_DIR/.env" 2>/dev/null && echo "found" || echo "not found"
```

**If already configured:** Validate the credentials work:

```bash
gh-app-token >/dev/null 2>&1 && echo "✓ GitHub App auth working" || echo "✗ Auth failed"
```

If working, tell the human and stop. If auth fails, continue to help fix it.

## Step 1: Create a GitHub App

Tell the human:

> Go to **https://github.com/settings/apps/new** and fill in:
>
> - **App name:** anything unique (e.g. `woltspace-yourusername`)
> - **Homepage URL:** `https://github.com/jerpint/woltspace` (or your fork)
> - **Webhook:** uncheck "Active" — we don't need webhooks
> - **Permissions → Repository:**
>   - Issues: **Read & write**
>   - Pull requests: **Read & write**
>   - Contents: **Read & write** (for pushing branches)
> - Click **Create GitHub App**

Wait for confirmation before continuing.

## Step 2: Generate a private key

Tell the human:

> On your new App's settings page, scroll down to **Private keys** and click **Generate a private key**.
>
> A `.pem` file will download. Open it in a text editor and copy the full contents (including the BEGIN/END lines).
>
> Paste it here.

Once they paste it, format for `.env` — replace actual newlines with `\n` so it fits on one line:

```bash
# The key should look like: -----BEGIN RSA PRIVATE KEY-----\nMIIE...\n...\n-----END RSA PRIVATE KEY-----
```

Add to `.env`:
```
GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----
```

## Step 3: Get the App ID

Tell the human:

> On your App's settings page, near the top you'll see **App ID** — it's a number. Paste it here.

Add to `.env`:
```
GITHUB_APP_ID=<app_id>
```

## Step 4: Install the App on a repo

Tell the human:

> Go to your App's settings page → **Install App** (left sidebar) → click **Install** next to your account.
>
> Choose **Only select repositories** and pick the repo(s) you want the bot to access.
>
> After installing, look at the URL — it ends with `/installations/<number>`. That number is your installation ID. Paste it here.

If they can't find it from the URL, they can also get it from:

```bash
# After setting APP_ID and PRIVATE_KEY, this will list installations:
gh-app-token >/dev/null 2>&1 && echo "token works — installation ID is correct"
```

Add to `.env`:
```
GITHUB_APP_INSTALLATION_ID=<installation_id>
```

## Step 5: Test

Run the auth tool:

```bash
gh-app-token >/dev/null 2>&1 && echo "✓ GitHub App auth working!" || echo "✗ Failed — check credentials"
```

Then test with `gh` CLI:

```bash
GH_TOKEN=$(gh-app-token) gh api repos/<owner>/<repo>/issues --jq '.[0:3] | .[] | "#\(.number) \(.title)"'
```

If issues are listed, everything works. The bot can now create issues and PRs as `<app-name>[bot]`.

## Troubleshooting

- **"Missing env vars"** — check that all three vars are in `.env`: `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`
- **"doesn't look like a PEM key"** — the private key must start with `-----BEGIN RSA PRIVATE KEY-----`. Make sure newlines are escaped as `\n` in `.env`
- **"token exchange failed"** — the App might not be installed on the repo, or the installation ID is wrong
- **403 on issue creation** — check that the App has Issues: Read & write permission
