# Architecture and trust boundaries

## Processes

`gateway.py` serves the web UI, routes terminal data, tracks viewers and maintains a connection to each registered agent. `server.py` runs as the tmux account on each node. It makes an outbound WebSocket connection, announces sessions, and attaches a PTY to tmux when a browser opens a session.

`protocol.py` multiplexes terminal channels and JSON control messages on that connection. There is no inbound listener on a standard node. Node polling refreshes the session list. A dropped connection retries with backoff; a successful registration resets the delay. Closing a terminal tab detaches its browser viewer without intentionally deleting the tmux session.

The browser uses xterm.js. The gateway buffers recent terminal output for attaching viewers; history capture asks the node for tmux scrollback. This does not recover content that an application never rendered or erased from its own internal UI.

## Authentication layers

1. **Browser admission:** the operator's private network or external authentication proxy. The application has no independent browser user database.
2. **Application node identity:** a gateway-issued certificate and fresh timestamp signature. A ten-minute enrollment code is consumed when credentials are issued.
3. **Optional machine transport:** the node's Cloudflare service token admits the carrier connection. Independently issued TLS certificates authenticate both ends of the inner TLS connection. An allowlisted fingerprint is bound to the node's application identity.

The optional mTLS listener is loopback-only and accepts only `/server-ws`. The gateway and TLS relay run on the same trusted machine. Carrier authentication and TLS authentication are separate layers; neither substitutes for browser authentication.

## Data and state

| Location | Contents | Sensitivity |
|---|---|---|
| Gateway configuration | Application CA key, issued certificate metadata, enrollment codes, layout | Private; CA key controls enrollment |
| Node agent directory | Node private key, certificate, gateway URL | Private; grants node identity |
| Optional transport directory | Transport CA, relay certificate, allowlist, carrier API token, node bundle records | Private; administrative control |
| Browser local storage | Tabs, layout preferences, device identity | Local browser state; not authorization |
| Gateway memory | Attached viewers, recent terminal output, pending RPCs | May contain shell output and secrets |

## Scope

All admitted browser operators share one trust domain. Input ownership is coordination, not authorization. A malicious or compromised gateway can issue commands to nodes. Private transport TLS does not prevent the gateway or the browser's HTTPS proxy from seeing their respective plaintext legs.

There is no built-in audit recorder, multi-tenant isolation, or cluster failover. Existing VPN and SSH access can remain available for administration and recovery; this project does not manage them.
