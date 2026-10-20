"""Fresh-install and pairing regressions using a disposable gateway identity."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
import websockets

from bridge_cli import gateway_url

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-setup-')
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'gateway'
        self.agent = Path(self.temp.name) / 'agent'
        self.command = [sys.executable, str(ROOT / 'bridge_cli.py'), '--config-dir', str(self.config), '--agent-dir', str(self.agent)]

    def cli(self, *args, input=None):
        return subprocess.run(self.command + list(args), input=input, text=True, capture_output=True, timeout=20)

    def test_init_preserves_identity_and_rejects_partial_identity(self):
        self.assertEqual(self.cli('init').returncode, 0)
        key = (self.config / 'ca.key').read_bytes()
        self.assertEqual(self.cli('init').returncode, 0)
        self.assertEqual((self.config / 'ca.key').read_bytes(), key)
        (self.config / 'ca.crt').unlink()
        self.assertNotEqual(self.cli('init').returncode, 0)
        self.assertEqual((self.config / 'ca.key').read_bytes(), key)

    def test_pairing_requires_encrypted_remote_origin(self):
        for url in ('http://100.64.0.1', 'https://user:pass@example.com', 'https://example.com/path', 'file:///tmp/x'):
            with self.assertRaises(ValueError):
                gateway_url(url)
        self.assertEqual(gateway_url('https://example.com/'), 'https://example.com')
        self.assertEqual(gateway_url('http://127.0.0.1:4601'), 'http://127.0.0.1:4601')

    def test_server_names_cannot_escape_config_or_inject_code(self):
        self.assertEqual(self.cli('init').returncode, 0)
        for name in ('../bad', 'bad/name', 'bad"name', '<script>'):
            self.assertNotEqual(self.cli('add-server', name).returncode, 0)
        self.assertEqual(list((self.config / 'tokens').glob('*.json')), [])

    def test_fresh_gateway_pairing_and_authenticated_agent_connection(self):
        self.assertEqual(self.cli('init').returncode, 0)
        added = self.cli('add-server', 'test-agent')
        self.assertEqual(added.returncode, 0, added.stderr)
        token = added.stdout.strip().splitlines()[-1]
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(self.command + ['gateway', '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        self.addCleanup(stop)
        url = 'http://127.0.0.1:' + str(port)
        with httpx.Client(timeout=2) as client:
            for _ in range(100):
                try:
                    if client.get(url + '/health').status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                self.fail('Disposable gateway did not start')
            self.assertEqual(client.get(url + '/enroll').status_code, 404)
            self.assertEqual(client.get(url + '/api/download/server.py').status_code, 404)
            self.assertEqual(client.post(url + '/api/enroll', json={'token': '../invalid'}).status_code, 403)
            self.assertEqual(client.post(url + '/api/enroll', content=b'x' * 1025).status_code, 413)
            paired = self.cli('pair', '--gateway', url, input=token+'\n')
            self.assertEqual(paired.returncode, 0, paired.stderr)
            self.assertEqual(client.post(url + '/api/enroll', json={'token': token}).status_code, 403)
            self.assertEqual((self.agent / 'server.key').stat().st_mode & 0o777, 0o600)
            original = (self.agent / 'server.key').read_bytes()
            self.assertNotEqual(self.cli('pair', '--gateway', url, input='unused\n').returncode, 0)
            self.assertEqual((self.agent / 'server.key').read_bytes(), original)
            self.assertEqual(self.cli('doctor', '--gateway', url).returncode, 0)
            self.assertNotEqual(self.cli('add-server', 'test-agent').returncode, 0)
            async def connect():
                from auth import create_auth_proof
                proof = create_auth_proof(original, (self.agent / 'server.crt').read_bytes())
                async with websockets.connect(url.replace('http:', 'ws:') + '/server-ws') as ws:
                    await ws.send(json.dumps(proof))
                    reply = json.loads(await ws.recv())
                    self.assertEqual(reply.get('type'), 'auth_ok')
                    self.assertEqual(reply.get('name'), 'test-agent')
            asyncio.run(connect())


if __name__ == '__main__':
    unittest.main()
