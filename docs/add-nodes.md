# Private node installers

Use this flow only after configuring the [Cloudflare/mTLS transport](cloudflare.md). For a simpler fresh installation, use the standard [pairing flow](setup.md).

## Generate on the gateway

```sh
./add-node.sh workstation
# Equivalent, after activating the virtual environment:
tmux-bridge add-node workstation
```

Names must start with a lowercase letter and contain lowercase letters, digits or hyphens, up to 48 characters. Names must be unique, including previously retired identities. For custom state locations:

```sh
tmux-bridge --config-dir /private/gateway add-node workstation --transport-dir /private/transport-admin
```

The generator verifies the configured machine Access application, its service-token-only policies and its authenticated loopback tunnel route. It creates a separate service token, policy, application credential and transport certificate. Existing node policies are preserved. It writes:

```text
~/.config/tmux-bridge/transport/nodes/workstation/workstation-installer.zip
```

`--output /private/path/node.zip` selects another file; `--port 14604` changes the node-local carrier listener if the default 14603 is occupied. Provisioning requires a functioning Cloudflare management API and sufficient scoped API permissions.

## Install on the node

Transfer the ZIP privately. It contains credentials and is not password-encrypted.

```sh
unzip workstation-installer.zip
cd workstation-installer
sh install.sh --check
sh install.sh
```

Run as the normal account that owns the tmux sessions, not `sudo sh install.sh`. Linux service installation may request sudo. Python 3.10+, TLS 1.3 support and Internet access are required. On Debian/Ubuntu, install the matching `python3-venv` package. The installer can install missing tmux through apt, dnf or Homebrew; otherwise install it yourself.

The installer checks the manifest, downloads pinned cloudflared with a verified digest, creates a private Python environment, probes mTLS and application authentication, and then installs a service. It refuses an existing agent or conflicting listener rather than replacing it.

Linux uses `tmux-bridge-node-workstation.service`; macOS uses the login LaunchAgent `com.user.tmux-bridge-node-workstation`. If no service manager is available, use `sh install.sh --foreground` and leave it running. State is stored under `~/.tmux-bridge-nodes/workstation/`.

The gateway shows the node once it connects. Existing tmux sessions appear automatically. Stop/restart only this node service when administering it. Neither installer nor runtime changes mesh networking.

## Expiry, replacement and revocation

Client transport certificates expire 90 days after bundle generation. `manifest.json` records the exact expiry. Application certificates and Access service tokens last one year. The gateway's transport certificate expires separately.

Automatic renewal and in-place upgrades are not implemented. Before expiry, prepare a replacement identity with a new name, stop the old node agent, install and verify the replacement, then revoke the old identity. Existing tmux sessions remain owned by the same OS account. Do not accidentally leave both identities connected to the same sessions.

After stopping an old node, revoke from the gateway:

```sh
tmux-bridge revoke-node-bundle workstation
```

This requires the gateway health check at `127.0.0.1:4601` to succeed and the named node to be offline. It removes that node's Access policy/token, transport allowlist entry and application registration. It does not remotely uninstall files. Custom transport directories can be passed as for generation. If Cloudflare cleanup fails, rerun revocation until it succeeds before deleting the administrative record.

Delete spare ZIP/extracted copies after installation. Keep the installed node directory private. Broad API credentials and both CA signing keys never belong in the bundle.

## Validation limits

Tests use isolated CAs, a simulated Cloudflare account and a real agent through a local TLS relay. They cover credential separation, policy preservation, tampered bundles, hostname rejection and preserving tmux sessions on shutdown. They do not validate every external Cloudflare account policy or physical machine. The first installation on each real node must pass its live connection probe.
