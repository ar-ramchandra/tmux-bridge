# Changelog

## Unreleased

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
