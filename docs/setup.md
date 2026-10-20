# Installation and remote access

## Requirements

Linux or macOS, Python 3.10+ (3.12+ recommended), tmux, and a modern browser. Install tmux with your OS package manager. Linux needs Python's venv support (often `python3-venv`). Windows agents are unsupported because the agent uses Unix PTYs.

Clone the repository and run `./setup.sh`. Activate `. .venv/bin/activate` in each terminal using the CLI. Alternatively install a release wheel with pip in your own virtual environment. `tmux-bridge doctor` checks imports, tmux and identities.

## Gateway

```sh
tmux-bridge init
tmux-bridge gateway
```

The listener is `127.0.0.1:4601`; use `gateway --port 4602` if necessary. The CA and layout live in `~/.config/tmux-bridge/`. `init` refuses a partial identity and preserves an existing complete one.

Global `--config-dir /private/path/gateway` selects another gateway identity. Set the same path for every gateway administration command. `TMUX_BRIDGE_CONFIG_DIR` is its environment equivalent. Do not share a configuration directory between independent gateways.

## Agent enrollment

On the gateway:

```sh
tmux-bridge add-server workstation
```

On the node, after installing the project:

```sh
tmux-bridge pair --gateway https://gateway.example.com
tmux-bridge agent
```

Enter the one-time code at the prompt, not as a shell argument. It expires in ten minutes. Run the agent as the account owning your tmux sessions. Existing agent files cause a refusal; use `--agent-dir /private/path/agent` for an independent identity. The default is `~/.tmux-bridge/`.

A browser login page is not an agent endpoint. If an authenticating proxy intercepts pairing or agent WebSockets, use an SSH forward/private route for the agent, or the separate machine transport in [Cloudflare setup](cloudflare.md). Do not bypass browser authentication globally to make pairing work.

## First remote connection using SSH

Keep the gateway loopback-only. On a remote node with authorized SSH access to the gateway host:

```sh
ssh -N -L 127.0.0.1:4602:127.0.0.1:4601 your-user@gateway-host
```

Keep that forward running. In another node terminal:

```sh
tmux-bridge pair --gateway http://127.0.0.1:4602
tmux-bridge agent
```

The agent's loopback connection travels inside your SSH connection. The same forwarding pattern lets a laptop open the gateway UI at `http://127.0.0.1:4602`. Restrict SSH access to trusted operators.

## Private HTTPS

An authenticated private-network HTTPS proxy can forward to the loopback gateway. For example, Tailscale Serve publishes to devices permitted by your tailnet policy; it is different from public Funnel. Configure access narrowly and consult the [Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve).

```sh
tailscale serve --bg http://127.0.0.1:4601
```

Use the HTTPS URL it prints in your browser and when pairing a permitted node. Review who can reach it: every authorized visitor has shell-level privileges in this application. This project does not configure or replace an existing VPN.

## Keep processes running

The general CLI runs in the foreground. Use your OS service manager for unattended operation. Keep virtual environment and source paths stable for editable installs. For agent services, ensure the service manager does not kill the tmux server on restart: tmux may inherit the agent's service cgroup. The private bundle installer supplies reviewed systemd/launchd units for its transport mode; see [add nodes](add-nodes.md).

Stopping a gateway or agent should detach browser clients, not intentionally delete sessions. tmux sessions do not survive a machine reboot unless you separately arrange session restoration. Treat commands that delete sessions as destructive.

## Revoke a generic agent

Stop that agent first. From the gateway's virtual environment, with the correct `TMUX_BRIDGE_CONFIG_DIR` if customized:

```sh
python -c 'from auth import revoke_server; revoke_server("workstation")'
```

Application revocation is checked on subsequent authentication; it does not evict an existing socket by itself. Restarting the gateway closes all current sockets if immediate eviction is necessary. For transport bundles, use their dedicated revocation command instead.

## Troubleshooting

- No machines: confirm the agent is running and using the correct gateway URL and CA.
- Pairing returns HTML: an authentication proxy probably intercepted the machine request.
- No sessions: confirm the account and tmux socket; create one with `tmux new-session -d -s work`.
- Disconnected after an update: reload the browser; inspect agent/gateway logs without sharing credentials.
- Port in use: choose another port; do not stop an unrelated process.
- Wrong scroll behavior: see [usage](usage.md) and include browser, OS, tmux version, and terminal application in a bug report.
