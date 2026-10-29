# Prompt status and notifications

The gateway records provider lifecycle metadata independently of terminal input.
You can keep driving every session yourself. No supervising agent is required.

Status labels distinguish Working, Needs input, Response finished, Failed,
Interrupted, Idle, and Session ended. **Response finished is not verification that
the requested goal succeeded.** Provider Stop hooks report a stopping point;
another hook may cause the provider to continue. SubagentStop is not treated as
parent completion. Missing events and disconnected nodes do not imply completion.

## Install on the gateway

For a source checkout, install `requirements-integrations.txt` in the gateway's
virtual environment. For a package install, use `pip install 'tmux-bridge[notifications,mcp]'`.
The node-side event collector needs only Python's standard library.

Gateway activity is stored under `~/.config/tmux-bridge/activity/` by default.
`TMUX_BRIDGE_CONFIG_DIR` or `TMUX_BRIDGE_ACTIVITY_DIR` can override the location.
The database and generated VAPID signing key are private files. Back up the VAPID
key with the deployment: replacing it requires browsers to subscribe again.

## Connect provider hooks on each node

Run these as the account that owns the terminal sessions, using the actual node
directory (private installer nodes use `~/.tmux-bridge-nodes/NAME`):

```sh
python3 integrations.py --agent-dir ~/.tmux-bridge install claude
python3 integrations.py --agent-dir ~/.tmux-bridge install codex
```

Installation preserves existing settings and hooks, makes a backup, and is
idempotent. Use a stable Python executable and script location. Hooks only store
short metadata locally and return an empty JSON object. They never send input,
approve tool calls, or modify prompts. Full prompts, answers, and transcripts are
not retained by this integration.

**Codex requires reviewing and trusting the new definitions using `/hooks`.**
The installer does not forge trust or bypass that requirement. Existing provider
sessions may need to reload settings or be resumed in a new process. Do not kill
an active task to enable notifications. Check `/hooks` in Claude as well if the
current process has not picked up the new configuration.

The supported interfaces are documented in [Codex hooks](https://learn.chatgpt.com/docs/hooks)
and [Claude hooks](https://code.claude.com/docs/en/hooks). Keep adapters updated
when provider schemas change.

Hooks associate their provider thread with `TMUX_PANE`. For a process launched
outside tmux, supply an explicit binding rather than guessing by directory:

```sh
python3 integrations.py --agent-dir ~/.tmux-bridge bind codex THREAD_ID SESSION_NAME
```

For OpenCode, generate the plugin matching the installed major version and place
it in that version's plugin directory:

```sh
python3 integrations.py --agent-dir ~/.tmux-bridge opencode-plugin --version 1
# or --version 2
```

V1 session.idle is reported as Idle, not verified completion. V2 execution lifecycle
events distinguish completion, failure, and interruption. Generated plugins are
optional; do not install both versions.

## Browser and phone settings

Open **Notification settings** beside the user controls. Settings apply to this
browser or Home Screen app independently of other devices:

- In-app alerts and optional sound while open.
- Response finished, needs input, and error filters.
- Node filters; no nodes selected means all accessible nodes.
- Enable/disable push and send a test notification.

Push needs HTTPS (or a browser-recognized localhost secure context), browser
permission, and the optional push dependency. On iPhone/iPad, add the gateway to
the Home Screen and enable notifications from that app. Background push delivery
depends on the browser/OS push service and device settings. In-app status works
without push permission. Notification previews contain provider/status only.

Gateway browser admission still controls access. This release has one shared
operator workspace; per-device preferences are not separate user authorization.
Do not treat it as a multi-tenant notification service.

## Connect an agent through MCP

Run the read-only stdio server on the gateway host. No additional public HTTP
listener is opened. A client can launch it locally or through an existing SSH
connection. Configure access in the launch command, outside model control:

```json
{
  "mcpServers": {
    "gateway_activity": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/activity_mcp.py", "--allow", "hpc/project-session"]
    }
  }
}
```

Repeat `--allow NODE/SESSION` for more scopes. `NODE/*` grants all sessions on one
node; `*/*` explicitly grants all activity. Node/session names match exactly.
Restrict SSH/OS access separately: tool scopes do not sandbox an account that
already has shell access to the database.

Tools: `list_sessions`, `get_prompt_status`, `get_events(after_cursor)`, and
`wait_for_events(after_cursor, timeout_seconds)`. The wait is capped at 25 seconds.
Persist `next_cursor`, including on empty filtered pages, and repeat the wait.
This works without depending on experimental MCP task subscriptions. Receiving
an event does not automatically wake an idle model; the client host must schedule
its next turn. There are no session input or task submission tools.

The node outbox retries until the gateway acknowledges durable storage. Duplicate
event IDs are collapsed per authenticated node. History is capped at approximately
100,000 events, with older history aged out after 30 days while retaining the most
recent 2,000 events. `history_gap` tells clients to refresh their status snapshot.
An outbox caps at 10,000 unacknowledged events and reports overflow locally.

## Verification

```sh
python -m unittest discover -s tests -p 'test_*.py'
npm test
BROWSER_ENGINE=webkit node tests/activity.cjs
```

Tests cover MCP stdio negotiation, scopes, reconnect replay, cursors, privacy,
notification filters, mobile settings, and terminal focus preservation. Actual
phone delivery must also be tested on an opted-in device using Test notification.
