# Remove Cloudflare Access Permission

Revoke a previously-granted email's access to one app, the lodge, or the wildcard.

Removing a policy is instant at the edge — the next request from that email gets the login page and fails to authenticate. Existing browser sessions stay valid until their `session_duration` expires; if the revocation is urgent, also revoke the user's session in **Zero Trust → My Team → Users**.

## Step 1: Gather info

Ask the user:
1. **Email to remove** — e.g. `friend@example.com`
2. **Scope** — which domain are they being removed from? (Same options as `add-access.md`: lodge, one app, or wildcard.)

If you're not sure where the email currently has access, list every Access app and inspect their policies (Step 3) — the email will appear in the `include` of every policy that grants it access.

## Step 2: Check env

```bash
source /workspace/wolts/.env
echo "API_TOKEN=${CLOUDFLARE_API_TOKEN:+SET}"
echo "ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-NOT SET}"
```

If either is missing, the named tunnel hasn't been set up yet — point the user at `setup.md` first.

## Step 3: Find the policy

List Access apps and their policies. This is verbose by design — show the user every policy that includes the target email so they can confirm which ones to remove.

```bash
source /workspace/wolts/.env
EMAIL="<email>"

curl -s "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -c "
import json, sys, urllib.request, os

email = '$EMAIL'
account_id = os.environ['CLOUDFLARE_ACCOUNT_ID']
token = os.environ['CLOUDFLARE_API_TOKEN']

apps = json.load(sys.stdin)['result']
for app in apps:
    pol_url = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/access/apps/{app[\"id\"]}/policies'
    req = urllib.request.Request(pol_url, headers={'Authorization': f'Bearer {token}'})
    pols = json.loads(urllib.request.urlopen(req).read())['result']
    for p in pols:
        emails = [i.get('email', {}).get('email') for i in p.get('include', []) if 'email' in i]
        if email in emails:
            print(f\"  app={app['name']} ({app.get('domain','')})  policy={p['name']}  app_id={app['id']}  policy_id={p['id']}\")
"
```

Show the matching rows to the user and ask which policy (or policies) to delete. Don't delete anything without explicit confirmation — Cloudflare deletions are immediate.

## Step 4: Delete the policy

For each policy the user confirmed:

```bash
source /workspace/wolts/.env
APP_ID="<app_id>"
POLICY_ID="<policy_id>"

curl -s -X DELETE \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/$APP_ID/policies/$POLICY_ID" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('Deleted' if r.get('success') else f'FAILED: {r}')
"
```

If the email was the *only* allowed user on a per-email policy (the typical case from `add-access.md`), the entire policy goes away. If the email was one of many on a shared `allow-owner` policy, deletion would also remove access for the other users — in that case PUT the policy with the email filtered out of the include list instead of DELETE-ing.

## Step 5: Confirm

Tell the user:
- Which policy (or policies) were removed and from which app
- That the email can no longer get a fresh OTP for that domain
- That existing browser sessions remain valid until they expire — point at **Zero Trust → My Team → Users → revoke session** if immediate revocation matters
- That nothing else changed (the tunnel and other users keep working)
