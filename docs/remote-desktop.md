# Remote desktop

Select a connected machine in the sidebar, then **Desktop**. The viewer opens inside the gateway, including on a phone or tablet. Enter that machine's desktop credentials when prompted. Tap the screen to click, use **Keyboard** for the phone keyboard, and use **Fit** to switch between a scaled screen and its original size. The accessory row provides Escape, Tab, arrows, Backspace, and Enter.

This controls the desktop supplied by the machine's VNC service. It can be used to complete a browser login or work in a graphical application. It does not create a desktop on a headless machine. Closing the viewer disconnects desktop control and leaves applications and tmux sessions running. One gateway viewer can control each machine at a time; close the existing viewer before opening another.

## Enable a machine

Update both the gateway and node agent. Configure a local VNC server, such as macOS Screen Sharing or a Linux VNC service, with authentication. The bridge never enables an operating-system service, changes its credentials, or opens a firewall automatically. Restrict the desktop service to loopback or otherwise firewall it appropriately; do not publish VNC or RDP ports on the internet.

Create `desktop.json` in the agent's private configuration directory (`~/.tmux-bridge` by default, or `TMUX_BRIDGE_AGENT_DIR` for a managed installation):

```json
{"enabled": true, "port": 5900}
```

Set its permissions to `0600`. Configuration is read each time a connection is requested, so no agent restart is needed after changing it. New node bundles include the desktop code but leave access disabled until this file is created. Set `enabled` to `false` to prevent new connections; close an existing viewer to end its active connection.

The agent always connects to `127.0.0.1`; the browser cannot choose a target host or port. Ports must be integers between 1024 and 65535. A status check verifies the VNC protocol before presenting the viewer. This is a VNC viewer, not an RDP client; a machine offering only RDP needs a separate supported desktop service.

## Transport and credentials

The desktop uses the gateway's existing browser admission policy and signed node identity. In an installation with the private-CA mTLS carrier, the separate desktop stream uses the same mTLS configuration as the terminal agent. It requires a short-lived, single-use connection ticket bound to that node. Tickets are kept in memory and never placed in browser URLs. The stream has bounded messages and backpressure, and does not share the terminal data connection.

Desktop credentials are sent to the VNC server and cleared from the form after submission. The bridge does not persist them or log desktop frames. Clipboard synchronization is not enabled. The bundled noVNC viewer is served locally; it does not load a third-party runtime from a CDN. Its source version, integrity hash, and licenses are in `tmux_bridge_assets/novnc`.

The gateway remains a trusted intermediary that can read desktop traffic. HTTPS proxies remain in the browser-side trust boundary, just as they do for terminal traffic; this is not browser-to-node end-to-end encryption. All admitted gateway operators have desktop access to enabled nodes. See [architecture](architecture.md) and [security](../SECURITY.md).

## Validation and limits

Automated Chromium and WebKit tests exercise VNC authentication, framebuffer rendering, touch pointer events, keyboard input, and disconnects. Relay tests check signed node authentication, origin enforcement, ticket expiry and identity binding, binary transport, and exclusive control. Physical mobile keyboards, operating-system permissions, and individual VNC security configurations can still vary. A service that requires an unsupported VNC security extension will not connect.
