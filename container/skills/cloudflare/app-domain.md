# Configure or migrate the app domain

Give remotely shared apps their own wildcard domain and route it to the app gateway. Use this for an existing named tunnel or as the app-domain part of a new setup.

This changes Cloudflare DNS, tunnel ingress, Access, and lodge settings. Explain the plan and get the owner's approval before making those external changes. Inspect first and preserve unrelated Cloudflare configuration.

## Step 1: Choose a safe app domain

Recommend a separate registered domain, such as `*.theirapps.tld`. Cloudflare Universal SSL covers that one-level wildcard on the free plan.

A nested wildcard such as `*.apps.theirdomain.tld` can work, but it shares a site with the lodge and needs a Cloudflare Advanced Certificate that explicitly covers the nested wildcard. Do not imply that Universal SSL covers it.

The app domain must not equal or contain the lodge hostname. For example, if the lodge is `home.theirdomain.tld`, neither `home.theirdomain.tld` nor `theirdomain.tld` is valid as the app domain. The lodge rejects those unsafe overlaps.

Record the chosen bare domain without `*.` or a scheme:

```bash
APP_DOMAIN="theirapps.tld"
```

If it is a separate registered domain, make sure it is an active Cloudflare zone and use that zone's ID for DNS below.

## Step 2: Inspect the lodge and Cloudflare state

Use the current lodge API, never a literal port:

```bash
curl -fsS "$WOLTSPACE_API/settings/apps-domain"
GATEWAY_PORT=$(curl -fsS "$WOLTSPACE_API/settings/app-gateway" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["port"])')
printf 'App gateway port: %s\n' "$GATEWAY_PORT"
```

The API returns the effective gateway port; when it has not been customized, the default is the lodge port minus 660.

Load the existing Cloudflare credentials and identify the named tunnel. If `CLOUDFLARE_TUNNEL_ID` was not saved, list the account's tunnels and select the exact existing Woltspace tunnel rather than guessing:

```bash
source /workspace/wolts/.env
curl -fsS "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel?is_deleted=false" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
```

Set `TUNNEL_ID` and `APPS_ZONE_ID` to the confirmed values. Do not print or copy the tunnel token.

## Step 3: Create the wildcard DNS record

First list matching records so the operation is idempotent:

```bash
curl -fsS "https://api.cloudflare.com/client/v4/zones/$APPS_ZONE_ID/dns_records?type=CNAME&name=*.$APP_DOMAIN" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
```

If the exact proxied CNAME does not exist, create it:

```bash
curl -fsS -X POST \
  "https://api.cloudflare.com/client/v4/zones/$APPS_ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{\"type\":\"CNAME\",\"name\":\"*.$APP_DOMAIN\",\"content\":\"$TUNNEL_ID.cfargotunnel.com\",\"proxied\":true}"
```

## Step 4: Add gateway ingress without losing existing rules

Tunnel configuration updates replace the full configuration. GET the current object, add or replace only the exact `*.$APP_DOMAIN` rule, put it above every other wildcard rule and before the final catch-all, then PUT the complete configuration back.

```bash
curl -fsS \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" > /tmp/woltspace-tunnel-config.json

APP_DOMAIN="$APP_DOMAIN" GATEWAY_PORT="$GATEWAY_PORT" python3 - <<'PY'
import json, os
from pathlib import Path

source = Path("/tmp/woltspace-tunnel-config.json")
payload = json.loads(source.read_text())["result"]
config = payload["config"]
hostname = "*." + os.environ["APP_DOMAIN"]
rule = {"hostname": hostname, "service": "http://localhost:" + os.environ["GATEWAY_PORT"]}
rules = [item for item in config.get("ingress", []) if item.get("hostname") != hostname]
insert_at = next((i for i, item in enumerate(rules)
                  if str(item.get("hostname", "")).startswith("*.")
                  or "hostname" not in item), len(rules))
rules.insert(insert_at, rule)
config["ingress"] = rules
Path("/tmp/woltspace-tunnel-put.json").write_text(json.dumps({"config": config}))
PY

curl -fsS -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/woltspace-tunnel-put.json
```

