"""New Claude sessions load the login environment and retain startup errors."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

import gateway


class SessionLaunchTests(unittest.TestCase):
    def test_existing_clients_get_login_launcher(self):
        rpc = AsyncMock(return_value=JSONResponse({'ok': True, 'created': True}))
        with patch.object(gateway, '_rpc_to_server', rpc):
            response = TestClient(gateway.app).post('/api/hpc/sessions/example', json={
                'command': 'claude',
            })
        self.assertEqual(response.status_code, 200)
        rpc.assert_awaited_once_with('hpc', 'POST', '/api/sessions/example', {
            'command': gateway.CLAUDE_SESSION_COMMAND,
        })

    def test_custom_commands_and_plain_shell_are_preserved(self):
        for body in ({}, {'command': 'python3 -u app.py'}):
            rpc = AsyncMock(return_value=JSONResponse({'ok': True}))
            with patch.object(gateway, '_rpc_to_server', rpc):
                TestClient(gateway.app).post('/api/hpc/sessions/example', json=body)
            self.assertEqual(rpc.await_args.args[-1], body)

    def test_login_environment_is_loaded_and_failure_leaves_a_shell(self):
        with tempfile.TemporaryDirectory(prefix='bridge launch ') as directory:
            root = Path(directory)
            shell = root / 'login-shell'
            # A deterministic user login profile supplies the CLI's PATH.
            shell.write_text('#!/bin/sh\n'
                             'if [ "$1" = "-lc" ]; then\n'
                             '  export PATH="$BRIDGE_TEST_BIN:/usr/bin:/bin"\n'
                             '  exec /bin/sh -c "$2"\n'
                             'fi\n'
                             '[ "$1" = "-l" ] || exit 99\n'
                             'echo FALLBACK_SHELL_READY\n')
            cli = root / 'claude'
            cli.write_text('#!/bin/sh\necho CLI_STARTED "$@"\nexit 17\n')
            shell.chmod(0o700)
            cli.chmod(0o700)
            result = subprocess.run(['/bin/sh', '-c', gateway.CLAUDE_SESSION_COMMAND],
                                    env={**os.environ, 'PATH': '/usr/bin:/bin',
                                         'SHELL': str(shell), 'BRIDGE_TEST_BIN': directory},
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('CLI_STARTED\n', result.stdout)
            self.assertNotIn('--dangerously-skip-permissions', result.stdout)
            self.assertIn('Claude exited (status 17)', result.stdout)
            self.assertIn('FALLBACK_SHELL_READY', result.stdout)


if __name__ == '__main__':
    unittest.main()
