import os
import asyncio
import json
import subprocess
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import account_usage as usage
import server
from protocol import pack_data, MSG_RPC_REQUEST


class LoginResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_launch_does_not_hold_terminal_input_and_is_bounded(self):
        started, release = asyncio.Event(), asyncio.Event()
        agent = server.ServerAgent({'gateway': 'http://test', 'name': 'test'})
        typed = []
        agent.manager.channels[1] = SimpleNamespace(write_input=lambda data: typed.append(data) or True)
        async def slow_login(ws, msg):
            started.set()
            await release.wait()
        class Socket:
            def __init__(self): self.sent = []
            async def send(self, message): self.sent.append(json.loads(message))
            async def __aiter__(self):
                yield json.dumps({'type': MSG_RPC_REQUEST, 'id': 'one', 'path': '/api/account-login', 'method': 'POST'})
                await started.wait()
                yield json.dumps({'type': MSG_RPC_REQUEST, 'id': 'two', 'path': '/api/account-login', 'method': 'POST'})
                yield pack_data(1, b'input while login opens')
        ws = Socket()
        with patch.object(agent, '_handle_rpc', side_effect=slow_login):
            try:
                await asyncio.wait_for(agent._message_loop(ws), 1)
                self.assertEqual(typed, [b'input while login opens'])
                self.assertEqual(len(ws.sent), 1)
                self.assertEqual(ws.sent[0]['status'], 409)
            finally:
                release.set()
                if agent._login_rpc_task:
                    await agent._login_rpc_task


class LoginTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
    def test_real_login_terminal_lifecycle_on_private_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root/'claude'
            cli.write_text('#!/bin/sh\nprintf "TEST LOGIN READY\\n"\nread code\ntest "$code" = "test-only-code"\n')
            cli.chmod(0o700)
            socket = str(root/'sock')
            run = subprocess.run
            def isolated(args, **kwargs):
                return run([args[0], '-S', socket, *args[1:]], **kwargs)
            def tmux(*args):
                return run(['tmux', '-S', socket, *args], capture_output=True, text=True, timeout=5)
            target = 'bridge-signin-claude'
            def wait_for(text):
                for _ in range(50):
                    if text in tmux('capture-pane', '-pt', target).stdout:
                        return
                    time.sleep(.1)
                self.fail('Login terminal did not reach expected state')
            try:
                with patch.dict(os.environ, {'TMUX_BRIDGE_CLAUDE_BIN': str(cli)}), patch.object(usage.subprocess, 'run', side_effect=isolated):
                    self.assertTrue(usage.start_login('claude')['body']['created'])
                    self.assertFalse(usage.start_login('claude')['body']['created'])
                wait_for('TEST LOGIN READY')
                self.assertEqual(tmux('send-keys', '-t', target, 'test-only-code', 'Enter').returncode, 0)
                wait_for('Sign-in completed.')
                tmux('send-keys', '-t', target, 'Enter')
                for _ in range(50):
                    if tmux('has-session', '-t', target).returncode:
                        break
                    time.sleep(.1)
                self.assertNotEqual(tmux('has-session', '-t', target).returncode, 0)
            finally:
                tmux('kill-server')

    def test_unknown_or_missing_cli_never_starts_a_process(self):
        with patch.object(usage, 'find_cli', return_value=None), patch.object(usage.subprocess, 'run') as run:
            for value in ['sh', 'claude; touch /tmp/oops', None, []]:
                self.assertEqual(usage.start_login(value)['status'], 400)
            self.assertEqual(usage.start_login('claude')['status'], 409)
            run.assert_not_called()

    def test_dedicated_session_and_existing_login_reuse(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=1 if args[1] == 'has-session' else 0, stdout='')
        with patch.object(usage, 'find_cli', return_value='/bin/claude'), patch.object(usage.subprocess, 'run', side_effect=run):
            result = usage.start_login('claude')
        self.assertTrue(result['body']['created'])
        create = next(c for c in calls if c[1] == 'new-session')
        self.assertIn('bridge-signin-claude', create)
        self.assertIn('account_usage.py login claude', create[-1])
        self.assertFalse(any('send-keys' in c for c in calls))
        with patch.object(usage, 'find_cli', return_value='/bin/claude'), patch.object(usage.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='claude\n')) as run:
            self.assertFalse(usage.start_login('claude')['body']['created'])
            self.assertEqual(run.call_count, 2)
        with patch.object(usage, 'find_cli', return_value='/bin/claude'), patch.object(usage.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='')):
            self.assertEqual(usage.start_login('claude')['status'], 409)

    def test_tmux_timeout_returns_retryable_error(self):
        with patch.object(usage, 'find_cli', return_value='/bin/codex'), patch.object(usage.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tmux', 5)):
            self.assertEqual(usage.start_login('codex')['status'], 503)

    def test_login_uses_provider_cli_and_remote_browser_flow(self):
        for provider, arguments in [('claude', ['auth', 'login']), ('codex', ['login', '--device-auth'])]:
            with patch.object(usage, 'find_cli', return_value='/some path/' + provider), patch.object(usage.subprocess, 'call', return_value=0) as call, patch('builtins.input'), patch('builtins.print'):
                usage.run_login(provider)
            self.assertEqual(call.call_args.args[0], ['/some path/' + provider, *arguments])
            self.assertEqual(call.call_args.kwargs['env']['BROWSER'], 'true')
            self.assertFalse(call.call_args.kwargs.get('shell', False))


if __name__ == '__main__':
    unittest.main()
