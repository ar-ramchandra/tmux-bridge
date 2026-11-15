"""Bounded HTTP/WebSocket application relay on a separate authenticated channel."""
from __future__ import annotations
import asyncio
import base64
import json
import re
from urllib.parse import urlsplit

import httpx
from websockets.legacy.client import connect
from auth import create_auth_proof
from protocol import make_rpc_response
from preview_registry import PreviewRegistry

CHUNK = 65536
MAX_BODY = 2 * 1024 * 1024
# Never send browser/Cloudflare identity or hop-by-hop routing to a local app.
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length',
       'forwarded', 'x-real-ip'}


def clean_headers(headers, response=False):
    nominated = set()
    for k, v in headers:
        if k.lower() == 'connection':
            nominated.update(x.strip().lower() for x in v.split(','))
    result = []
    for key, value in headers:
        k = key.lower()
        if (k in HOP or k in nominated or k.startswith(('cf-', 'x-forwarded-', 'sec-websocket-', 'proxy-'))
                or '\r' in value or '\n' in value):
            continue
        if k == 'cookie':
            value = '; '.join(v.strip() for v in value.split(';')
                              if not v.strip().split('=', 1)[0].lower().startswith(('cf_', '__cf', '__host-bridge')))
            if not value:
                continue
        if k == 'set-cookie':
            if value.split('=', 1)[0].strip().lower().startswith(('cf_', '__cf', '__host-bridge')):
                continue
            value = re.sub(r';\s*domain=[^;]*', '', value, flags=re.I)
        if response and k in {'access-control-allow-origin', 'access-control-allow-credentials',
                              'service-worker-allowed', 'alt-svc', 'clear-site-data'}:
            continue
        result.append((key, value))
    return result


class PreviewAgent:
    def __init__(self, root, config, tls_options):
        self.registry = PreviewRegistry(root)
        self.config, self.tls_options = config, tls_options
        self.tasks = set()
        self.streams = {}  # task -> preview id

    async def snapshot(self):
        try:
            return await asyncio.to_thread(self.registry.listing)
        except (ValueError, OSError):
            return []

    async def announce(self, ws):
        rows = await self.snapshot()
        ids = {row['id'] for row in rows}
        for task, ident in list(self.streams.items()):
            if ident not in ids:
                task.cancel()
        await ws.send(json.dumps({'type': 'previews', 'items': rows}))

    async def request(self, ws, msg):
        if len(self.tasks) >= 8:
            await ws.send(make_rpc_response(msg['id'], 429, {'error': 'Preview requests busy'}))
            return
        task = asyncio.create_task(self._request(ws, msg))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _request(self, ws, msg):
        try:
            body = msg.get('body') or {}
            method, path = msg.get('method'), msg.get('path')
            if path == '/api/previews' and method == 'GET':
                result = await self.snapshot()
            elif path == '/api/previews' and method == 'POST':
                result = await asyncio.to_thread(self.registry.create, body.get('session'), body.get('port'), body.get('title', ''), body.get('ttl', 7200))
            elif path == '/api/previews/stop' and method == 'POST':
                result = await asyncio.to_thread(self.registry.stop, body.get('id'))
            elif path == '/api/previews/connect' and method == 'POST':
                row = await asyncio.to_thread(self.registry.get, body.get('id'))
                if len(self.streams) >= 32:
                    raise ValueError('Too many preview connections')
                ticket = body.get('ticket')
                if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket):
                    raise ValueError('Invalid stream ticket')
                task = asyncio.create_task(self._relay(row, ticket))
                self.streams[task] = row['id']
                task.add_done_callback(self.streams.pop)
                result = {'ok': True}
            else:
                raise ValueError('Unknown preview request')
            await self.announce(ws)
            await ws.send(make_rpc_response(msg['id'], 200, result))
        except (ValueError, TypeError, OSError) as e:
            await ws.send(make_rpc_response(msg['id'], 400, {'error': str(e)}))

    async def _relay(self, row, ticket):
        try:
            url = self.config['gateway'].replace('https:', 'wss:').replace('http:', 'ws:')+'/server-ws'
            async with connect(url, max_size=MAX_BODY*2, max_queue=2, compression=None,
                               open_timeout=12, close_timeout=1, ping_interval=20, ping_timeout=30,
                               **self.tls_options(self.config)) as ws:
                root = self.registry.root
                await ws.send(json.dumps(create_auth_proof((root/'server.key').read_bytes(), (root/'server.crt').read_bytes())))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if reply != {'type': 'auth_ok', 'name': self.config['name']}:
                    return
                await ws.send(json.dumps({'type': 'preview', 'ticket': ticket}))
                request = json.loads(await asyncio.wait_for(ws.recv(), 10))
                # Revalidate after stream admission and before opening loopback.
                await asyncio.to_thread(self.registry.get, row['id'])
                remaining = row['expires']-__import__('time').time()
                await asyncio.wait_for(self.serve(ws, row, request), max(.01, remaining))
        except Exception:
            pass  # Do not log request contents, auth cookies, or tickets.

    async def serve(self, ws, row, req):
        path = req['path']
        if not path.startswith('/') or path.startswith('//') or '\\' in path or any(ord(c) < 32 for c in path):
            raise ValueError('Invalid application path')
        target = 'http://127.0.0.1:'+str(row['port'])+path
        headers = clean_headers(req.get('headers', []))
        # Preserve app origin semantics for CSRF and HMR, while fixing its Host.
        public_origin = row['url'].rstrip('/')
        headers = [(k, 'http://127.0.0.1:'+str(row['port']) if k.lower() == 'origin' and v == public_origin else v)
                   for k, v in headers]
        if req['kind'] == 'http':
            body = base64.b64decode(req.get('body', ''), validate=True)
            if len(body) > MAX_BODY or req['method'] not in {'GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'}:
                raise ValueError('Invalid request')
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(30, connect=5, read=None)) as client:
                async with client.stream(req['method'], target, headers=headers, content=body) as response:
                    out = clean_headers(response.headers.multi_items(), response=True)
                    rewritten = []
                    for k, v in out:
                        if k.lower() == 'location':
                            parsed = urlsplit(v)
                            if parsed.hostname in {'localhost','127.0.0.1'} and parsed.port == row['port']:
                                v = public_origin+(parsed.path or '/')+('?' + parsed.query if parsed.query else '')+('#'+parsed.fragment if parsed.fragment else '')
                        rewritten.append((k, v))
                    await ws.send(json.dumps({'status': response.status_code, 'headers': rewritten}))
                    async for chunk in response.aiter_raw():
                        for offset in range(0, len(chunk), CHUNK):
                            await ws.send(chunk[offset:offset+CHUNK])
                    await ws.send(json.dumps({'end': True}))
        elif req['kind'] == 'websocket':
            protocols = req.get('protocols', [])
            async with connect(target.replace('http:', 'ws:', 1), extra_headers=headers,
                               subprotocols=protocols, max_size=2**20, max_queue=2,
                               compression=None, open_timeout=5, close_timeout=1) as upstream:
                await ws.send(json.dumps({'status': 101, 'protocol': upstream.subprotocol}))
                async def copy(source, dest):
                    async for message in source:
                        await dest.send(message)
                tasks = [asyncio.create_task(copy(ws, upstream)), asyncio.create_task(copy(upstream, ws))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        else:
            raise ValueError('Unsupported preview protocol')

    async def stop(self):
        tasks = list(self.tasks) + list(self.streams)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
