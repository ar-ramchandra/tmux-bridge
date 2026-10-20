# Optional Cloudflare and private mTLS transport

This is an advanced, operator-configured deployment. The public alpha does not automatically create a complete new Cloudflare account/tunnel configuration. Standard local/SSH/private-HTTPS pairing works independently of Cloudflare.

## Two distinct routes

| Route | Destination on the gateway | Admission |
|---|---|---|
| `terminal.example.com` | `http://127.0.0.1:4601` | Browser Access application, restricted to your operator identities |
| `agents.example.com` | `tcp://127.0.0.1:14610` | Separate machine Access application, Service Auth policies restricted to individual service tokens |

The machine route carries an inner TLS 1.3 stream. `mtls_proxy.py` terminates that stream using your private CA and relays only authenticated `/server-ws` connections to the gateway. Cloudflare's mTLS product is not used. Browser HTTPS remains a separate connection terminated at Cloudflare.

Keep both listeners loopback-only. The tunnel connector must validate the relevant Access JWT audience for each route. Do not add a public bypass policy for the browser, agent endpoint, pairing route or health checks. The machine endpoint must not proxy the entire gateway HTTP API.

Consult the official [service-token documentation](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/) and [tunnel origin parameters](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/). Cloudflare APIs and dashboard labels can change.

## Local transport identity

After `tmux-bridge init`, activate the project's Python environment and create the transport identity once:

```python
from pathlib import Path
import json
from transport_pki import init_ca, issue, write_private

root = Path.home() / '.config/tmux-bridge'
transport = root / 'transport'
init_ca(transport / 'ca')
issue(transport / 'ca', 'agents.example.com', transport / 'server', server=True, days=90)
write_private(transport / 'allowed-clients.json', b'{}\n')
relay = {
    'port': 14610,
    'upstream': 'ws://127.0.0.1:4601/server-ws',
    'certificate': str(transport / 'server/tls.crt'),
    'private_key': str(transport / 'server/tls.key'),
    'client_ca': str(transport / 'ca/ca.crt'),
    'allowed_clients': str(transport / 'allowed-clients.json'),
}
write_private(transport / 'relay.json', json.dumps(relay, indent=2).encode())
```

Replace the example hostname before running. Existing files are not overwritten. The transport CA is separate from the application CA. Run the relay with `python -m mtls_proxy --config ~/.config/tmux-bridge/transport/relay.json` and keep it supervised alongside the gateway and connector.

## Operator metadata for bundle generation

The generator currently uses these filenames as its metadata contract. They are local private state, not files to add to this repository.

`~/.config/tmux-bridge/cloudflare-deployment.json`:

```json
{
  "account_id": "YOUR_ACCOUNT_ID",
  "tunnel_id": "YOUR_TUNNEL_ID",
  "team": "YOUR_ZERO_TRUST_TEAM_NAME"
}
```

`~/.config/tmux-bridge/transport/cloudflare-test.json` (legacy filename retained for compatibility):

```json
{
  "domain": "agents.example.com",
  "application": {
    "id": "YOUR_MACHINE_ACCESS_APPLICATION_ID",
    "aud": "YOUR_MACHINE_ACCESS_AUDIENCE"
  }
}
```

Use actual values from your machine Access application. On its ingress rule, the generator expects:

```json
{
  "hostname": "agents.example.com",
  "service": "tcp://127.0.0.1:14610",
  "originRequest": {
    "access": {
      "required": true,
      "teamName": "YOUR_ZERO_TRUST_TEAM_NAME",
      "audTag": ["YOUR_MACHINE_ACCESS_AUDIENCE"]
    }
  }
}
```

Merge that rule into your existing tunnel configuration, preserving unrelated routes and a final catch-all 404. Configure the browser route with its own Access audience. Verify unauthenticated requests fail before attempting enrollment.

Store the administrative API token in `~/.config/tmux-bridge/cloudflare-api-token`, mode 0600. The containing directories should be mode 0700. The generator reads the tunnel configuration and Access application/policies, and creates/deletes service tokens and application policies. Scope the API token to the intended account and those capabilities; bundle generation does not need DNS write permission or access to unrelated accounts. Use the Cloudflare permission catalog to select the corresponding current token grants.

Then follow [private node installers](add-nodes.md). The generator verifies this state before issuing a bundle. Failure or policy mismatch must be corrected at the source; do not weaken its checks to proceed.

## Certificate maintenance

Renew the server certificate before expiry with the same hostname, stage the new files privately, verify the key and certificate, update the relay configuration and restart the relay. Existing connections will reconnect; keep independent SSH/private-network access available. Do not rotate the CA casually, because all clients trust it.

Node renewal currently uses a replacement bundle/name as described in [add nodes](add-nodes.md). Removing a fingerprint rejects new connections; restart the relay to terminate existing ones immediately. A relay restart affects every node using it.

This project neither stops nor reconfigures Meshnet, Tailscale or other recovery access. There is no automatic fallback between transport modes in the public release.
