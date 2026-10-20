"""Recovery must survive a tmux server death without restarting the bridge."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import gateway
import server


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmux_death_notifies_and_restored_session_reattaches(self):
        with tempfile.TemporaryDirectory(prefix='bridge-recovery-') as temp:
            sock = str(Path(temp) / 'tmux.sock')
            real_run, real_popen = subprocess.run, subprocess.Popen
            def isolated(args):
                return args[:1] + ['-S', sock, '-f', '/dev/null'] + args[1:] if args[0] == 'tmux' else args
            def run(args, **kwargs):
                # Popen is already patched below; avoid injecting the socket twice.
                return real_run(args, **kwargs)
            def popen(args, **kwargs):
                return real_popen(isolated(args), **kwargs)
            ended = asyncio.Queue()
            async def exited(channel):
                await ended.put(channel)
            async def received(data):
                pass
            manager = server.SessionManager(exited)
            with patch.object(subprocess, 'Popen', popen):
                try:
                    run(['tmux', 'new-session', '-d', '-s', 'recover', 'sleep 60'], check=True)
                    session = await manager.open_channel(11, 'recover', received)
                    self.assertIsNotNone(session)
                    run(['tmux', 'kill-server'], check=True)
                    self.assertEqual(await asyncio.wait_for(ended.get(), 5), 11)
                    self.assertNotIn(11, manager.channels)
                    run(['tmux', 'new-session', '-d', '-s', 'recover', 'sleep 60'], check=True)
                    restored = await manager.open_channel(12, 'recover', received)
                    self.assertTrue(restored.alive)
                    await manager.close_channel(12)
                    await asyncio.sleep(.15)
                    self.assertTrue(ended.empty(), 'Intentional close must not trigger recovery')
                finally:
                    await manager.stop_all()
                    run(['tmux', 'kill-server'], capture_output=True)

    async def test_failed_attach_is_reported_over_protocol(self):
        agent = server.ServerAgent({'gateway': 'http://localhost', 'name': 'test'})
        class Socket:
            def __init__(self): self.messages = []
            async def send(self, message): self.messages.append(json.loads(message))
        socket = Socket()
        with patch.object(agent.manager, 'open_channel', return_value=None):
            await agent._handle_connect(socket, {'channel': 7, 'session': 'missing'})
        self.assertEqual(socket.messages[0]['type'], 'channel_closed')
        self.assertEqual(socket.messages[0]['channel'], 7)

    async def test_gateway_closes_dead_terminal_socket_and_discards_replay(self):
        class Socket:
            def __init__(self): self.messages = []; self.code = None
            async def send_text(self, data): self.messages.append(json.loads(data))
            async def close(self, code, reason): self.code = code
        viewer = Socket()
        proxy = gateway.ProxySession(None, 'test')
        proxy.viewers[viewer] = None
        proxy.channel = 8; proxy._channel_open = True; proxy.buffer.extend(b'old terminal')
        await proxy.terminal_ended('Terminal ended')
        self.assertEqual(viewer.code, 1012)
        self.assertFalse(proxy._channel_open)
        self.assertIsNone(proxy.channel)
        self.assertFalse(proxy.buffer)
        self.assertEqual(viewer.messages[0]['msg'], 'Terminal ended')


if __name__ == '__main__':
    unittest.main()
