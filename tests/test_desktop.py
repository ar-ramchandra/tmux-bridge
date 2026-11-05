import asyncio
import json
from pathlib import Path
import secrets
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI, WebSocket
import uvicorn
from websockets.legacy.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

import auth
from browser_security import BrowserOriginMiddleware
from desktop_agent import DesktopAgent, desktop_port, desktop_status
from desktop_gateway import DesktopGateway, DesktopLink


class DesktopConfigTests(unittest.TestCase):
    def test_explicit_opt_in_and_bounded_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(desktop_port(root))
            for data in [{}, {'enabled': 'true'}, {'enabled': True, 'port': True},
                         {'enabled': True, 'port': 22}, {'enabled': True, 'port': 65536}]:
                (root/'desktop.json').write_text(json.dumps(data))
                self.assertIsNone(desktop_port(root))
            (root/'desktop.json').write_text('{"enabled":true,"port":5900}')
            self.assertEqual(desktop_port(root), 5900)


class DesktopRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ca = self.root/'gateway'
        self.patch = patch.multiple(auth, CONFIG_DIR=ca, CA_KEY_FILE=ca/'ca.key',
                                   CA_CERT_FILE=ca/'ca.crt', SERVERS_DIR=ca/'servers',
                                   TOKENS_DIR=ca/'tokens', REVOKED_FILE=ca/'revoked.json')
        self.patch.start()
        auth.ensure_config_dir()
        auth.init_ca()
        key, cert, _ = auth.issue_server_cert('node')
        (self.root/'server.key').write_bytes(key)
        (self.root/'server.crt').write_bytes(cert)
        self.vnc = await asyncio.start_server(self.echo_desktop, '127.0.0.1', 0)
        port = self.vnc.sockets[0].getsockname()[1]
        (self.root/'desktop.json').write_text(json.dumps({'enabled': True, 'port': port}))
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        self.port = listener.getsockname()[1]
        self.agent = DesktopAgent(self.root, {'gateway': 'http://127.0.0.1:'+str(self.port), 'name': 'node'}, lambda config: {})
        agent = self.agent
        class Connection:
            is_active = True
            async def rpc(self, method, path, body=None):
                future = asyncio.get_running_loop().create_future()
                class Reply:
                    async def send(self, message):
                        result = json.loads(message)
                        future.set_result({'status': result['status'], 'body': result['body']})
                await agent.request(Reply(), {'id': 'test', 'method': method, 'path': path, 'body': body})
                return await asyncio.wait_for(future, 5)
        self.gateway = DesktopGateway(lambda name: Connection() if name == 'node' else None)
        app = FastAPI()
        app.add_middleware(BrowserOriginMiddleware)
        app.include_router(self.gateway.router)
        @app.websocket('/server-ws')
        async def server(ws: WebSocket):
            await ws.accept()
            proof = json.loads(await ws.receive_text())
            name = auth.verify_auth_proof(proof['cert'].encode(), proof['signature'], proof['timestamp'])
            await ws.send_json({'type': 'auth_ok', 'name': name})
            msg = json.loads(await ws.receive_text())
            self.assertEqual(msg['type'], 'desktop')
            await self.gateway.accept_agent(ws, name, msg['ticket'])
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        self.running = asyncio.create_task(self.server.serve(sockets=[listener]))
        while not self.server.started:
            await asyncio.sleep(.01)

    async def asyncTearDown(self):
        await self.agent.stop()
        self.server.should_exit = True
        await asyncio.wait_for(self.running, 5)
        self.vnc.close()
        await self.vnc.wait_closed()
        self.patch.stop()
        self.temp.cleanup()

    async def echo_desktop(self, reader, writer):
        try:
            writer.write(b'RFB 003.008\n')
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_real_authenticated_binary_stream_and_exclusive_control(self):
        self.assertTrue((await desktop_status(self.root))['available'])
        url = 'ws://127.0.0.1:'+str(self.port)+'/ws/desktop/node'
        origin = 'http://127.0.0.1:'+str(self.port)
        async with connect(url, origin=origin) as browser:
            self.assertEqual(await asyncio.wait_for(browser.recv(), 5), b'RFB 003.008\n')
            payload = bytes(range(256))*1024
            await browser.send(payload)
            received = b''
            while len(received) < len(payload):
                received += await asyncio.wait_for(browser.recv(), 5)
            self.assertEqual(received, payload)
            async with connect(url, origin=origin) as other:
                with self.assertRaises(ConnectionClosed):
                    await other.recv()
                self.assertEqual(other.close_code, 1013)
            await browser.send(b'still connected')
            self.assertEqual(await browser.recv(), b'still connected')
        for _ in range(100):
            if not self.gateway.links and self.agent.stream.done():
                break
            await asyncio.sleep(.01)
        self.assertFalse(self.gateway.links)
        self.assertTrue(self.agent.stream.done())

    async def test_browser_requires_same_origin(self):
        url = 'ws://127.0.0.1:'+str(self.port)+'/ws/desktop/node'
        for origin in [None, 'https://foreign.example']:
            with self.assertRaises(InvalidStatusCode):
                async with connect(url, origin=origin):
                    pass
        self.assertFalse(self.gateway.links)

    async def test_ticket_bound_to_identity_expiry_and_single_use(self):
        class Peer:
            def __init__(self): self.code = None
            async def close(self, code=1000, reason=''): self.code = code
        ticket = secrets.token_urlsafe(32)
        link = DesktopLink('node', Peer(), time.monotonic()+20, asyncio.get_running_loop().create_future())
        self.gateway.links[ticket] = link
        foreign = Peer()
        await self.gateway.accept_agent(foreign, 'another-node', ticket)
        self.assertEqual(foreign.code, 1008)
        self.assertFalse(link.ready.done())
        first = Peer()
        task = asyncio.create_task(self.gateway.accept_agent(first, 'node', ticket))
        self.assertIs(await link.ready, first)
        second = Peer()
        await self.gateway.accept_agent(second, 'node', ticket)
        self.assertEqual(second.code, 1008)
        link.finished.set()
        await task
        link.agent = None
        link.expires = time.monotonic()-1
        expired = Peer()
        await self.gateway.accept_agent(expired, 'node', ticket)
        self.assertEqual(expired.code, 1008)
        self.gateway.links.clear()
