import ast
import asyncio
import json
import pathlib
import unittest
from types import SimpleNamespace


class PresenceTests(unittest.TestCase):
    def test_same_session_name_on_two_servers_keeps_separate_control(self):
        tree = ast.parse((pathlib.Path(__file__).resolve().parents[1] / 'gateway.py').read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'broadcast_presence')
        namespace = {'json': json, 'asyncio': asyncio, 'WebSocketState': SimpleNamespace(CONNECTED='connected')}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'gateway.py', 'exec'), namespace)
        class Socket:
            client_state = 'connected'
            def __init__(self): self.messages = []
            async def send_text(self, text): self.messages.append(json.loads(text))
        a, b = Socket(), Socket()
        def session(server, socket, owner):
            return SimpleNamespace(server_conn=SimpleNamespace(server_name=server), session_name='main',
                                   viewers={socket: None}, get_viewers_info=lambda: [{'id': owner, 'active': True}])
        manager = SimpleNamespace(proxy_sessions={'a/main': session('a', a, 'owner-a'),
                                                 'b/main': session('b', b, 'owner-b')})
        asyncio.run(namespace['broadcast_presence'](manager))
        self.assertEqual(a.messages[0]['sessions']['main']['viewers'][0]['id'], 'owner-a')
        self.assertEqual(b.messages[0]['sessions']['main']['viewers'][0]['id'], 'owner-b')