Remove the two temporary JSON files after the PUT. If the GET shape differs or the existing configuration cannot be preserved exactly, stop instead of replacing it.

## Step 5: Create the wildcard Access application

Create one self-hosted Access application for `*.$APP_DOMAIN`. Allow the owner's email and anyone the owner wants to share with. Alternatively, an owner may choose an everyone-identified policy and rely on Woltspace's per-app share lists for authorization; explain that this lets any successfully identified Access user reach the gateway before Woltspace applies the app-specific list.

List Access applications and reuse the exact wildcard application if it exists:

```bash
curl -fsS "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"
```

For a new application, POST the complete object:

```bash
curl -fsS -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{\"name\":\"woltspace-apps\",\"domain\":\"*.$APP_DOMAIN\",\"type\":\"self_hosted\",\"session_duration\":\"24h\"}"
```

For an existing application, GET it by ID, preserve its complete writable configuration, change only the intended fields, and PUT the full application object back. Never send a partial PUT:

```bash
curl -fsS \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/$APP_ID" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"

# Build and inspect the complete writable application object as access-app-put.json.
# Preserve every existing setting that the owner did not ask to change.
curl -fsS -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/$APP_ID" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @access-app-put.json
```

The minimum complete object for a newly created wildcard application is:

```json
{
  "name": "woltspace-apps",
  "domain": "*.theirapps.tld",
  "type": "self_hosted",
  "session_duration": "24h"
}
```

Use the policy pattern from `add-access.md` for explicit email allow policies. Record the wildcard application's `aud` value as `apps_aud`.

Also inspect the existing lodge Access application and record its `aud` as `lodge_aud`. Read the team's Access domain from the existing Access organization or dashboard; it looks like `team.cloudflareaccess.com`.

## Step 6: Save Woltspace settings through the lodge API

Do not hand-edit `woltspace.json`. Preserve the existing access values where appropriate, then POST the full required access object:

```bash
curl -fsS "$WOLTSPACE_API/settings/access"

curl -fsS -X POST "$WOLTSPACE_API/settings/apps-domain" \
  -H "Content-Type: application/json" \
  --data "{\"apps_domain\":\"$APP_DOMAIN\"}"

curl -fsS -X POST "$WOLTSPACE_API/settings/access" \
  -H "Content-Type: application/json" \
  --data '{
    "access": {
      "team_domain": "<team>.cloudflareaccess.com",
      "lodge_aud": "<lodge Access application aud>",
      "apps_aud": "<wildcard app Access application aud>",
      "owner_email": "<owner email>"
    }
  }'
```

Confirm both POST responses contain `"ok": true`. App-domain and gateway lifecycle changes apply on the next lodge start; tell the owner that a restart is needed, but do not restart the lodge unless they explicitly ask.

## Step 7: Verify before removing anything old

After the owner restarts the lodge, choose a running app and verify the new edge route:

```bash
curl -sS -o /dev/null -D - "https://<app>.$APP_DOMAIN"
```

Expect an Access redirect or denial before authentication, not an origin error. Then have the owner open the same app URL while logged in and confirm the app loads.

Only after the new domain works, offer to remove the obsolete `*.theirdomain.tld` DNS, ingress, and Access application that routed app traffic to the lodge. Show the exact old objects and get explicit confirmation before deleting them. Preserve the lodge's own exact-host DNS, ingress, and Access application.

## Troubleshooting

- **Cloudflare 502** — confirm the wildcard ingress targets the effective gateway port returned by `/settings/app-gateway`, not the lodge port.
- **Wrong wildcard wins** — Cloudflare tunnel ingress is first-match; move the app-domain wildcard above an older wildcard.
- **Certificate error on a nested wildcard** — Universal SSL does not cover `*.apps.theirdomain.tld`; provision an Advanced Certificate or use a separate registered app domain.
- **Woltspace rejects the domain** — choose a domain that neither equals nor contains the lodge hostname.
- **Authenticated but forbidden** — verify the wildcard Access `aud` was saved as `apps_aud`, then inspect the app's Woltspace share list.
