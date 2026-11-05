"""Single-viewer desktop relay. The node initiates its own authenticated stream."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import secrets
import time

from fastapi import APIRouter, WebSocket
from fastapi.responses import FileResponse, JSONResponse


@dataclass
class DesktopLink:
    server: str
    browser: WebSocket
    expires: float
    ready: asyncio.Future
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    agent: WebSocket | None = None


class DesktopGateway:
    def __init__(self, get_server):
        self.get_server = get_server
        self.links = {}
        self.router = APIRouter()
        self.router.add_api_route('/api/{server}/desktop', self.status, methods=['GET'])
        self.router.add_api_websocket_route('/ws/desktop/{server}', self.browser)
        self.router.add_api_route('/desktop.js', self.script, methods=['GET'])
        self.router.add_api_route('/desktop-assets/{name:path}', self.asset, methods=['GET'])

    async def script(self):
        return FileResponse(Path(__file__).parent/'tmux_bridge_assets/desktop.js', media_type='application/javascript')

    async def asset(self, name: str):
        root = (Path(__file__).parent/'tmux_bridge_assets/novnc').resolve()
        path = (root/name).resolve()
        if not path.is_relative_to(root) or path.suffix != '.js' or not path.is_file():
            return JSONResponse({'error': 'Not found'}, status_code=404)
        return FileResponse(path, media_type='application/javascript')

    async def status(self, server: str):
        conn = self.get_server(server)
        if not conn or not conn.is_active:
            return JSONResponse({'error': 'Device is offline'}, status_code=503)
        result = await conn.rpc('GET', '/api/desktop')
        if result['status'] == 404:
            return JSONResponse({'enabled': False, 'available': False, 'message': 'This device needs an agent update.'})
        return JSONResponse(result['body'], status_code=result['status'])

    async def accept_agent(self, ws, server, ticket):
        link = self.links.get(ticket) if isinstance(ticket, str) else None
        if not link or link.server != server or link.agent or link.expires < time.monotonic():
            await ws.close(code=1008, reason='Desktop request expired')
            return
        link.agent = ws
        link.ready.set_result(ws)
        await link.finished.wait()

    async def browser(self, ws: WebSocket, server: str):
        await ws.accept()
        conn = self.get_server(server)
        if not conn or not conn.is_active:
            await ws.close(code=1013, reason='Device is offline')
            return
        if len(self.links) >= 16 or any(link.server == server for link in self.links.values()):
            await ws.close(code=1013, reason='Desktop is already open. Close the other viewer first.')
            return
        ticket = secrets.token_urlsafe(32)
        link = DesktopLink(server, ws, time.monotonic()+20, asyncio.get_running_loop().create_future())
        self.links[ticket] = link
        pumps = []
        try:
            result = await conn.rpc('POST', '/api/desktop/connect', {'ticket': ticket})
            if result['status'] != 202:
                await ws.close(code=1013, reason='Desktop unavailable. Check device setup.')
                return
            agent = await asyncio.wait_for(asyncio.shield(link.ready), max(.1, link.expires-time.monotonic()))

            async def copy(source, target):
                while True:
                    message = await source.receive()
                    data = message.get('bytes')
                    if message['type'] == 'websocket.disconnect':
                        return
                    if data is None or len(data) > 2**20:
                        raise ValueError('Invalid desktop frame')
                    await target.send_bytes(data)

            pumps = [asyncio.create_task(copy(ws, agent)), asyncio.create_task(copy(agent, ws))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            self.links.pop(ticket, None)
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            link.finished.set()
            for peer in [ws, link.agent]:
                if peer:
                    try:
                        await peer.close()
                    except Exception:
                        pass

    async def close_node(self, server):
        for link in list(self.links.values()):
            if link.server == server:
                try:
                    await link.browser.close(code=1013, reason='Device disconnected')
                except Exception:
                    pass
                if not link.ready.done():
                    link.ready.cancel()
