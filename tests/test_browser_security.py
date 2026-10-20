import unittest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from browser_security import BrowserOriginMiddleware


class BrowserSecurityTests(unittest.TestCase):
    def setUp(self):
        app=FastAPI();app.add_middleware(BrowserOriginMiddleware)
        @app.post('/command')
        async def command():return {'ok':True}
        @app.websocket('/ws/terminal')
        async def terminal(ws:WebSocket):
            await ws.accept();await ws.send_text('connected');await ws.close()
        @app.websocket('/server-ws')
        async def agent(ws:WebSocket):
            await ws.accept();await ws.send_text('auth-required');await ws.close()
        self.client=TestClient(app)

    def test_commands_reject_cross_origin_and_allow_same_origin(self):
        for origin in ['https://evil.example','null','http://testserver.evil.example','http://testserver:8080','http://testserver/path','http://user@testserver']:
            self.assertEqual(self.client.post('/command',headers={'Origin':origin}).status_code,403)
        self.assertEqual(self.client.post('/command',headers={'Origin':'http://testserver'}).status_code,200)
        self.assertEqual(self.client.post('/command').status_code,200)
        self.assertEqual(self.client.post('/command',headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)

    def test_terminal_rejects_foreign_or_missing_origin(self):
        for headers in [{},{'Origin':'https://evil.example'},{'Origin':'null'}]:
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect('/ws/terminal',headers=headers):pass
        with self.client.websocket_connect('/ws/terminal',headers={'Origin':'http://testserver'}) as ws:
            self.assertEqual(ws.receive_text(),'connected')

    def test_agent_auth_channel_does_not_need_browser_origin(self):
        with self.client.websocket_connect('/server-ws') as ws:
            self.assertEqual(ws.receive_text(),'auth-required')

    def test_https_origin_matches_forwarded_secure_connection(self):
        client=TestClient(self.client.app,base_url='https://remote.example')
        self.assertEqual(client.post('/command',headers={'Origin':'https://remote.example'}).status_code,200)
        self.assertEqual(client.post('/command',headers={'Origin':'http://remote.example'}).status_code,403)

    def test_authenticated_content_cannot_be_framed_or_cached(self):
        response=self.client.post('/command')
        self.assertEqual(response.headers['x-frame-options'],'DENY')
        self.assertIn("frame-ancestors 'none'",response.headers['content-security-policy'])
        self.assertEqual(response.headers['cache-control'],'no-store')
