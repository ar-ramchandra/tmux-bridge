# Contributing

Small, focused fixes and reproducible bug reports are welcome. This is an early alpha with a deliberately small trust model: every browser operator is trusted with shell access.

## Development setup

Install Python 3.10+ and tmux, then run `./setup.sh`. For backend tests:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests create disposable credentials and isolated tmux servers. Do not run the `live-*` scripts against someone else's deployment: those scripts intentionally create temporary sessions and exercise connection recovery.

For browser fixtures, install Node.js 20+ and the pinned development dependency:

```sh
npm ci
npx playwright install chromium webkit
npm test
BROWSER_ENGINE=webkit npm test
TOUCH_MODE=0 node tests/direct-display.cjs
```

On Linux, Playwright may require `npx playwright install --with-deps chromium webkit`. Browser fixtures load pinned xterm assets from the CDN, so these tests require Internet access. `PLAYWRIGHT_PATH` can point to an existing Playwright installation.

## Making changes

- Describe the user-visible problem and include the smallest reproduction.
- Add regression coverage for changed authentication, transport, input, or session-lifecycle behavior.
- Preserve existing tmux sessions when restarting or reconnecting agents.
- Keep private network and recovery tools under operator control.
- Do not commit credentials, runtime configuration, terminal recordings or private installer bundles.
- Update the relevant guide when changing an operator command or configuration format.

All contributions are made under the repository's MIT license. Report suspected vulnerabilities privately as described in [SECURITY.md](SECURITY.md), rather than attaching secrets to an issue.

## Live checks

`BRIDGE_URL` defaults to loopback port 4601 and `BRIDGE_SERVER` defaults to `test-node`. Use a dedicated test node. Read each script before running it. For example:

```sh
BRIDGE_URL=http://127.0.0.1:4601 BRIDGE_SERVER=test-node node tests/live-selection.cjs
```

These checks do not constitute a penetration test or certification of a deployment's external access policy.
