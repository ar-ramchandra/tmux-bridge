# Native cmux backend (experimental)

The gateway can connect to the terminals already running in the cmux Mac app.
It discovers workspaces and terminal surfaces through cmux's local Unix socket.
It does not launch tmux inside cmux or create a second terminal process for a viewer.

```text
Browser → authenticated gateway → outbound agent on your Mac → local cmux socket
                               → outbound Linux agent       → tmux
```

The gateway owns enrollment, routing, browser connections and input ownership.
cmux owns workspace layout and terminal processes. Each agent identity uses one
backend. To expose both backends on one Mac, enroll two names with separate
`--agent-dir` directories.

## Set up a Mac

Install and open cmux. Its desktop app requires macOS; Linux machines continue
using the tmux backend. Install this bridge into a Python virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

On the gateway, create an enrollment code:

```sh
tmux-bridge add-server mac-cmux
```

In a **terminal inside cmux**, pair and start the agent:

```sh
tmux-bridge --agent-dir ~/.tmux-bridge-cmux pair --gateway https://YOUR-GATEWAY
tmux-bridge --agent-dir ~/.tmux-bridge-cmux agent --backend cmux
```

Running inside cmux satisfies its default process-ancestry socket policy. The
adapter does not relax that policy, expose the Unix socket on the network, or
change cmux preferences. A launchd agent may be denied by cmux's socket policy;
this version's supported setup is a foreground agent inside cmux.

Socket selection uses `--cmux-socket`, `cmux_socket` in the agent's config,
`CMUX_SOCKET_PATH`, then the stable cmux socket locations. It never searches
debug sockets or chooses the most recently used app instance. Pass an explicit
socket for a tagged development app. The agent config can persist
`"backend": "cmux"` and `"cmux_socket": "/absolute/path/to/cmux.sock"` alongside
the existing enrollment settings. Credentials stay in that agent directory.

## Browser controls

Choose the Mac in the gateway. Terminal rows show workspace and terminal titles;
routing uses UUIDs, so renaming a workspace does not change its identity.
The **cmux workspaces** section provides:

- New workspace in a selected Mac window.
- Show a selected workspace on the Mac.
- Split the open terminal left, right, up or down.
- Close a workspace after confirming that all its programs will stop.

The terminal row's close button closes that surface after confirmation. cmux may
refuse to close the last surface; use Close workspace to close that workspace.
Closing a browser tab detaches the viewer and clears only its viewport report.
It never sends a workspace or surface close command. Avoid running the bridge
agent in a workspace you intend to close: ending its shell disconnects the Mac.

The existing gateway input owner controls keyboard input and requested geometry.
cmux chooses the effective shared grid; the browser uses the grid returned by the
app. Disconnecting the gateway or browser does not stop terminal processes.
Quitting cmux is a separate lifecycle event: cmux session restoration is not a
checkpoint of arbitrary running processes.

## Transport and current limits

This is an experimental screen adapter, not a raw PTY stream. It uses
`terminal.replay` full `cmux.render-grid.v1` snapshots at up to five requests per
second per viewed terminal. Only changed snapshots are sent to the browser.
The agent and gateway retain the latest frame instead of accumulating full-screen
repaints for slow viewers. UTF-8 keyboard input uses `terminal.input`; resize and
detach use `terminal.viewport`. Requests have deadlines and bounded replies;
mutations are never automatically retried after an ambiguous timeout.

Text, basic colors and styles, cursor position, application cursor keys and
bracketed paste are supported. Mouse control, inline graphics, browser surfaces,
native scroll gestures, and pixel-identical Ghostty rendering are not supported
in this version. Use **History** for a plain-text scrollback snapshot (up to 2,000
lines). A browser disconnect loses no processes, but intermediate output between
screen samples is not recorded by the bridge. Rapid animations can be missed.

The implementation was checked against cmux's local protocol at upstream commit
`730e1cbe9d1e5685547ee9de21addee2165d2830`. Older versions may lack these methods.
Attachment fails visibly if the app cannot provide a full render-grid snapshot.
This bridge contains an independently written protocol adapter, with no cmux
implementation files vendored into the project.

## Verification

The automated tests use a disposable Unix socket fixture and the actual agent,
gateway proxy, and browser renderer. They cover Unicode input/output, explicit
UUID routing, resize feedback prevention, bounded snapshots, detach cleanup,
workspace controls and confirmation before workspace closure.

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
npm test
BROWSER_ENGINE=webkit npm test
```

Live testing against a running cmux app remains required before production use.
Use a disposable workspace to verify shell input, a full-screen terminal app,
resize, browser reconnect, create/split/select/close, and graceful agent exit.
These checks have not yet been run against the native app.
