# Account usage

The system-usage panel includes **AI accounts** for the selected machine. It shows
a masked account label, plan when available, remaining allowance, reset times,
and the age of the observation. If the same identified account is reported by
several machines, its newest observation is shown once with the machines listed.
Percentages are never added across machines.

## Data sources

- Codex: read-only `account/read` and `account/rateLimits/read` requests through a
  dedicated `codex app-server --stdio` process. No conversation is started and no
  shared daemon or running terminal is restarted. Window durations come from the
  provider; the primary window is not assumed to mean five hours.
- Claude: the documented `rate_limits` fields passed to a status-line command,
  plus account metadata from `claude auth status --json`. Context remaining and
  token costs are not treated as subscription allowance. Status-line data appears
  after Claude receives an API response; unsupported billing methods may not
  supply a quota.

The node checks installed default CLI accounts every five minutes in a dedicated
worker, separate from terminal input and CPU/memory sampling. The agent sends
cached metadata to the gateway approximately every ten seconds. Claude status-line
reports are tied to their observed account when an identity is available; unknown
identities are kept separate. Account metadata is cached for up to five minutes.
Custom CLI homes or accounts that are not used by these processes are not scanned.
This is observed account usage, not a complete inventory of credentials.

An observation older than fifteen minutes is marked stale. A passed reset time
is labelled as awaiting a fresh reading, never assumed to mean 100% available.
Signed-out, unsupported, and unavailable states are explicit. An account quota
can be shared with other provider products; it is not usage attributable solely
to the selected machine.

## Setup

Codex needs its CLI installed and signed in under the node agent's user account.
The collector searches PATH, `~/.local/bin`, Homebrew, and installed nvm versions.
`TMUX_BRIDGE_CODEX_BIN` and `TMUX_BRIDGE_CLAUDE_BIN` can pin the CLI executable.

To enable Claude's quota reports, run with the node's Python interpreter:

```sh
python account_usage.py --agent-dir ~/.tmux-bridge install-claude
```

This backs up Claude's settings and preserves any existing status-line command and
its output. It does not change hook trust. New or resumed Claude sessions pick up
the setting; an existing session may need a settings reload. Usage stays unknown
until the provider actually reports it. The helper emits no additional terminal
status text when no previous renderer exists.

The integration stores quota metadata in the private agent directory. It does
not read credential files. Only masked account labels, hashed account identifiers,
plan names, quotas, and timestamps are forwarded; no authentication tokens,
conversation text, or full email addresses are forwarded. Existing custom status
renderers retain their existing execution privileges and receive the same stdin
that Claude normally provides.

## API and MCP

`GET /api/account-usage` returns cached observations grouped by node, including
connection and freshness indicators. `GET /api/NODE/metrics` also includes the
node's cached `account_usage` field. Both use the gateway's existing admission
boundary; this remains one shared trusted operator workspace.

The read-only MCP tool `get_account_usage` exposes cached quotas only for
`NODE/*` or `*/*` scopes. A grant to a single session does not expose account-wide
information. MCP reports observation and reception times; it does not infer a
live connection from stored data. Consumers must check `stale` and `reset_passed`
and must not add shared-account percentages.

Provider references: [Codex account limits](https://learn.chatgpt.com/docs/app-server)
and [Claude status-line fields](https://code.claude.com/docs/en/statusline).
