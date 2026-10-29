# Changelog

## Unreleased

- Fix push signing for phone notifications, exercise real signing/encryption in
  tests, and show useful delivery errors without asking users to register again.

- Added provider prompt status in the session list and active terminal header.
- Added per-device browser/phone push settings, event and node filters, sound,
  and a test notification action.
- Added read-only MCP tools for scoped status queries and durable event polling.
- Added additive Codex/Claude hooks and optional OpenCode plugin generation.
- Kept notification storage and delivery separate from terminal input; queued
  events retry across reconnects, including on legacy Python 3.8 node agents.

- Scroll full-screen applications with empty tmux history using application paging, coalesce touch movement into pages, and retain remote touch scrolling when copy mode changes screen buffers. Validate visible content changes and draft preservation.
- Reconnect browser terminals when an agent replaces a stale connection, preventing a connected-looking terminal from silently discarding input on a dead channel.
- Restore terminal keyboard focus when selecting or switching desktop sessions. Preserve keyboard tab navigation and mobile keyboard behavior.

## 0.1.0a1

First public alpha, extracted from a working private deployment into a clean repository.

- Central browser gateway with outbound Linux/macOS agents and persistent tmux sessions.
- One-time pairing, signed agent identities and optional private-CA mTLS transport.
- Private node installer bundles for a preconfigured Cloudflare deployment.
- Terminal tabs, input ownership, reconnect behavior, history and direct selection/link controls.
- Portable private state directories and foreground CLI commands.
- Loopback-only defaults, explicit tunnel configuration and normal CLI permission prompts.
- Installation, architecture, node lifecycle, security and contributor documentation.

Known limits: browser authentication is delegated to the deployment; all admitted operators share shell-level trust. Certificate renewal is manual. Physical mobile-device validation and full fresh-account Cloudflare bootstrap remain incomplete. See [validation](docs/validation.md).
