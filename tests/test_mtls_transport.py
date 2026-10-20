import asyncio
import json
from pathlib import Path
import ssl
import tempfile
import unittest

from websockets.legacy.client import connect
from websockets.legacy.server import serve

import mtls_proxy
import server
import transport_pki


class MutualTLSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-mtls-test-')
        self.root = Path(self.temp.name)
        transport_pki.init_ca(self.root/'ca')
        transport_pki.issue(self.root/'ca', 'bridge.test', self.root/'gateway', server=True)
        self.fingerprint = transport_pki.issue(self.root/'ca', 'canary', self.root/'client')
        transport_pki.issue(self.root/'ca', 'canary', self.root/'expired', days=-1)
        transport_pki.init_ca(self.root/'untrusted-ca')
        transport_pki.issue(self.root/'untrusted-ca', 'canary', self.root/'untrusted-client')
        self.registry = self.root/'allowed.json'
        self.registry.write_text(json.dumps({self.fingerprint: 'canary'}))
        self.received = []
        async def echo(ws, path):
            async for message in ws:
                self.received.append(message)
                await ws.send(message)
        self.upstream = await serve(echo, '127.0.0.1', 0)
        port = self.upstream.sockets[0].getsockname()[1]
        self.proxy = mtls_proxy.AgentTLSProxy({
            'port': 0, 'upstream': f'ws://127.0.0.1:{port}/server-ws',
            'certificate': str(self.root/'gateway/tls.crt'),
            'private_key': str(self.root/'gateway/tls.key'),
            'client_ca': str(self.root/'ca/ca.crt'), 'allowed_clients': str(self.registry)})
        self.listener = await self.proxy.start()
        port = self.listener.sockets[0].getsockname()[1]
        self.url = f'wss://127.0.0.1:{port}/server-ws'

    async def asyncTearDown(self):
        for listener in (self.listener, self.upstream):
            listener.close()
            await listener.wait_closed()
        self.temp.cleanup()

    def context(self, client='client', ca='ca'):
        context = ssl.create_default_context(cafile=str(self.root/ca/'ca.crt'))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        if client:
            context.load_cert_chain(str(self.root/client/'tls.crt'), str(self.root/client/'tls.key'))
        return context

    def proof(self, directory='client'):
        return json.dumps({'type': 'auth', 'cert': (self.root/directory/'tls.crt').read_text()})

    async def test_valid_mutual_tls_relays_binary_and_text(self):
        async with connect(self.url, ssl=self.context(), server_hostname='bridge.test') as ws:
            self.assertEqual(ws.transport.get_extra_info('ssl_object').version(), 'TLSv1.3')
            await ws.send(self.proof())
            self.assertEqual(await ws.recv(), self.proof())
            await ws.send(b'\x00\x07terminal input')
            self.assertEqual(await ws.recv(), b'\x00\x07terminal input')

    async def test_missing_untrusted_and_expired_clients_are_rejected(self):
        for client in (None, 'untrusted-client', 'expired'):
            with self.subTest(client=client):
                with self.assertRaises(Exception):
                    async with connect(self.url, ssl=self.context(client), server_hostname='bridge.test', open_timeout=2) as ws:
                        await ws.send(self.proof())
                        await ws.recv()
        self.assertEqual(self.received, [])

    async def test_wrong_gateway_name_or_ca_are_rejected(self):
        for name, ca in (('wrong.test', 'ca'), ('bridge.test', 'untrusted-ca')):
            with self.assertRaises(ssl.SSLCertVerificationError):
                async with connect(self.url, ssl=self.context(ca=ca), server_hostname=name):
                    pass
        self.assertEqual(self.received, [])

    async def test_revoked_fingerprint_is_rejected_without_restart(self):
        self.registry.write_text('{}')
        async with connect(self.url, ssl=self.context(), server_hostname='bridge.test') as ws:
            await ws.wait_closed()
            self.assertEqual(ws.close_code, 1008)
        self.assertEqual(self.received, [])

    async def test_transport_certificate_cannot_impersonate_another_bridge(self):
        transport_pki.issue(self.root/'ca', 'other-agent', self.root/'other-agent')
        async with connect(self.url, ssl=self.context(), server_hostname='bridge.test') as ws:
            await ws.send(self.proof('other-agent'))
            await ws.wait_closed()
            self.assertEqual(ws.close_code, 1008)
        self.assertEqual(self.received, [])

    async def test_browser_and_admin_paths_are_unreachable(self):
        for path in ('/', '/api/servers', '/api/enroll', '/ws/hpc/test'):
            with self.assertRaises(Exception):
                async with connect(self.url.removesuffix('/server-ws')+path,
                                   ssl=self.context(), server_hostname='bridge.test'):
                    pass
        self.assertEqual(self.received, [])

    def test_agent_requires_verified_tls_and_refuses_plaintext_fallback(self):
        tls = {'ca': str(self.root/'ca/ca.crt'), 'certificate': str(self.root/'client/tls.crt'),
               'private_key': str(self.root/'client/tls.key'), 'server_hostname': 'bridge.test'}
        with self.assertRaises(ValueError):
            server.websocket_tls_options({'gateway': 'http://127.0.0.1:4601', 'tls': tls})
        options = server.websocket_tls_options({'gateway': self.url, 'tls': tls})
        self.assertTrue(options['ssl'].check_hostname)
        self.assertEqual(options['ssl'].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(options['ssl'].minimum_version, ssl.TLSVersion.TLSv1_3)
        self.assertEqual(server.websocket_tls_options({'gateway': 'http://localhost'}), {})


if __name__ == '__main__':
    unittest.main()
