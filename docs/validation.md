# Alpha validation record

Release preparation: 2026-10-20. This records checks actually performed, not a security certification.

## Passed locally

- A fresh virtual environment installed the project on macOS with Python 3.14.4.
- 59 Python tests passed: enrollment and replay refusal, browser-origin checks, retry/reconnect, input ownership, history/scrolling, PTY behavior, metrics, isolated tmux recovery, mTLS rejection cases and node-bundle lifecycle checks.
- 49 browser regression checks passed in each of Chromium and WebKit (98 total), using Playwright 1.59.1. Seven additional direct-display checks passed per engine with touch disabled (14 total).
- Built both the wheel and source distribution. Installed the wheel in another clean virtual environment outside the source tree.
- From that installed wheel: initialized a fresh gateway identity, paired a real agent, connected an isolated tmux server, and checked live drag selection, copy, links and history in Chromium and WebKit. Stopping the agent preserved the existing isolated tmux session.
- A disposable live-node selection check passed against the existing deployment. The test waits for tmux's initial attach/resize redraw before dragging; a redraw during initial attachment can clear a selection.
- Release files were allowlisted into a new checkout and inspected for credential/private-deployment patterns. Runtime state, private installer bundles, CA keys, service tokens, private hostnames and old deployment history are excluded.

## Continuous integration

The GitHub Actions workflow runs the Python suite on Linux/macOS with Python 3.10 and 3.12, builds distributions, and runs Chromium/WebKit browser fixtures on Linux. Check the workflow status for the exact commit being installed.

## Not yet established

- Independent security audit, penetration test, or multi-tenant isolation.
- Physical phone/tablet keyboard and clipboard behavior across supported browsers.
- Fresh-account Cloudflare bootstrap: the advanced installer requires an existing configured gateway and policies. API tests use a simulated account; local mTLS tests use real TLS and an actual agent.
- Automatic certificate renewal, in-place bundle upgrades, high availability or reboot restoration of tmux state.
- Long-duration load testing or a latency guarantee. Network carriers and terminal applications affect responsiveness.

The private deployment's operational history does not substitute for these missing checks. See the [security model](../SECURITY.md) and [roadmap](roadmap.md).
