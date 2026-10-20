# Security

This is an early alpha with no independent security audit. It grants remote shell access by design. Use it only with machines and operators you trust.

## Report a vulnerability

Use the repository's **Security → Report a vulnerability** private advisory form. Do not post credentials, shell history, private keys, or exploit details in public issues. Security advisories and fixes will identify affected versions; there is no guaranteed response SLA.

## Browser access is privileged

The gateway has no built-in user login, role system, or per-node authorization. An operator who reaches it can manage sessions, send commands, and open a shell on the gateway. “Viewing only” and “Take control” coordinate typing; they are not security roles.

The CLI and direct source entry point bind to loopback by default. Do not bind an unauthenticated gateway to a LAN or public address. Remote browser access needs a tightly restricted private network or an authenticating reverse proxy. Protect HTTP routes and WebSocket upgrades alike. Enforce authentication at the connector when using Cloudflare Access, and block direct paths to the origin. Cloudflare Tunnel alone is not an authentication policy.

Origin checking rejects cross-origin browser mutations and terminal sockets; it does not authenticate a user. Native HTTP clients can omit Origin. Local processes under the gateway account are trusted. Reverse-proxy headers must only be trusted from the proxy.

## Node authentication and encryption

Agents authenticate with a CA-signed application certificate and a timestamp signature. Pairing codes expire after ten minutes and are single-use. Remote pairing requires HTTPS. Loopback HTTP is intended for local testing or an explicitly secured forward. Do not send codes through URL query parameters; the legacy script-returning enrollment route is removed.

The optional mTLS relay requires TLS 1.3, an allowlisted transport-certificate fingerprint, and a matching application identity. It exposes only `/server-ws`, not the browser UI. The agent verifies the server hostname. The gateway holds both CA signing keys; a compromised gateway can control enrolled nodes. A node has only its own private keys and scoped carrier credential.

Private mTLS protects the agent-to-relay connection through the carrier. The relay and gateway see plaintext terminal data. With Cloudflare HTTPS for browser access, Cloudflare terminates browser TLS and can see that leg. This is not end-to-end encryption from browser to node.

## Credentials and operations

- Keep the gateway configuration directory private and back it up securely. Never commit it.
- Installer ZIPs contain node credentials and are not password-encrypted. Transfer them privately and delete spare copies after installation. Manifest checksums detect damage, not an untrusted installer author.
- Application certificates last one year; bundled client transport certificates last 90 days. The server transport certificate has its own expiry. Renewal is manual; schedule it before expiration.
- Application revocation is checked on connection. Transport allowlist revocation applies to new connections. For immediate eviction, stop the affected agent and its current connection; a relay restart ends all current relay connections.
- The gateway is a privileged single trust domain, not an isolation boundary for untrusted tenants. Session names, output, uploaded files and links should still be treated as untrusted content.
- Terminal content can contain secrets. Diagnostics should omit it. Shared screenshots and bug reports must be redacted.
- Browser terminal libraries are loaded from pinned CDN versions with integrity hashes. Availability still depends on the CDN. See THIRD_PARTY.md.

Only the current alpha is supported. Native Windows agents, automatic credential rotation, high availability, and independent security certification are outside this release.
