# Private application previews

Run a web app on `127.0.0.1:3000` on a connected node. Click its localhost link in
the terminal, or open **Previews** in the sidebar and enter `3000`. The gateway
opens an isolated address in a new tab, including on phones and tablets.
Each preview belongs to the selected tmux session, uses one explicit loopback
port, and expires after two hours. The sidebar shows its remaining lifetime.
**Stop** removes remote access and closes active connections. It does not kill
the development server or change files. Closing a browser tab alone does not
stop a preview. Killing the owning tmux session also revokes its previews.

## Agents and scripts

On the node, using its bridge Python environment and configuration directory:

```sh
python /path/to/node/preview_registry.py --config-dir /path/to/node create \
  --session experiments --port 3000 --title 'App review' --ttl 7200
python /path/to/node/preview_registry.py --config-dir /path/to/node list
python /path/to/node/preview_registry.py --config-dir /path/to/node stop PREVIEW_ID
```

Packaged installations also provide `tmux-bridge-preview`. Set
`TMUX_BRIDGE_AGENT_DIR` to the node configuration directory, or pass
`--config-dir`. Commands return JSON including the URL. CLI changes reach the
gateway on the next node announcement (within ten seconds while connected).
Repeated creation of a live session/port pair returns its existing URL without
extending expiry. Create again after stopping to get a new, never-reused origin.
TTL can be 60 seconds to 24 hours. Each node may register 16 previews.

For MCP, install the project's optional `mcp` dependency in the node environment
and configure a **separate, local stdio server**:

```json
{
  "command": "/path/to/node/.venv/bin/python",
  "args": ["/path/to/node/preview_mcp.py", "--config-dir", "/path/to/node",
           "--session", "experiments"]
}
```

It exposes `preview_create(port, title, ttl_seconds)`, `preview_list()`, and
`preview_stop(preview_id)`, scoped to that session. The existing activity MCP
remains read-only. Neither tool needs Cloudflare credentials. Anyone with shell
access as the node account can manage that account's preview registry; MCP
scope is not an operating-system sandbox.

## Deployment and authentication

Previews are disabled until explicitly configured. On the gateway, create a
private `previews.json` in `TMUX_BRIDGE_CONFIG_DIR`:

```json
{
  "enabled": true,
  "host_suffix": "-preview.example.net",
  "gateway_hosts": ["remote.example.net"]
}
```

Place the same file in each participating node's `TMUX_BRIDGE_AGENT_DIR`.
New installer bundles inherit the gateway's preview settings. List all genuine
gateway UI/control hostnames in `gateway_hosts`; loopback access remains allowed.
All other hosts are denied or routed to the registered application, never to
terminal controls. Use a first-level subdomain per preview, for example
`f9b7…-preview.example.net`. This fits a standard `*.example.net` TLS certificate.

The reverse proxy must authenticate the **entire preview hostname and every
path**, including WebSocket upgrades. With Cloudflare Access:

1. Create an Access application for `*-preview.example.net`, copying the
   gateway's allowed-user policy. Do not use a Bypass or Everyone policy.
2. Add DNS routing to the **existing** tunnel. A wildcard DNS record can be used;
   retain existing explicit DNS records.
3. After the existing exact gateway and machine-transport ingress rules, add a
   wildcard hostname route to the existing loopback gateway listener. Require
   Access JWT validation at the connector with the preview application's
   audience and team name. Preserve the original HTTP Host. Keep the final 404
   catch-all rule. Unknown hostnames must not bypass authentication.
4. Verify unauthenticated requests are challenged, unauthorized identities are
   denied, authenticated preview requests work, and other hosted apps retain
   their existing policies and routes.

Browser admission uses the configured gateway/preview Access policy: everyone
allowed by that policy may view registered previews. This version does not add
per-person preview ACLs. Trusted local forwarding has the same trust boundary
as the existing gateway. The Python gateway itself does not verify Access JWTs;
keep its listener on loopback and enforce that check at the connector.

A public Cloudflare hostname remains internet-facing and login-protected.
Cloudflare terminates browser TLS. This is not a VPN-private address or an
end-to-end encryption layer that hides browser content from Cloudflare.
No new app tunnel, public app listener, or Cloudflare-managed node certificate
is required. The node's existing own-certificate transport and signed identity
are reused. Bulk traffic uses separate outbound connections, keeping it off
the terminal control stream.

Before rolling back to a gateway version without preview support, remove the
preview tunnel ingress route first and verify preview requests are denied.
Otherwise an older gateway could serve its control UI on a preview hostname.

## Boundaries and limits

- Only an explicitly registered `127.0.0.1` HTTP port is dialed. There is no URL,
  LAN-host, arbitrary TCP, redirect-following, or HTTPS-upstream proxy feature.
  Common desktop and bridge control ports are excluded.
- HTTP, root-relative assets, streaming responses, and WebSockets are supported.
  Localhost absolute HTTP redirects are rewritten. App-generated absolute URLs
  inside HTML/JavaScript are not rewritten; configure the development server's
  public/HMR URL if it hard-codes localhost.
- Uploads are limited to 2 MiB, WebSocket messages to 1 MiB, and relay concurrency
  to 24 connections per node / 64 gateway-wide. HTTP downloads stream in bounded
  chunks. Node WebSocket queues are bounded. Browser disconnection cancels work.
- Access cookies and Cloudflare identity headers never reach the application.
  App cookies are restricted to the preview hostname. Cross-origin browser
  fetches, mutations, and sockets are rejected; authenticated top-level links
  can be opened from the private localhost UI or another website.
  Preview hosts cannot reach gateway API/control routes.
- Session identity includes its creation timestamp. Disconnect, expiry, Stop,
  or session removal closes relays. A gateway restart fails closed until nodes
  reannounce live registrations. Node-local Stop/session changes can take up to
  ten seconds to propagate. Expiry is enforced independently on both ends.
- Already downloaded content cannot be recalled. Preview addresses are never
  reused, isolating browser storage/service workers between preview lifetimes.
  A preview is not a sandbox for running untrusted code on the node.

## Validation

`python -m unittest discover -s tests -p test_previews.py` runs real loopback
HTTP/WebSocket relay tests for streaming, redirects, auth stripping, origin and
route isolation, ticket/node binding, revocation, expiry, and body limits.
`node tests/previews.cjs` checks the phone UI and click-to-preview flow.
