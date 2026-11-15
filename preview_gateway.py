"""Isolated preview origins, served only through authenticated node tickets.

Deployment MUST put the preview host suffix behind the same browser identity
policy as the gateway, and validate its Access JWT at the tunnel connector.
"""
from __future__ import annotations
import asyncio
import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, StreamingResponse, FileResponse
from starlette.websockets import WebSocket
from preview_registry import ID_RE
from preview_agent import MAX_BODY, CHUNK, clean_headers


@dataclass
class Link:
    server: str
    ident: str
    expires: float
    ready: asyncio.Future
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    agent: object = None
    task: object = None


class PreviewGateway:
    def __init__(self, get_server, root):
        self.get_server, self.root = get_server, Path(root)
        self.items, self.links = {}, {}
        self.router = APIRouter()
        self.router.add_api_route('/api/previews', self.listing, methods=['GET'])
        self.router.add_api_route('/api/{server}/previews', self.create, methods=['POST'])
        self.router.add_api_route('/api/{server}/previews/{ident}', self.stop, methods=['DELETE'])
        self.router.add_api_route('/previews.js', self.script, methods=['GET'])

    def settings(self):
        try:
            data = json.loads((self.root/'previews.json').read_text())
            if re.fullmatch(r'-preview\.[a-z0-9.-]+', data.get('host_suffix', '')):
                return data
        except (OSError, ValueError, AttributeError):
            pass
        return {}

    async def script(self):
        return FileResponse(Path(__file__).parent/'tmux_bridge_assets/previews.js', media_type='application/javascript')

    def live(self, row):
        conn = self.get_server(row['server'])
        return bool(conn and conn.is_active and row['expires'] > time.time()
                    and any(s.get('name') == row['session'] and int(s.get('created', -1)) == row['created'] for s in conn.sessions))

    def update(self, server, rows):
        config = self.settings()
        if not config.get('enabled') or not isinstance(rows, list):
            return
        fresh = {}
        for item in rows[:16]:
            try:
                row = {k: item[k] for k in ('id', 'session', 'created', 'port', 'title', 'expires')}
                if (not ID_RE.fullmatch(row['id']) or not isinstance(row['session'], str)
                        or type(row['created']) is not int or type(row['port']) is not int
                        or not 1024 <= row['port'] <= 65535 or not isinstance(row['title'], str)
                        or len(row['title']) > 80 or not time.time() < row['expires'] <= time.time()+86401):
                    continue
                existing = self.items.get(row['id'])
                if existing and existing['server'] != server:
                    continue
                row.update(server=server, url='https://'+row['id']+config['host_suffix']+'/')
                if self.live(row):
                    fresh[row['id']] = row
            except (KeyError, TypeError, ValueError):
                continue
        self.items = {k: v for k, v in self.items.items() if v['server'] != server}
        self.items.update(fresh)
        for link in list(self.links.values()):
            if link.server == server and link.ident not in fresh and link.task:
                link.task.cancel()

    async def listing(self):
        return {'enabled': self.settings().get('enabled') is True, 'previews': [r for r in self.items.values() if self.live(r)]}

    async def create(self, server: str, request: Request):
        if not self.settings().get('enabled'):
            return JSONResponse({'error': 'Private previews are not configured'}, status_code=409)
        conn = self.get_server(server)
        if not conn or not conn.is_active:
            return JSONResponse({'error': 'Device is offline'}, status_code=503)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            return JSONResponse({'error': 'Invalid preview request'}, status_code=400)
        sessions = await conn.rpc('GET', '/api/sessions')
        if sessions['status'] == 200 and isinstance(sessions['body'], list):
            conn.sessions = sessions['body']
        result = await conn.rpc('POST', '/api/previews', body)
        return JSONResponse(result['body'], status_code=result['status'])

    async def stop(self, server: str, ident: str):
        row = self.items.get(ident)
        if not row or row['server'] != server:
            return JSONResponse({'error': 'Preview not found'}, status_code=404)
        conn = self.get_server(server)
        if not conn or not conn.is_active:
            return JSONResponse({'error': 'Device is offline'}, status_code=503)
        result = await conn.rpc('POST', '/api/previews/stop', {'id': ident})
        if result['status'] == 200:
            self.items.pop(ident, None)
            for link in list(self.links.values()):
                if link.ident == ident and link.task:
                    link.task.cancel()
        return JSONResponse(result['body'], status_code=result['status'])

    async def accept_agent(self, ws, server, ticket):
        link = self.links.get(ticket) if isinstance(ticket, str) else None
        if not link or link.server != server or link.agent or link.expires <= time.monotonic():
            await ws.close(code=1008)
            return
        link.agent = ws
        if not link.ready.done():
            link.ready.set_result(ws)
        await link.finished.wait()

    async def close_node(self, server):
        self.items = {k:v for k,v in self.items.items() if v['server'] != server}
        for link in list(self.links.values()):
            if link.server == server and link.task:
                link.task.cancel()

    async def worker(self):
        while True:
            await asyncio.sleep(1)
            for ident, row in list(self.items.items()):
                if not self.live(row):
                    self.items.pop(ident, None)
            for link in list(self.links.values()):
                if link.ident not in self.items and link.task:
                    link.task.cancel()

    async def relay(self, row, scope, receive, send):
        if len(self.links) >= 64 or sum(l.server == row['server'] for l in self.links.values()) >= 24:
            await self.error(scope, receive, send, 429, 'Preview connections busy')
            return
        ticket = secrets.token_urlsafe(32)
        link = Link(row['server'], row['id'], time.monotonic()+20, asyncio.get_running_loop().create_future(), task=asyncio.current_task())
        self.links[ticket] = link
        browser = None
        started = False
        try:
            req = Request(scope, receive) if scope['type'] == 'http' else None
            raw_path = scope.get('raw_path', scope['path'].encode()).decode('ascii')
            query = scope.get('query_string', b'').decode('ascii')
            data = {'kind': scope['type'], 'path': raw_path+('?' + query if query else ''),
                    'headers': clean_headers([(k.decode('latin1'), v.decode('latin1')) for k,v in scope['headers']])}
            if req:
                body = bytearray()
                async def read_body():
                    async for part in req.stream():
                        body.extend(part)
                        if len(body) > MAX_BODY:
                            raise ValueError('Upload limit is 2 MiB')
                await asyncio.wait_for(read_body(), 30)
                data.update(method=req.method, body=base64.b64encode(body).decode())
            else:
                browser = WebSocket(scope, receive, send)
                data['protocols'] = scope.get('subprotocols', [])
            conn = self.get_server(row['server'])
            if not conn or not self.live(row):
                raise ValueError('Preview unavailable')
            result = await conn.rpc('POST', '/api/previews/connect', {'id': row['id'], 'ticket': ticket})
            if result['status'] != 200:
                raise ValueError('Preview unavailable')
            agent = await asyncio.wait_for(asyncio.shield(link.ready), 15)
            await agent.send_text(json.dumps(data))
            head = json.loads(await asyncio.wait_for(agent.receive_text(), 30))
            if browser:
                if head.get('status') != 101 or head.get('protocol') not in [None]+data['protocols']:
                    raise ValueError('Application WebSocket unavailable')
                await browser.accept(subprotocol=head.get('protocol'))
                started = True
                async def copy(source, dest):
                    while True:
                        msg = await source.receive()
                        if msg['type'] == 'websocket.disconnect':
                            return
                        if msg.get('bytes') is not None:
                            if len(msg['bytes']) > 2**20:
                                raise ValueError('Frame too large')
                            await dest.send_bytes(msg['bytes'])
                        elif msg.get('text') is not None:
                            if len(msg['text'].encode()) > 2**20:
                                raise ValueError('Frame too large')
                            await dest.send_text(msg['text'])
                tasks = [asyncio.create_task(copy(browser, agent)), asyncio.create_task(copy(agent, browser))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                status = head['status']
                if type(status) is not int or not 200 <= status <= 599:
                    raise ValueError('Invalid application response')
                async def content():
                    while True:
                        msg = await agent.receive()
                        if msg.get('bytes') is not None:
                            if len(msg['bytes']) > CHUNK:
                                raise ValueError('Frame too large')
                            yield msg['bytes']
                        elif msg.get('text') and json.loads(msg['text']).get('end') is True:
                            return
                        else:
                            raise ValueError('Application disconnected')
                response = StreamingResponse(content(), status_code=status)
                response.raw_headers = [(k.encode('latin1'),v.encode('latin1')) for k,v in clean_headers(head.get('headers', []), response=True)
                                        if k.lower() not in {'content-security-policy','x-frame-options','cache-control','referrer-policy'}]
                response.raw_headers.extend([(b'cache-control',b'no-store'),(b'referrer-policy',b'no-referrer'),
                    (b'content-security-policy',b"frame-ancestors 'none'; object-src 'none'"),
                    (b'x-frame-options',b'DENY'),(b'x-content-type-options',b'nosniff')])
                started = True
                await response(scope, receive, send)
        except asyncio.CancelledError:
            if not started:
                await self.error(scope, receive, send, 410, 'Preview stopped')
            # Cancelled active responses must be closed, never masquerade as a
            # successful truncated HTTP body.
            else:
                raise
        except Exception:
            if not started:
                await self.error(scope, receive, send, 502, 'Application unavailable; check its local port')
            else:
                raise
        finally:
            self.links.pop(ticket, None)
            link.finished.set()
            for peer in (browser, link.agent):
                if peer:
                    try:
                        await peer.close()
                    except Exception:
                        pass

    @staticmethod
    async def error(scope, receive, send, status, detail):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
        else:
            await JSONResponse({'error': detail}, status_code=status, headers={'Cache-Control':'no-store'})(scope, receive, send)


class PreviewOriginMiddleware:
    """Host dispatch happens before FastAPI; preview hosts cannot reach controls."""
    def __init__(self, app, previews):
        self.app, self.previews = app, previews

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http','websocket'}:
            return await self.app(scope, receive, send)
        config = self.previews.settings()
        headers = {k.decode('latin1').lower():v.decode('latin1') for k,v in scope['headers']}
        host = headers.get('host', '').lower().split(':')[0]
        if not config:
            if '-preview.' in host:
                return await self.previews.error(scope, receive, send, 410, 'Previews are disabled')
            return await self.app(scope, receive, send)
        controls = config.get('gateway_hosts', []) + ['127.0.0.1','localhost','[']
        if host in controls:
            return await self.app(scope, receive, send)
        suffix = config['host_suffix']
        ident = host[:-len(suffix)] if host.endswith(suffix) else ''
        row = self.previews.items.get(ident) if ID_RE.fullmatch(ident) else None
        if not config.get('enabled') or not row or not self.previews.live(row):
            return await self.previews.error(scope, receive, send, 410, 'Preview has stopped or expired')
        origin = headers.get('origin')
        expected = 'https://'+host
        # Deny browser requests from another app, including same-site siblings.
        if (origin and origin != expected) or headers.get('sec-fetch-site') == 'cross-site' or (scope['type'] == 'websocket' and origin != expected):
            return await self.previews.error(scope, receive, send, 403, 'Cross-origin request denied')
        return await self.previews.relay(row, scope, receive, send)
