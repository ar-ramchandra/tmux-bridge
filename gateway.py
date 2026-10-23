#!/usr/bin/env python3
"""
Gateway — user-facing web UI that proxies to terminal servers.
Servers connect inward via WebSocket (reverse connection model).
Owns: HTML/JS/CSS, layout persistence, viewer tracking, presence, cert auth.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
import hashlib
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketState

import os
import pty
import fcntl
import struct
import termios
import signal

from auth import (
    verify_auth_proof, issue_server_cert, consume_enrollment_token,
    ca_exists, list_registered_servers, revoke_server,
)
from protocol import (
    MSG_AUTH, MSG_REGISTER, MSG_CONNECT, MSG_DISCONNECT, MSG_RESIZE,
    MSG_RPC_REQUEST, MSG_RPC_RESPONSE, MSG_SESSION_UPDATE, MSG_CHANNEL_CLOSED, MSG_PING, MSG_PONG,
    pack_data, unpack_data, make_connect, make_disconnect, make_resize,
    make_rpc_request, make_ping, parse_message,
)
import cloudflare
from browser_security import BrowserOriginMiddleware

app = FastAPI()
app.add_middleware(BrowserOriginMiddleware)

LAYOUT_FILE = (Path(os.environ["TMUX_BRIDGE_CONFIG_DIR"]).expanduser() / "layout.json"
               if os.environ.get("TMUX_BRIDGE_CONFIG_DIR") else Path.home() / ".config/tmux-bridge/layout.json")


def load_layout() -> dict:
    try:
        return json.loads(LAYOUT_FILE.read_text())
    except Exception:
        return {"projects": [], "assignments": {}, "sessionOrder": [], "collapsed": {}}


def save_layout(data: dict):
    LAYOUT_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    LAYOUT_FILE.write_text(json.dumps(data, indent=2))


# ── ViewerInfo ──

@dataclass
class ViewerInfo:
    ws: WebSocket
    viewer_id: str
    device: str
    label: str
    session_name: str = ""
    cols: int = 120
    rows: int = 40
    connected_at: float = field(default_factory=time.time)
    last_typed: float = 0.0
    ready: bool = False
    ui_version: str = ""
    scroll_events: int = 0
    last_scroll_ok: bool | None = None

    @property
    def is_typing(self) -> bool:
        if not self.ready:
            return False
        if (time.time() - self.connected_at) < 2.0:
            return False
        return (time.time() - self.last_typed) < 3.0

    def to_dict(self, is_active: bool) -> dict:
        return {
            "id": self.viewer_id,
            "device": self.device,
            "label": self.label,
            "active": is_active,
            "typing": self.is_typing,
            "loading": not self.ready,
            "connected_at": self.connected_at,
            "ui_version": self.ui_version,
            "scroll_events": self.scroll_events,
            "last_scroll_ok": self.last_scroll_ok,
        }


# ── ServerConnection: manages one server's reverse WebSocket ──

class ServerConnection:
    """Represents a connected server (server connects inward to gateway)."""

    def __init__(self, ws, server_name: str):
        self.ws = ws
        self.server_name = server_name
        self.connected_at = time.time()
        self.sessions: list[dict] = []  # latest session list from server
        self._next_channel = 1
        self._rpc_futures: dict[str, asyncio.Future] = {}
        self._active = True

    def allocate_channel(self) -> int:
        ch = self._next_channel
        self._next_channel += 1
        if self._next_channel > 65535:
            self._next_channel = 1
        return ch

    async def send_text(self, msg: str):
        try:
            await self.ws.send_text(msg)
        except Exception:
            self._active = False

    async def send_bytes(self, data: bytes):
        try:
            await self.ws.send_bytes(data)
        except Exception:
            self._active = False

    async def rpc(self, method: str, path: str, body: dict | None = None,
                  timeout: float = 10.0) -> dict:
        """Send RPC request and wait for response."""
        req_id = str(uuid.uuid4())[:8]
        future = asyncio.get_event_loop().create_future()
        self._rpc_futures[req_id] = future

        await self.send_text(make_rpc_request(req_id, method, path, body))

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._rpc_futures.pop(req_id, None)
            return {"status": 504, "body": {"error": "RPC timeout"}}

    def resolve_rpc(self, req_id: str, status: int, body):
        future = self._rpc_futures.pop(req_id, None)
        if future and not future.done():
            future.set_result({"status": status, "body": body})

    @property
    def is_active(self) -> bool:
        return self._active


# ── ProxySession: manages browser viewers + channel to server ──

class ProxySession:
    def __init__(self, server_conn: ServerConnection, session_name: str):
        self.server_conn = server_conn
        self.session_name = session_name
        self.channel: int | None = None
        self.viewers: dict[WebSocket, ViewerInfo] = {}
        self.active_input: WebSocket | None = None
        self.buffer = deque(maxlen=100000)
        self._closing = False
        self._channel_open = False

    async def ensure_channel(self, cols: int = 120, rows: int = 40):
        """Ask server to open a terminal channel for this session."""
        if self._channel_open:
            return
        self.channel = self.server_conn.allocate_channel()
        self._channel_open = True
        await self.server_conn.send_text(
            make_connect(self.channel, self.session_name, cols, rows)
        )

    def receive_data(self, data: bytes):
        """Called when terminal data arrives from server for this channel."""
        self.buffer.extend(data)
        # Broadcast synchronously-ish via create_task
        asyncio.create_task(self._broadcast_to_viewers(data))

    async def _broadcast_to_viewers(self, data: bytes):
        dead = set()
        for ws in list(self.viewers.keys()):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_bytes(data)
                else:
                    dead.add(ws)
            except Exception:
                dead.add(ws)
        for ws in dead:
            await self.remove_viewer(ws)

    async def _broadcast_text_to_viewers(self, text: str):
        dead = set()
        for ws in list(self.viewers.keys()):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(text)
                else:
                    dead.add(ws)
            except Exception:
                dead.add(ws)
        for ws in dead:
            await self.remove_viewer(ws)

    def _resize_to_active(self):
        if not self.viewers:
            return
        if self.active_input and self.active_input in self.viewers:
            info = self.viewers[self.active_input]
            cols, rows = info.cols, info.rows
        else:
            cols = max(v.cols for v in self.viewers.values())
            rows = max(v.rows for v in self.viewers.values())
        if self._channel_open and self.channel is not None:
            asyncio.create_task(
                self.server_conn.send_text(make_resize(self.channel, cols, rows))
            )

    async def add_viewer(self, ws: WebSocket, info: ViewerInfo) -> bytes:
        # Reconnects can arrive before a broken socket times out. Transfer
        # ownership from that browser's old socket before publishing presence.
        replaced = [old for old, viewer in self.viewers.items()
                    if old is not ws and viewer.viewer_id == info.viewer_id]
        owns_input = self.active_input in replaced
        for old in replaced:
            self.viewers.pop(old, None)
        self.viewers[ws] = info
        if owns_input or self.active_input is None:
            self.active_input = ws
        for old in replaced:
            asyncio.create_task(self._close_replaced_viewer(old))
        await self.ensure_channel(cols=info.cols, rows=info.rows)
        self._resize_to_active()
        await gw_manager.broadcast_presence()
        return bytes(self.buffer)

    async def _close_replaced_viewer(self, ws):
        try:
            await asyncio.wait_for(
                ws.close(code=1000, reason="replaced by browser reconnect"), timeout=1,
            )
        except Exception:
            pass

    async def remove_viewer(self, ws: WebSocket):
        if ws not in self.viewers:
            return
        self.viewers.pop(ws, None)
        if self.active_input == ws:
            if self.viewers:
                self.active_input = max(
                    self.viewers.keys(),
                    key=lambda w: self.viewers[w].connected_at
                )
            else:
                self.active_input = None
        self._resize_to_active()
        await gw_manager.broadcast_presence()
        if not self.viewers:
            await self.close_channel()

    async def set_active(self, ws: WebSocket):
        if ws in self.viewers:
            self.active_input = ws
            self._resize_to_active()
            await gw_manager.broadcast_presence()

    async def release_active(self, ws: WebSocket):
        if self.active_input == ws:
            others = [w for w in self.viewers if w != ws]
            if others:
                self.active_input = max(others, key=lambda w: self.viewers[w].connected_at)
            else:
                self.active_input = None
            self._resize_to_active()
            await gw_manager.broadcast_presence()

    async def write_input(self, ws: WebSocket, data: bytes) -> bool:
        if self.active_input != ws:
            try:
                await ws.send_text(json.dumps({"type": "no_control"}))
            except Exception:
                pass
            return False
        if self._channel_open and self.channel is not None:
            await self.server_conn.send_bytes(pack_data(self.channel, data))
            info = self.viewers.get(ws)
            if info:
                was_typing = info.is_typing
                info.last_typed = time.time()
                if not was_typing:
                    gw_manager.request_presence()
            return True
        return False

    def get_viewers_info(self) -> list[dict]:
        result = []
        for ws, info in self.viewers.items():
            result.append(info.to_dict(ws == self.active_input))
        result.sort(key=lambda v: (not v["active"], -v["connected_at"]))
        return result

    async def close_channel(self):
        self._closing = True
        if self._channel_open and self.channel is not None:
            await self.server_conn.send_text(make_disconnect(self.channel))
        self._channel_open = False
        self.channel = None
        self._closing = False

    def notify_server_disconnected(self):
        """Server went away — notify all viewers."""
        self._channel_open = False
        self.channel = None
        self.buffer.clear()
        asyncio.create_task(self._close_viewers(list(self.viewers), "Server connection lost. Reconnecting…"))

    async def terminal_ended(self, reason):
        """A live agent can lose tmux: do not leave browsers on a dead channel."""
        self._channel_open = False
        self.channel = None
        self.buffer.clear()
        await self._close_viewers(list(self.viewers), reason)

    async def _close_viewers(self, viewers, reason):
        for ws in viewers:
            try:
                await ws.send_text(json.dumps({"type": "error", "msg": reason}))
                await ws.close(code=1012, reason="terminal reconnect required")
            except Exception:
                pass


# ── GatewayManager ──

class GatewayManager:
    def __init__(self):
        self.proxy_sessions: dict[str, ProxySession] = {}  # "server/session" -> ProxySession
        self.server_connections: dict[str, ServerConnection] = {}  # server_name -> ServerConnection
        self._presence_task = None
        self._presence_broadcast_task = None
        self._presence_dirty = False

    async def start(self):
        self._presence_task = asyncio.create_task(self._presence_loop())

    def get_server(self, server_name: str) -> ServerConnection | None:
        return self.server_connections.get(server_name)

    async def register_server(self, server_name: str, conn: ServerConnection):
        # Disconnect old connection if any
        old = self.server_connections.pop(server_name, None)
        if old:
            old._active = False
            try:
                await old.ws.close(code=1012, reason="replaced by new connection")
            except Exception:
                pass
        self.server_connections[server_name] = conn
        # A replacement can arrive before the old socket reports a disconnect.
        # Its terminal channels no longer exist: make existing browsers reconnect
        # rather than leaving them apparently connected with nowhere to send input.
        for key, ps in self.proxy_sessions.items():
            if key.startswith(f"{server_name}/"):
                ps.server_conn = conn
                ps.notify_server_disconnected()

    async def unregister_server(self, server_name: str):
        conn = self.server_connections.pop(server_name, None)
        if conn:
            conn._active = False
        # Notify all proxy sessions for this server
        for key, ps in list(self.proxy_sessions.items()):
            if key.startswith(f"{server_name}/"):
                ps.notify_server_disconnected()

    async def get_proxy(self, server_name: str, session_name: str) -> ProxySession | None:
        conn = self.get_server(server_name)
        if not conn or not conn.is_active:
            return None
        key = f"{server_name}/{session_name}"
        if key not in self.proxy_sessions:
            self.proxy_sessions[key] = ProxySession(conn, session_name)
        return self.proxy_sessions[key]

    def request_presence(self):
        """Coalesce typing indicators without waiting on browser writes."""
        self._presence_dirty = True
        if self._presence_broadcast_task is None or self._presence_broadcast_task.done():
            self._presence_broadcast_task = asyncio.create_task(self._flush_presence())

    async def _flush_presence(self):
        while self._presence_dirty:
            self._presence_dirty = False
            await self.broadcast_presence()

    async def broadcast_presence(self):
        # Identical session names on different servers have separate owners.
        by_server = {}
        sessions = list(self.proxy_sessions.values())
        for ps in sessions:
            if not ps.viewers:
                continue
            presence = by_server.setdefault(ps.server_conn.server_name, {})
            presence[ps.session_name] = {"viewers": ps.get_viewers_info(), "tmux_clients": []}

        async def send(ws, msg):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await asyncio.wait_for(ws.send_text(msg), timeout=1)
            except Exception:
                pass

        writes = []
        for ps in sessions:
            msg = json.dumps({"type": "presence", "sessions": by_server.get(ps.server_conn.server_name, {})})
            writes.extend(send(ws, msg) for ws in list(ps.viewers))
        # A slow viewer must not delay presence/control for every other viewer.
        if writes:
            await asyncio.gather(*writes)

    async def _presence_loop(self):
        ticks = 0
        while True:
            await asyncio.sleep(2)
            ticks += 1
            needs_update = False
            for ps in self.proxy_sessions.values():
                for info in ps.viewers.values():
                    if 3.0 < (time.time() - info.last_typed) < 5.0:
                        needs_update = True
            if ticks >= 5:
                ticks = 0
                needs_update = True
            if needs_update:
                await self.broadcast_presence()
            # Clean up empty proxy sessions
            dead = [k for k, ps in self.proxy_sessions.items()
                    if not ps.viewers and not ps._channel_open]
            for k in dead:
                del self.proxy_sessions[k]


gw_manager = GatewayManager()


@app.on_event("startup")
async def startup():
    await gw_manager.start()
    asyncio.create_task(_auto_start_cloudflared())


async def _auto_start_cloudflared():
    """Start cloudflared on gateway boot if configured."""
    if os.environ.get("TMUX_BRIDGE_AUTO_TUNNEL", "0") == "0":
        return
    config = cloudflare.load_config()
    if not config.get("cf_api_token"):
        return
    try:
        result = await cloudflare.start_cloudflared(config)
        if result:
            if config.get("quick_tunnel"):
                config["domain"] = result
                cloudflare.save_config(config)
            print(f"[GW] Tunnel running: https://{config.get('domain', result)}", flush=True)
        else:
            print("[GW] Failed to start cloudflared", flush=True)
    except Exception as e:
        print(f"[GW] Cloudflared error: {e}", flush=True)


# ── Static assets ──

@app.get("/")
async def index():
    return HTMLResponse(HTML, headers={
        "X-Bridge-Version": "2026-10-22-terminal-focus",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0",
    })


MANIFEST = {
    "name": "Tmux Bridge", "short_name": "Tmux Bridge",
    "start_url": "/", "display": "standalone", "orientation": "any",
    "background_color": "#2b2a27", "theme_color": "#2b2a27",
    "description": "Browser access to persistent tmux sessions",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
}


@app.get("/manifest.json")
async def manifest():
    return MANIFEST


@app.get("/icon.svg")
async def icon():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
      <rect width="192" height="192" rx="40" fill="#1a1915"/>
      <text x="96" y="128" text-anchor="middle" font-family="monospace"
            font-size="120" font-weight="bold" fill="#da7756">&gt;_</text>
    </svg>'''
    return HTMLResponse(content=svg, media_type="image/svg+xml")


# ── Layout (gateway-local) ──

@app.get("/api/layout")
async def get_layout():
    return JSONResponse(load_layout())


@app.put("/api/layout")
async def put_layout(request: Request):
    data = await request.json()
    save_layout(data)
    return {"ok": True}


# ── Gateway specs (this machine) ──

@app.get("/api/specs")
async def gateway_specs():
    from sysinfo import get_specs
    return get_specs()


# ── Server registry (live connection state) ──

@app.get("/api/servers")
async def list_servers():
    """Returns servers with live connection state."""
    registered = list_registered_servers()
    result = []
    for srv in registered:
        if srv.get("revoked"):
            continue
        name = srv["name"]
        conn = gw_manager.get_server(name)
        result.append({
            "name": name,
            "connected": conn is not None and conn.is_active,
            "connected_since": conn.connected_at if conn and conn.is_active else None,
            "sessions": conn.sessions if conn and conn.is_active else [],
        })
    return result


# ── Proxied API endpoints (RPC over server WebSocket) ──

async def _rpc_to_server(server_name: str, method: str, path: str,
                         body: dict | None = None) -> JSONResponse:
    conn = gw_manager.get_server(server_name)
    if not conn or not conn.is_active:
        return JSONResponse({"error": "server not connected"}, status_code=503)
    result = await conn.rpc(method, path, body)
    return JSONResponse(result["body"], status_code=result["status"])


@app.get("/api/{server}/sessions")
async def proxy_sessions(server: str):
    conn = gw_manager.get_server(server)
    if not conn or not conn.is_active:
        return JSONResponse({"error": "server not connected"}, status_code=503)
    result = await conn.rpc("GET", "/api/sessions")
    sessions = result.get("body", [])
    # Merge gateway viewer info
    if isinstance(sessions, list):
        for s in sessions:
            key = f"{server}/{s['name']}"
            ps = gw_manager.proxy_sessions.get(key)
            if ps and ps.viewers:
                s["viewers"] = ps.get_viewers_info()
    return JSONResponse(sessions)


# tmux started by a service may pass a minimal PATH to non-login commands.
# Load the user's login environment and keep a shell alive if Claude exits, so
# errors stay visible instead of leaving the browser reconnecting to a gone pane.
CLAUDE_SESSION_COMMAND = 'exec "${SHELL:-/bin/sh}" -lc ' + shlex.quote(
    'claude; bridge_status=$?; '
    'printf "\\nClaude exited (status %s). You can retry from this shell.\\n" "$bridge_status"; '
    'exec "${SHELL:-/bin/sh}" -l'
)


@app.post("/api/{server}/sessions/{name}")
async def proxy_create_session(server: str, name: str, request: Request = None):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    # Normalize here as well as serving the UI, so already-open clients benefit.
    if body.get("command") == "claude":
        body["command"] = CLAUDE_SESSION_COMMAND
    return await _rpc_to_server(server, "POST", f"/api/sessions/{name}", body)


@app.delete("/api/{server}/sessions/{name}")
async def proxy_kill_session(server: str, name: str):
    return await _rpc_to_server(server, "DELETE", f"/api/sessions/{name}")


@app.get("/api/{server}/specs")
async def proxy_specs(server: str):
    return await _rpc_to_server(server, "GET", "/api/specs")


@app.get("/api/{server}/files")
async def proxy_files(server: str, path: str = "~"):
    return await _rpc_to_server(server, "GET", "/api/files", {"path": path})


@app.get("/api/{server}/history/{session}")
async def proxy_history(server: str, session: str):
    return await _rpc_to_server(server, "GET", "/api/history", {"session": session})


@app.post("/api/{server}/upload")
async def proxy_upload(server: str, request: Request):
    body = await request.json()
    return await _rpc_to_server(server, "POST", "/api/upload", body)


@app.get("/api/{server}/metrics")
async def proxy_metrics(server: str):
    return await _rpc_to_server(server, "GET", "/api/metrics")


# ── Server reverse WebSocket ──

@app.websocket("/server-ws")
async def server_ws(websocket: WebSocket):
    """Accept inbound connection from a terminal server."""
    await websocket.accept()

    server_name = None
    try:
        # Wait for auth message
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != MSG_AUTH:
            await websocket.send_text(json.dumps({"type": "error", "msg": "Expected auth message"}))
            await websocket.close()
            return

        # Verify cert + signature
        try:
            server_name = verify_auth_proof(
                msg["cert"].encode(),
                msg["signature"],
                msg["timestamp"],
            )
        except ValueError as e:
            await websocket.send_text(json.dumps({"type": "error", "msg": str(e)}))
            await websocket.close()
            return

        await websocket.send_text(json.dumps({"type": "auth_ok", "name": server_name}))
        print(f"[GW] Server '{server_name}' authenticated", flush=True)

        # Wait for register message
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != MSG_REGISTER:
            await websocket.close()
            return

        # Create ServerConnection and register
        # Use the underlying websockets connection for the server side
        conn = ServerConnection(websocket, server_name)
        conn.sessions = msg.get("sessions", [])
        await gw_manager.register_server(server_name, conn)
        print(f"[GW] Server '{server_name}' registered with {len(conn.sessions)} sessions", flush=True)

        # Message loop — route incoming messages
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                # Binary frame: terminal data from server
                data = message["bytes"]
                if len(data) >= 2:
                    channel_id, payload = unpack_data(data)
                    # Find proxy session for this channel
                    for ps in gw_manager.proxy_sessions.values():
                        if (ps.server_conn is conn and ps.channel == channel_id):
                            ps.receive_data(payload)
                            break
            elif "text" in message and message["text"]:
                msg = json.loads(message["text"])
                msg_type = msg.get("type")

                if msg_type == MSG_RPC_RESPONSE:
                    conn.resolve_rpc(msg["id"], msg["status"], msg["body"])
                elif msg_type == MSG_SESSION_UPDATE:
                    conn.sessions = msg.get("sessions", [])
                elif msg_type == MSG_CHANNEL_CLOSED:
                    for ps in list(gw_manager.proxy_sessions.values()):
                        if ps.server_conn is conn and ps.channel == msg.get("channel"):
                            await ps.terminal_ended(msg.get("reason", "Terminal attachment ended."))
                            break
                elif msg_type == MSG_PONG:
                    pass
                elif msg_type == MSG_PING:
                    await websocket.send_text(json.dumps({"type": "pong"}))

    except (WebSocketDisconnect, Exception) as e:
        pass
    finally:
        if server_name:
            # Only unregister if this connection is still the active one
            current = gw_manager.server_connections.get(server_name)
            if current and current.ws is websocket:
                await gw_manager.unregister_server(server_name)
                print(f"[GW] Server '{server_name}' disconnected", flush=True)


# ── Enrollment endpoint ──

@app.post("/api/enroll")
async def enroll_credentials(request: Request):
    # Pairing returns data only; clients install code from their own checkout.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024:
            return JSONResponse({"error": "pairing request too large"}, status_code=413)
    try:
        body = json.loads(raw)
        token = body.get("token") if isinstance(body, dict) else None
    except (ValueError, UnicodeDecodeError):
        token = None
    name = consume_enrollment_token(token)
    if not name:
        return JSONResponse({"error": "Pairing code is invalid, expired, or already used."}, status_code=403)
    key, cert, ca = issue_server_cert(name)
    return JSONResponse({"name": name, "key": key.decode(), "cert": cert.decode(), "ca": ca.decode()},
                        headers={"Cache-Control": "no-store"})


# ── Admin endpoints (used by CLI) ──

@app.post("/api/admin/remove")
async def admin_remove(request: Request):
    data = await request.json()
    name = data.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    await gw_manager.unregister_server(name)
    return {"ok": True}


# ── Gateway status ──

@app.get("/api/gateway/status")
async def gateway_status():
    config = cloudflare.load_config()
    return {
        "configured": ca_exists(),
        "cf_configured": bool(config.get("cf_api_token")),
        "domain": config.get("domain", ""),
        "tunnel_running": cloudflare.is_tunnel_running(),
        "server_connections": len(gw_manager.server_connections),
    }


# ── Local gateway shell WebSocket ──

@app.websocket("/ws/local-shell")
async def local_shell_ws(websocket: WebSocket):
    await websocket.accept()
    cols = int(websocket.query_params.get("cols", 120))
    rows = int(websocket.query_params.get("rows", 40))

    pid, fd = pty.openpty()
    child_pid = os.fork()
    if child_pid == 0:
        os.setsid()
        os.close(pid)
        slave_fd = fd
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.environ["TERM"] = "xterm-256color"
        shell = os.environ.get("SHELL", "/bin/bash")
        os.execvp(shell, [shell, "--login"])

    os.close(fd)
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(pid, termios.TIOCSWINSZ, winsize)

    loop = asyncio.get_event_loop()

    async def read_pty():
        try:
            while True:
                data = await loop.run_in_executor(None, lambda: os.read(pid, 4096))
                if not data:
                    break
                await websocket.send_bytes(data)
        except (OSError, WebSocketDisconnect, Exception):
            pass

    reader_task = asyncio.create_task(read_pty())

    try:
        while True:
            msg = await websocket.receive()
            if "text" in msg:
                text = msg["text"]
                if text.startswith("{"):
                    try:
                        data = json.loads(text)
                        if data.get("type") == "resize":
                            winsize = struct.pack("HHHH", data["rows"], data["cols"], 0, 0)
                            fcntl.ioctl(pid, termios.TIOCSWINSZ, winsize)
                            os.kill(child_pid, signal.SIGWINCH)
                            continue
                        elif data.get("type") == "ping":
                            await websocket.send_text(json.dumps({"type": "pong"}))
                            continue
                    except (json.JSONDecodeError, OSError):
                        pass
                os.write(pid, text.encode())
            elif "bytes" in msg:
                os.write(pid, msg["bytes"])
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        reader_task.cancel()
        try:
            os.kill(child_pid, signal.SIGHUP)
            os.waitpid(child_pid, 0)
        except OSError:
            pass
        try:
            os.close(pid)
        except OSError:
            pass


# ── Proxied terminal WebSocket ──

@app.websocket("/ws/{server}/{session}")
async def proxy_terminal_ws(websocket: WebSocket, server: str, session: str):
    await websocket.accept()

    cols = int(websocket.query_params.get("cols", 120))
    rows = int(websocket.query_params.get("rows", 40))
    device = websocket.query_params.get("device", "Unknown")
    client_id = websocket.query_params.get("client_id", "")

    proxy = await gw_manager.get_proxy(server, session)
    if not proxy:
        await websocket.send_text(json.dumps({
            "type": "error", "msg": f"Server '{server}' not connected."
        }))
        await websocket.close()
        return

    viewer_id = client_id or hashlib.md5(
        f"{websocket.client.host}:{websocket.client.port}:{time.time()}".encode()
    ).hexdigest()[:8]

    info = ViewerInfo(
        ws=websocket, viewer_id=viewer_id, device=device, label=device,
        session_name=session, cols=cols, rows=rows,
        ui_version=websocket.query_params.get("ui_version", "")[:80],
    )

    replay = await proxy.add_viewer(websocket, info)
    if replay:
        await websocket.send_bytes(replay)
    info.ready = True
    await gw_manager.broadcast_presence()

    try:
        while True:
            msg = await websocket.receive()
            if "text" in msg:
                text = msg["text"]
                if text.startswith("{"):
                    try:
                        data = json.loads(text)
                        if data.get("type") == "resize":
                            info.cols = data["cols"]
                            info.rows = data["rows"]
                            proxy._resize_to_active()
                            continue
                        elif data.get("type") == "take_input":
                            await proxy.set_active(websocket)
                            continue
                        elif data.get("type") == "release_input":
                            await proxy.release_active(websocket)
                            continue
                        elif data.get("type") == "ping":
                            await websocket.send_text(json.dumps({"type": "pong"}))
                            continue
                        elif data.get("type") == "return_live":
                            if proxy.active_input == websocket:
                                result = await proxy.server_conn.rpc(
                                    "POST", "/api/history/live", {"session": session, "pane": data.get("pane")}
                                )
                                await websocket.send_text(json.dumps({
                                    "type": "return_live_result",
                                    "ok": result.get("status") == 200,
                                }))
                            continue
                        elif data.get("type") == "scroll":
                            info.scroll_events += 1
                            if proxy.active_input != websocket:
                                await websocket.send_text(json.dumps({"type": "scroll_result", "ok": False, "error": "Take control to scroll the live pane"}))
                                continue
                            result = await proxy.server_conn.rpc("POST", "/api/scroll", {
                                "session": session, "lines": data.get("lines"),
                                "col": data.get("col"), "row": data.get("row"),
                            })
                            info.last_scroll_ok = result.get("status") == 200
                            await websocket.send_text(json.dumps({
                                **result.get("body", {}), "type": "scroll_result",
                                "ok": result.get("status") == 200,
                            }))
                            continue
                    except json.JSONDecodeError:
                        pass
                await proxy.write_input(websocket, text.encode())
            elif "bytes" in msg:
                await proxy.write_input(websocket, msg["bytes"])
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await proxy.remove_viewer(websocket)


@app.get("/health")
async def health():
    total_viewers = sum(len(ps.viewers) for ps in gw_manager.proxy_sessions.values())
    return {
        "status": "ok",
        "server_connections": len(gw_manager.server_connections),
        "proxy_sessions": len(gw_manager.proxy_sessions),
        "viewers": total_viewers,
    }


# ── HTML/CSS/JS Template ──

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#2b2a27">
<meta name="mobile-web-app-capable" content="yes">
<title>Tmux Bridge</title>
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css" integrity="sha384-tStR1zLfWgsiXCF3IgfB3lBa8KmBe/lG287CL9WCeKgQYcp1bjb4/+mwN6oti4Co" crossorigin="anonymous">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { background: #1a1915; }
  body {
    background: #2b2a27; color: #e8e4db;
    font-family: 'Styrene A', 'Sohne', -apple-system, 'Helvetica Neue', Helvetica, Arial, sans-serif;
    height: 100dvh; display: flex; overflow: hidden;
    overscroll-behavior: none;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
    -webkit-user-select: none; user-select: none;
  }

  /* Server strip */
  #server-strip {
    width: 72px; background: #1a1915;
    display: flex; flex-direction: column; align-items: center;
    padding: 12px 0; gap: 8px; flex-shrink: 0;
  }
  .server-icon {
    width: 48px; height: 48px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; font-weight: 700; cursor: pointer;
    transition: border-radius 0.2s, background 0.2s; position: relative;
  }
  .server-icon:hover, .server-icon.active { border-radius: 16px; }
  .server-icon.active::before {
    content: ''; position: absolute; left: -16px;
    width: 4px; height: 40px; background: #e8e4db; border-radius: 0 4px 4px 0;
  }
  #home-server { background: #2b2a27; color: #da7756; font-size: 24px; margin-bottom: 4px; }
  #home-server:hover, #home-server.active { background: #da7756; color: #fff; }
  .server-divider { width: 32px; height: 2px; background: #3d3929; border-radius: 1px; }
  .machine-server { background: #3d3929; color: #e8e4db; }
  .machine-server:hover { background: #da7756; color: #fff; }
  .machine-server.active { background: #da7756; color: #fff; }
  .machine-server.disconnected { opacity: 0.4; cursor: not-allowed; }
  .machine-server.disconnected:hover { background: #3d3929; color: #e8e4db; border-radius: 50%; }
  .machine-server .srv-offline {
    position: absolute; bottom: -2px; right: -2px;
    width: 12px; height: 12px; border-radius: 50%;
    background: #c94a3e; border: 2px solid #1a1915;
  }

  /* Sidebar */
  #sidebar {
    width: 240px; background: #23221e;
    display: flex; flex-direction: column; flex-shrink: 0;
  }
  #sidebar-header {
    padding: 0 16px; font-size: 16px; font-weight: 700;
    color: #e8e4db; height: 48px; display: flex;
    align-items: center; border-bottom: 1px solid #1a1915;
    justify-content: space-between; gap: 8px;
  }
  #mobile-server-select {
    display: none; background: #1a1915; color: #da7756; border: 1px solid #3d3929;
    border-radius: 6px; padding: 4px 8px; font-size: 11px; font-weight: 700;
    font-family: inherit; outline: none; max-width: 120px;
    -webkit-appearance: none; appearance: none;
  }
  .category-label {
    padding: 18px 8px 4px 8px; font-size: 11px; font-weight: 700;
    color: #8b8579; text-transform: uppercase; letter-spacing: 0.05em;
    display: flex; align-items: center;
  }
  .category-label .cat-text { flex: 1; }
  .category-label .cat-add {
    display: none; background: none; border: none; color: #8b8579;
    cursor: pointer; padding: 0; line-height: 1;
    width: 16px; height: 16px; align-items: center; justify-content: center;
  }
  .category-label:hover .cat-add { display: flex; }
  .category-label .cat-add:hover { color: #e8e4db; }
  .category-label .cat-add svg { width: 14px; height: 14px; }
  #welcome-item { margin: 1px 8px; }
  #session-list { flex: 1; overflow-y: auto; padding: 0 8px; }

  /* Session channel item */
  .session-item {
    display: flex; align-items: center; padding: 5px 8px; margin: 1px 0;
    border-radius: 4px; cursor: pointer; color: #8b8579; font-size: 14px;
    gap: 6px; transition: background 0.1s, color 0.1s; position: relative;
  }
  .session-item:hover { background: #322f2a; color: #e8e4db; }
  .session-item.active { background: #3d3929; color: #e8e4db; }
  .session-item .hash { font-size: 16px; font-weight: 500; opacity: 0.5; }
  .session-item .s-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .session-item .s-actions {
    display: none; margin-left: auto; gap: 2px; flex-shrink: 0; align-items: center;
  }
  .session-item:hover .s-actions { display: flex; }
  .session-item .s-btn {
    background: none; border: none; color: #8b8579;
    cursor: pointer; padding: 0 2px; line-height: 1;
    display: flex; align-items: center; justify-content: center;
    width: 18px; height: 18px;
  }
  .session-item .s-btn svg { width: 14px; height: 14px; }
  .session-item .s-btn:hover { color: #e8e4db; }
  .session-item .s-btn.s-close:hover { color: #c94a3e; }
  [draggable="true"] { cursor: grab; }
  .dragging { opacity: 0.4; }
  .drop-target { outline: 2px dashed #da7756; outline-offset: -2px; border-radius: 4px; }
  .session-item, .project-group { position: relative; }
  .drop-above::before, .drop-below::after {
    content: ''; position: absolute; left: 8px; right: 8px;
    height: 2px; background: #da7756; border-radius: 1px;
    pointer-events: none; z-index: 2;
  }
  .drop-above::before { top: -1px; }
  .drop-below::after { bottom: -1px; }

  /* Directories */
  .project-header {
    display: flex; align-items: center; padding: 6px 4px 2px;
    cursor: pointer; color: #8b8579; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.05em;
    gap: 2px; transition: color 0.1s; margin-top: 8px;
  }
  .project-header:hover { color: #e8e4db; }
  .project-header .arrow {
    font-size: 7px; width: 10px; text-align: center;
    transition: transform 0.15s;
  }
  .project-header.collapsed .arrow { transform: rotate(-90deg); }
  .project-header { position: relative; }
  .project-header .p-name { flex: 1; }
  .project-header .p-actions {
    display: none; gap: 2px; align-items: center;
  }
  .project-header:hover .p-actions { display: flex; }
  .project-header .p-btn {
    background: none; border: none; color: #8b8579;
    cursor: pointer; padding: 0 2px; line-height: 1;
    display: flex; align-items: center; justify-content: center;
    width: 16px; height: 16px;
  }
  .project-header .p-btn svg { width: 12px; height: 12px; }
  .project-header .p-btn:hover { color: #e8e4db; }
  .project-header .p-btn.p-del:hover { color: #c94a3e; }
  .project-group { border-radius: 4px; }
  .project-group.dragging { opacity: 0.4; }
  .project-group.drop-target { outline: 2px dashed #da7756; outline-offset: -2px; }
  .project-group > .session-item { padding-left: 18px; }
  .project-group > .viewer-list { padding-left: 10px; }
  .project-group.collapsed > .session-item,
  .project-group.collapsed > .viewer-list { display: none; }

  /* Viewer entries */
  .viewer-list { padding: 0 0 2px 0; }
  .viewer-entry {
    display: flex; align-items: center; gap: 6px;
    padding: 1px 8px 1px 30px; font-size: 12px;
    color: #6b6560; line-height: 18px;
  }
  .viewer-entry .v-dot {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    background: #c96442;
  }
  .viewer-entry.has-control .v-dot { background: #5ba97b; }
  .viewer-entry.loading .v-dot {
    background: transparent;
    border: 2px solid #5ba97b;
    border-top-color: transparent;
    animation: spin 0.8s linear infinite;
    width: 7px; height: 7px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .viewer-entry .v-label {
    flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .viewer-entry.has-control .v-label { color: #a8a194; }
  .typing-dots {
    display: inline-flex; gap: 2px; margin-left: 2px; align-items: center;
  }
  .typing-dots span {
    width: 3.5px; height: 3.5px; border-radius: 50%; background: #5ba97b;
    animation: bounce 1.4s infinite ease-in-out both;
  }
  .typing-dots span:nth-child(1) { animation-delay: -0.32s; }
  .typing-dots span:nth-child(2) { animation-delay: -0.16s; }
  .typing-dots span:nth-child(3) { animation-delay: 0s; }
  @keyframes bounce {
    0%, 80%, 100% { transform: scale(0); opacity: 0.4; }
    40% { transform: scale(1); opacity: 1; }
  }

  /* Bottom panel */
  #bottom-panel {
    margin: 0 8px 8px; flex-shrink: 0; background: #1a1915;
    border-radius: 8px; overflow: hidden;
  }
  #conn-header {
    display: none; padding: 8px 8px 0; cursor: default;
  }
  #conn-header.visible { display: block; }
  #conn-header-row {
    display: flex; align-items: center; gap: 6px;
  }
  #conn-header-info { flex: 1; min-width: 0; }
  #conn-header-title {
    font-size: 12px; font-weight: 600; color: #5ba97b;
    display: flex; align-items: center; gap: 5px;
  }
  #conn-header-title .signal-icon { display: flex; align-items: flex-end; gap: 1px; height: 10px; }
  #conn-header-title .signal-icon span {
    width: 2.5px; background: #5ba97b; border-radius: 1px;
  }
  #conn-header-title .signal-icon span:nth-child(1) { height: 3px; }
  #conn-header-title .signal-icon span:nth-child(2) { height: 6px; }
  #conn-header-title .signal-icon span:nth-child(3) { height: 9px; }
  #conn-header-sub {
    font-size: 11px; color: #6b6560; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; margin-top: 1px;
  }
  #conn-claim {
    width: 28px; height: 28px; border-radius: 4px; border: none;
    background: none; cursor: pointer; display: flex;
    align-items: center; justify-content: center;
    color: #8b8579; transition: background 0.1s, color 0.1s;
    flex-shrink: 0;
  }
  #conn-claim:hover { background: #5ba97b33; color: #5ba97b; }
  #conn-claim.has-control { color: #5ba97b; }
  #conn-claim.has-control:hover { background: #da775633; color: #da7756; }
  #conn-claim svg { width: 16px; height: 16px; }
  #conn-disconnect {
    width: 28px; height: 28px; border-radius: 4px; border: none;
    background: none; cursor: pointer; display: flex;
    align-items: center; justify-content: center;
    color: #8b8579; transition: background 0.1s, color 0.1s;
    flex-shrink: 0;
  }
  #conn-disconnect:hover { background: #c94a3e33; color: #c94a3e; }
  #conn-disconnect svg { width: 18px; height: 18px; }
  #conn-divider {
    display: none; height: 1px; background: #2b2a27; margin: 6px 8px 0;
  }
  #conn-divider.visible { display: block; }
  #user-row {
    display: flex; align-items: center; gap: 8px;
    padding: 8px;
  }
  .avatar {
    width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700; flex-shrink: 0;
    background: #da7756; color: #fff; position: relative;
  }
  .avatar .status-pip {
    position: absolute; bottom: -1px; right: -1px;
    width: 10px; height: 10px; border-radius: 50%;
    border: 2.5px solid #1a1915; background: #6b6560;
  }
  .avatar .status-pip.online { background: #5ba97b; }
  .user-name-block { flex: 1; min-width: 0; }
  .username { font-size: 13px; font-weight: 600; color: #e8e4db; }
  .user-sub { font-size: 11px; color: #6b6560; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .user-btns { display: flex; gap: 2px; flex-shrink: 0; }

  /* Main area */
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #content-wrap { flex: 1; display: flex; min-height: 0; position: relative; }
  #content-area { flex: 1; display: flex; flex-direction: column; min-width: 0; }

  /* Right panel */
  #right-panel {
    position: absolute; right: 0; top: 0; bottom: 0;
    width: 240px; background: #23221e;
    overflow-y: auto; display: flex; flex-direction: column;
    border-left: 1px solid #1a1915;
    transition: transform 0.15s, opacity 0.15s;
    z-index: 10;
  }
  #right-panel.collapsed { transform: translateX(100%); opacity: 0; pointer-events: none; border: none; }
  #right-panel-header {
    padding: 12px 12px 4px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; color: #6b6560; font-weight: 700;
  }
  #rp-tabs {
    display: flex; border-bottom: 1px solid #3d3929; padding: 0 8px; flex-shrink: 0;
  }
  .rp-tab {
    flex: 1; padding: 8px 0; background: none; border: none; border-bottom: 2px solid transparent;
    color: #6b6560; font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.05em; cursor: pointer; text-align: center;
    -webkit-tap-highlight-color: transparent;
  }
  .rp-tab.active { color: #da7756; border-bottom-color: #da7756; }
  #rp-files { flex: 1; overflow-y: auto; }
  #file-path-bar {
    padding: 8px 12px; font-size: 11px; color: #8b8579; font-family: monospace;
    border-bottom: 1px solid #3d392944; display: flex; align-items: center; gap: 6px;
  }
  #file-path-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #file-list { padding: 4px 0; }
  .file-item {
    display: flex; align-items: center; gap: 8px; padding: 6px 12px;
    font-size: 12px; color: #e8e4db; cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .file-item:active { background: #da775611; }
  .file-item .fi-icon { width: 16px; text-align: center; font-size: 13px; flex-shrink: 0; }
  .file-item .fi-icon.dir { color: #d4a04a; }
  .file-item .fi-icon.file { color: #6b6560; }
  .file-item .fi-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-item .fi-size { font-size: 10px; color: #6b6560; }
  .file-item-back { color: #8b8579; font-style: italic; }
  #right-panel .rp-section {
    font-size: 9px; text-transform: uppercase; letter-spacing: 0.08em;
    color: #6b6560; font-weight: 700; padding: 10px 12px 4px;
  }
  #right-panel .rp-graph { padding: 6px 12px; }
  #right-panel .rp-graph .rp-row {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 3px;
  }
  #right-panel .rp-label {
    font-size: 10px; color: #8b8579; font-weight: 600;
    display: flex; align-items: center; gap: 4px;
  }
  #right-panel .rp-label svg { width: 11px; height: 11px; color: #da7756; }
  #right-panel .rp-val {
    font-size: 10px; color: #e8e4db; font-family: monospace; font-weight: 600;
  }
  #right-panel .rp-canvas { width: 100%; height: 32px; display: block; }
  #right-panel .rp-legend {
    display: flex; gap: 8px; justify-content: flex-end;
    font-size: 8px; color: #6b6560; margin-top: 2px;
  }
  #right-panel .rp-legend span::before {
    content: ''; display: inline-block; width: 6px; height: 2px;
    border-radius: 1px; margin-right: 2px; vertical-align: middle;
  }
  #right-panel .rp-legend .leg-primary::before { background: #da7756; }
  #right-panel .rp-legend .leg-secondary::before { background: #5ba97b; }
  #right-panel .rp-divider { height: 1px; background: #2b2a27; margin: 4px 12px; }
  /* Gauges */
  .gauge-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
    padding: 8px 10px 4px;
  }
  .gauge-card {
    background: #1a1915; border-radius: 8px; padding: 10px;
    display: flex; flex-direction: column; align-items: center; gap: 6px;
  }
  .gauge-ring {
    position: relative; width: 52px; height: 52px;
  }
  .gauge-ring svg { width: 52px; height: 52px; transform: rotate(-90deg); }
  .gauge-ring .gauge-bg {
    fill: none; stroke: #2b2a27; stroke-width: 5;
  }
  .gauge-ring .gauge-fg {
    fill: none; stroke-width: 5; stroke-linecap: round;
    transition: stroke-dashoffset 0.6s ease, stroke 0.3s;
  }
  .gauge-pct {
    position: absolute; inset: 0; display: flex;
    align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; color: #e8e4db;
    font-family: monospace;
  }
  .gauge-label {
    font-size: 9px; font-weight: 700; color: #8b8579;
    text-transform: uppercase; letter-spacing: 0.05em;
    text-align: center; line-height: 1.2;
  }
  .gauge-sub {
    font-size: 8px; color: #6b6560; font-family: monospace;
    text-align: center;
  }

  .rp-accordion {
    display: flex; align-items: center; gap: 4px;
    padding: 10px 12px 4px; cursor: pointer;
    font-size: 9px; text-transform: uppercase; letter-spacing: 0.08em;
    color: #6b6560; font-weight: 700; user-select: none;
  }
  .rp-accordion:hover { color: #8b8579; }
  .rp-accordion .rp-arrow { font-size: 7px; transition: transform 0.15s; }
  .rp-accordion.collapsed .rp-arrow { transform: rotate(-90deg); }
  .rp-accordion-body { overflow: hidden; }
  .rp-accordion.collapsed + .rp-accordion-body { display: none; }
  #rpanel-toggle, #topbar-reload {
    background: none; border: none; color: #8b8579; cursor: pointer;
    padding: 4px; display: flex; align-items: center;
    -webkit-tap-highlight-color: transparent;
  }
  #rpanel-toggle:hover, #topbar-reload:hover { color: #e8e4db; }
  #topbar-reload:active { color: #da7756; }
  #topbar {
    display: flex; align-items: center; padding: 0 16px;
    background: #2b2a27; border-bottom: 1px solid #3d3929;
    height: 48px; flex-shrink: 0; gap: 8px;
  }
  .channel-name {
    font-weight: 700; font-size: 16px; color: #e8e4db;
    display: flex; align-items: center; gap: 4px; min-width:0;
  }
  #chan-title { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .channel-name .hash { color: #8b8579; font-size: 20px; }
  .topbar-divider { width: 1px; height: 24px; background: #3d3929; margin: 0 4px; }
  .status-text { font-size: 14px; color: #8b8579; display: flex; align-items: center; gap: 6px; }
  .conn-dot { width: 10px; height: 10px; border-radius: 50%; background: #6b6560; }
  .conn-dot.connected { background: #5ba97b; }
  .conn-dot.disconnected { background: #c94a3e; }
  .topbar-stats {
    margin-left: auto; font-size: 11px; color: #6b6560;
    font-family: monospace; white-space: nowrap;
  }
  #hamburger {
    display: block; background: none; border: none; color: #a8a194;
    font-size: 24px; cursor: pointer; padding: 4px;
    -webkit-tap-highlight-color: transparent;
  }
  #sidebar.desktop-collapsed { width: 0; min-width: 0; overflow: hidden; border: none; padding: 0; }
  #sidebar.desktop-collapsed ~ #main { margin-left: 0; }
  #terminal-container { flex: 1; overflow: hidden; background: #1a1915; position: relative; }
  #terminal-container.hidden { display: none; }
  #jump-bottom {
    display: none; position: fixed; bottom: 96px; right: 8px; z-index: 51;
    width: 36px; height: 36px; border-radius: 50%; border: 1px solid #3d392966;
    background: rgba(26,25,21,0.85); color: #da7756; font-size: 16px;
    cursor: pointer; align-items: center; justify-content: center;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    -webkit-tap-highlight-color: transparent;
  }
  #jump-bottom.visible { display: flex; }
  .mob-bksp { color: #c94a3e !important; border-color: #c94a3e44 !important; }
  .mob-bksp:active { background: #c94a3e33 !important; }
  .mob-bksp svg { fill: none; stroke: currentColor; }
  .mob-esc-inline { color: #da7756 !important; border-color: #da775644 !important; }
  .mob-esc-overflow { color: #da7756 !important; border-color: #da775644 !important; display: none; }
  .mob-row-esc { display: none; }
  #mob-esc {
    display: none; position: fixed; bottom: 46px; left: 4px; z-index: 51;
    height: 32px; min-width: 32px; padding: 0 8px; border-radius: 8px;
    border: 1px solid #da775644;
    background: rgba(26,25,21,0.85); color: #da7756; font-size: 10px;
    font-weight: 700; font-family: -apple-system, sans-serif;
    cursor: pointer; align-items: center; justify-content: center;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    -webkit-tap-highlight-color: transparent;
    transition: background 0.1s, transform 0.1s;
  }
  #mob-esc:active { background: #da775633; transform: scale(0.92); }
  #mob-esc.visible { display: none; }
  #mobile-controls {
    display: none; position: fixed; bottom: env(safe-area-inset-bottom, 6px); left: 4px; right: 8px; z-index: 50;
    padding: 0;
    flex-direction: column; align-items: stretch; gap: 4px;
    box-sizing: border-box;
  }
  #mobile-controls.visible { display: flex; }
  .mob-row { display: flex; gap: 4px; align-items: flex-end; box-sizing: border-box; }
  .mob-btn {
    height: 36px; min-width: 36px; border-radius: 8px; border: 1px solid #3d392966;
    background: rgba(26,25,21,0.85); color: #8b8579; font-size: 10px;
    font-weight: 700; font-family: -apple-system, sans-serif; letter-spacing: 0.3px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    -webkit-tap-highlight-color: transparent; padding: 0 6px;
    transition: background 0.1s, transform 0.1s; flex-shrink: 0;
  }
  .mob-btn:active { background: #da775633; color: #da7756; transform: scale(0.92); }
  .mob-btn svg { width: 16px; height: 16px; stroke: currentColor; fill: none; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }
  .mob-spacer { flex: 1; min-width: 4px; }
  .mob-arrow { color: #da7756; min-width: 40px; padding: 0; }
  .arrow-cluster {
    display: grid; grid-template-columns: 40px 40px 40px; grid-template-rows: 36px 36px;
    gap: 3px; flex-shrink: 0;
  }
  .arrow-cluster .mob-btn:nth-child(1) { grid-column: 2; grid-row: 1; }
  .arrow-cluster .mob-btn:nth-child(2) { grid-column: 1; grid-row: 2; }
  .arrow-cluster .mob-btn:nth-child(3) { grid-column: 2; grid-row: 2; }
  .arrow-cluster .mob-btn:nth-child(4) { grid-column: 3; grid-row: 2; }
  .mob-enter {
    background: rgba(91,169,123,0.15) !important; color: #5ba97b !important;
    border-color: #5ba97b44 !important; width: 44px !important; height: 75px !important;
    border-radius: 10px !important; flex-shrink: 0; min-width: unset !important;
  }
  .mob-enter svg { width: 22px !important; height: 22px !important; }
  .mob-enter:active { background: rgba(91,169,123,0.35) !important; }
  @media (max-width: 374px) {
    .mob-btn { height: 32px; min-width: 32px; font-size: 9px; padding: 0 4px; border-radius: 6px; }
    .mob-btn svg { width: 14px; height: 14px; }
    .mob-arrow { min-width: 32px; }
    .arrow-cluster { grid-template-columns: 32px 32px 32px; grid-template-rows: 32px 32px; gap: 2px; }
    .mob-enter { width: 36px !important; min-width: unset !important; height: 67px !important; }
    .mob-enter svg { width: 18px !important; height: 18px !important; }
    .mob-row { gap: 3px; }
    #mobile-controls { bottom: 4px; left: 2px; right: 8px; gap: 3px; }
    .mob-esc-inline { display: none !important; }
    .mob-row-esc { display: flex !important; }
    .mob-esc-overflow { display: flex !important; }
  }
  .mob-row-top { justify-content: flex-start; }
  .mob-row-bottom { justify-content: flex-end; align-items: flex-end; }
  #mobile-controls.kbd-mode { padding: 0 4px; gap: 2px; }
  #mobile-controls.kbd-mode .mob-btn { height: 30px; min-width: 30px; border-radius: 6px; font-size: 9px; padding: 0 4px; }
  #mobile-controls.kbd-mode .mob-btn svg { width: 13px; height: 13px; }
  #mobile-controls.kbd-mode .arrow-cluster { grid-template-columns: 30px 30px 30px; grid-template-rows: 30px 30px; gap: 2px; }
  #mobile-controls.kbd-mode .mob-enter { min-width: 38px !important; height: 63px !important; }
  #mobile-controls.kbd-mode .mob-enter svg { width: 16px !important; height: 16px !important; }
  #paste-modal {
    display: none; position: fixed; inset: 0; z-index: 200;
    background: rgba(0,0,0,0.7); align-items: center; justify-content: center;
  }
  #paste-modal.visible { display: flex; }
  #paste-modal-inner {
    background: #23221e; border: 1px solid #3d3929; border-radius: 12px;
    padding: 16px; width: 85%; max-width: 340px;
  }
  #paste-modal-inner label { color: #8b8579; font-size: 12px; display: block; margin-bottom: 6px; }
  #paste-area {
    width: 100%; min-height: 80px; background: #1a1915; border: 1px solid #3d3929;
    border-radius: 6px; color: #e8e4db; font-family: 'DejaVu Sans Mono', monospace;
    font-size: 14px; padding: 10px; outline: none; resize: none;
    -webkit-user-select: auto; user-select: auto;
  }
  #paste-area:focus { border-color: #da7756; }
  #paste-modal-btns { display: flex; gap: 8px; margin-top: 10px; justify-content: flex-end; }
  #paste-modal-btns button {
    border: none; border-radius: 6px; padding: 8px 16px; font-size: 13px;
    font-weight: 600; cursor: pointer; -webkit-tap-highlight-color: transparent;
  }
  #paste-cancel { background: #3d3929; color: #e8e4db; }
  #paste-send { background: #da7756; color: #1a1915; }
  .xterm { height: 100%; }
  .xterm-viewport { overflow-y: auto !important; overscroll-behavior: contain; }
  .xterm-viewport::-webkit-scrollbar { display: none; }

  /* Server landing */
  #server-landing {
    display: none; flex: 1; overflow-y: auto; background: #1a1915;
    flex-direction: column; align-items: center; justify-content: center;
    padding: 40px 24px; text-align: center;
  }
  #server-landing.visible { display: flex; }
  #server-landing .landing-icon {
    width: 64px; height: 64px; border-radius: 50%; background: #2b2a27;
    display: flex; align-items: center; justify-content: center;
    margin-bottom: 16px; color: #da7756; font-size: 28px; font-weight: 700;
  }
  #server-landing h2 { color: #e8e4db; font-size: 20px; font-weight: 700; margin-bottom: 6px; }
  #server-landing p { color: #6b6560; font-size: 13px; max-width: 260px; line-height: 1.5; }
  #landing-session-count { color: #8b8579; font-size: 12px; margin-top: 16px; }
  #specs-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
    margin-top: 20px; width: 100%; max-width: 400px; text-align: left;
  }
  .spec-section {
    grid-column: 1 / -1; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; color: #6b6560; font-weight: 700;
    margin-top: 10px; padding: 0 2px;
  }
  .spec-section:first-child { margin-top: 0; }
  .spec-card {
    background: #2b2a27; border-radius: 6px; padding: 10px 12px;
  }
  .spec-card .spec-label {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
    color: #6b6560; margin-bottom: 3px; font-weight: 600;
    display: flex; align-items: center; gap: 5px;
  }
  .spec-card .spec-label svg {
    width: 12px; height: 12px; color: #da7756; flex-shrink: 0;
  }
  .spec-card .spec-value {
    font-size: 13px; color: #e8e4db; font-weight: 600;
    font-family: monospace; word-break: break-word;
  }
  .spec-card .spec-sub {
    font-size: 11px; color: #8b8579; margin-top: 2px;
    font-family: monospace;
  }
  .spec-card.wide { grid-column: 1 / -1; }

  /* Home view */
  #home-view {
    display: none; flex: 1; overflow-y: auto;
    background: #1a1915;
    flex-direction: column;
  }
  #home-view.visible { display: flex; }


  /* Tab panels */
  .home-panel {
    display: none; flex: 1; overflow-y: auto;
    padding: 40px 24px;
    flex-direction: column; align-items: center;
  }
  .home-panel.active { display: flex; }
  /* Panels with sub-tabs need no padding, no scroll — children scroll */
  .home-panel.has-chtabs {
    padding: 0; overflow: hidden;
  }

  /* Welcome tab */
  #tab-welcome h1 { color: #da7756; font-size: 24px; font-weight: 700; margin-bottom: 8px; }
  #tab-welcome h2 { color: #e8e4db; font-size: 16px; font-weight: 600; margin: 24px 0 8px; }
  #tab-welcome p { color: #8b8579; font-size: 13px; line-height: 1.6; max-width: 460px; text-align: center; }
  #tab-welcome .welcome-features {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
    margin-top: 20px; max-width: 460px; width: 100%;
  }
  .welcome-feat {
    background: #2b2a27; border-radius: 8px; padding: 14px 16px;
  }
  .welcome-feat .wf-title {
    font-size: 12px; font-weight: 700; color: #e8e4db; margin-bottom: 4px;
    display: flex; align-items: center; gap: 6px;
  }
  .welcome-feat .wf-title svg { width: 14px; height: 14px; color: #da7756; }
  .welcome-feat .wf-desc { font-size: 11px; color: #6b6560; line-height: 1.4; }
  .welcome-cta {
    margin-top: 28px; display: flex; gap: 10px;
  }
  .welcome-btn {
    padding: 10px 24px; border-radius: 8px; border: none; cursor: pointer;
    font-size: 13px; font-weight: 600; font-family: inherit;
    transition: background 0.15s;
  }
  .welcome-btn.primary { background: #da7756; color: #fff; }
  .welcome-btn.primary:hover { background: #c96842; }
  .welcome-btn.secondary { background: #2b2a27; color: #8b8579; border: 1px solid #3d3929; }
  .welcome-btn.secondary:hover { background: #322f2a; color: #e8e4db; }

  /* Dashboard tab */
  #tab-dashboard h2 { color: #e8e4db; font-size: 18px; font-weight: 700; margin-bottom: 16px; }
  .server-cards { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; max-width: 500px; width: 100%; }
  .server-card {
    background: #2b2a27; border: 1px solid #3d3929; border-radius: 10px;
    padding: 16px 20px; cursor: pointer; flex: 1; min-width: 140px;
    transition: border-color 0.15s, background 0.15s;
    display: flex; flex-direction: column; gap: 6px;
  }
  .server-card:hover { border-color: #da7756; background: #322f2a; }
  .server-card.current { border-color: #5ba97b; }
  .server-card .sc-name { font-size: 15px; font-weight: 600; color: #e8e4db; }
  .server-card .sc-url { font-size: 11px; color: #6b6560; word-break: break-all; }
  .server-card .sc-status { font-size: 11px; }
  .server-card .sc-status.online { color: #5ba97b; }
  .server-card .sc-status.offline { color: #c94a3e; }
  .home-add-btn {
    background: #2b2a27; border: 2px dashed #3d3929; border-radius: 10px;
    padding: 16px 20px; cursor: pointer; flex: 1; min-width: 140px;
    display: flex; align-items: center; justify-content: center;
    color: #5ba97b; font-size: 15px; font-weight: 600;
    transition: border-color 0.15s, background 0.15s;
  }
  .home-add-btn:hover { border-color: #5ba97b; background: #5ba97b22; }

  /* Specs tab */
  .specs-grid-home {
    display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
    width: 100%; max-width: 400px; text-align: left;
  }

  /* Channel sub-tabs (used inside home panels) */
  .ch-tabs {
    display: flex; gap: 4px; padding: 12px 24px 0;
    border-bottom: 1px solid #3d3929; flex-shrink: 0;
    align-self: stretch;
  }
  .ch-tab {
    background: none; border: none; color: #6b6560; cursor: pointer;
    font-size: 11px; font-weight: 600; padding: 8px 14px 10px;
    border-bottom: 2px solid transparent;
    transition: color 0.1s, border-color 0.12s; font-family: inherit;
  }
  .ch-tab:hover:not(.disabled) { color: #a8a194; }
  .ch-tab.active { color: #da7756; border-bottom-color: #da7756; }
  .ch-tab.disabled {
    color: #3d3929; cursor: default; pointer-events: none;
  }

  .ch-panel { display: none; flex: 1; overflow-y: auto; padding: 28px 24px;
    flex-direction: column; align-items: center; }
  .ch-panel.active { display: flex; }

  /* Route picker cards */
  .route-cards { display: flex; gap: 12px; }
  .route-card {
    flex: 1; background: #2b2a27; border: 1px solid #3d3929; border-radius: 10px;
    padding: 20px; cursor: pointer; transition: border-color 0.15s, background 0.15s;
    position: relative;
  }
  .route-card:hover { border-color: #da7756; background: #322f2a; }
  .route-icon { color: #da7756; margin-bottom: 12px; }
  .route-title { font-size: 14px; font-weight: 700; color: #e8e4db; margin-bottom: 6px; }
  .route-desc { font-size: 11px; color: #6b6560; line-height: 1.5; }
  .route-tag {
    display: inline-block; margin-top: 10px; font-size: 10px; font-weight: 700;
    color: #da7756; background: #da775615; padding: 3px 8px; border-radius: 4px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }

  /* Setup architecture diagram */
  .arch-diagram {
    display: flex; align-items: center; justify-content: center;
    gap: 0; padding: 32px 0 24px; position: relative;
  }
  .arch-node {
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    opacity: 0; transform: translateY(12px);
    animation: archNodeIn 0.5s ease forwards;
  }
  .arch-node:nth-child(1) { animation-delay: 0.2s; }
  .arch-node:nth-child(3) { animation-delay: 0.7s; }
  .arch-node:nth-child(5) { animation-delay: 1.2s; }
  @keyframes archNodeIn {
    to { opacity: 1; transform: translateY(0); }
  }
  .arch-box {
    width: 72px; height: 72px; border-radius: 14px;
    display: flex; align-items: center; justify-content: center;
    font-size: 28px; border: 1.5px solid #3d3929; background: #2b2a27;
    transition: border-color 0.3s;
  }
  .arch-node.client .arch-box { border-color: #6b6560; }
  .arch-node.client .arch-label { color: #8b8579; }
  .arch-node.gw .arch-box { border-color: #da7756; background: #da775612; }
  .arch-node.srv .arch-box { border-color: #5ba97b; background: #5ba97b12; }
  .arch-label {
    font-size: 11px; font-weight: 600; color: #6b6560;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  .arch-node.gw .arch-label { color: #da7756; }
  .arch-node.srv .arch-label { color: #5ba97b; }
  .arch-sublabel {
    font-size: 10px; color: #4a4540; margin-top: -4px;
  }
  .arch-node.gw .arch-sublabel { color: #da775688; }

  .arch-arrow {
    display: flex; align-items: center; width: 48px; height: 2px;
    position: relative; overflow: hidden;
    opacity: 0; animation: archArrowIn 0.4s ease forwards;
  }
  .arch-arrow:nth-child(2) { animation-delay: 0.5s; }
  .arch-arrow:nth-child(4) { animation-delay: 1.0s; }
  @keyframes archArrowIn {
    to { opacity: 1; }
  }
  .arch-arrow-line {
    position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: #3d3929; border-radius: 1px;
  }
  .arch-arrow-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: #da7756; position: absolute; top: -2px;
    animation: archDotMove 2s ease-in-out infinite;
  }
  .arch-arrow:nth-child(4) .arch-arrow-dot {
    background: #5ba97b;
  }
  .arch-arrow.bidi .arch-arrow-dot { animation: archDotBidi 3s ease-in-out infinite; }
  @keyframes archDotMove {
    0% { left: 0; opacity: 0; }
    10% { opacity: 1; }
    90% { opacity: 1; }
    100% { left: calc(100% - 6px); opacity: 0; }
  }
  @keyframes archDotBidi {
    0% { left: 0; opacity: 0; }
    5% { opacity: 1; }
    45% { left: calc(100% - 6px); opacity: 1; }
    50% { left: calc(100% - 6px); opacity: 0; }
    55% { left: calc(100% - 6px); opacity: 1; }
    95% { left: 0; opacity: 1; }
    100% { left: 0; opacity: 0; }
  }

  .arch-note {
    text-align: center; color: #4a4540; font-size: 11px;
    font-style: italic; margin-top: -8px; margin-bottom: 20px;
    opacity: 0; animation: archNodeIn 0.5s ease 1.6s forwards;
  }

  /* Local network diagram */
  .net-bubble {
    border: 1.5px dashed #3d3929; border-radius: 18px;
    padding: 20px 16px 14px; position: relative; margin: 16px 0;
  }
  .net-bubble-label {
    position: absolute; top: -9px; left: 18px;
    background: #1e1d1a; padding: 0 8px;
    font-size: 10px; font-weight: 700; color: #6b6560;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .net-devices {
    display: flex; align-items: flex-start; justify-content: center;
    gap: 20px; flex-wrap: wrap;
  }
  .net-device {
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    min-width: 64px;
  }
  .net-device-icon {
    width: 44px; height: 44px; border-radius: 10px; border: 1.5px solid #3d3929;
    background: #2b2a27; display: flex; align-items: center; justify-content: center;
    font-size: 20px;
  }
  .net-device.gw .net-device-icon { border-color: #da7756; background: #da775612; }
  .net-device.srv .net-device-icon { border-color: #5ba97b; background: #5ba97b12; }
  .net-device-name {
    font-size: 10px; font-weight: 600; color: #6b6560;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .net-device.gw .net-device-name { color: #da7756; }
  .net-device.srv .net-device-name { color: #5ba97b; }
  .net-router {
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    margin: 0 8px;
  }
  .net-router-icon {
    width: 44px; height: 44px; border-radius: 10px; border: 1.5px solid #4a4540;
    background: #2b2a27; display: flex; align-items: center; justify-content: center;
    font-size: 18px;
  }
  .net-router-name { font-size: 10px; color: #4a4540; font-weight: 600; text-transform: uppercase; }
  .net-no-internet {
    text-align: center; margin-top: 10px; display: flex; align-items: center;
    justify-content: center; gap: 6px; color: #4a4540; font-size: 11px;
  }
  .net-no-internet svg { opacity: 0.5; }

  /* Requirements checklist */
  .req-section { margin-top: 20px; }
  .req-title {
    font-size: 11px; font-weight: 700; color: #6b6560;
    text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px;
  }
  .req-list { list-style: none; padding: 0; margin: 0 0 16px; }
  .req-list li {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: #a8a194; padding: 5px 0;
  }
  .req-check {
    width: 16px; height: 16px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 9px; font-weight: 700;
  }
  .req-check.ok { background: #5ba97b22; color: #5ba97b; }
  .req-check.info { background: #da775622; color: #da7756; }
  .req-check.manual { background: #3d3929; color: #6b6560; }
  .req-role {
    font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .req-role.gw { background: #da775615; color: #da7756; }
  .req-role.srv { background: #5ba97b15; color: #5ba97b; }
  .req-role.client { background: #3d3929; color: #6b6560; }
  /* Control buttons */
  .ctl-btn {
    width: 32px; height: 32px; border-radius: 4px; border: none;
    background: none; cursor: pointer; position: relative;
    display: flex; align-items: center; justify-content: center;
    color: #8b8579; transition: background 0.1s, color 0.1s;
  }
  .ctl-btn:hover { background: #322f2a; color: #e8e4db; }
  .ctl-btn.barred { color: #c94a3e; }
  .ctl-btn.barred:hover { background: #c94a3e22; }
  .ctl-btn svg { width: 18px; height: 18px; }
  .ctl-btn .bar-line {
    display: none; position: absolute;
    width: 24px; height: 2px; background: #c94a3e;
    transform: rotate(-45deg); border-radius: 1px;
  }
  .ctl-btn.barred .bar-line { display: block; }

  /* Mobile */
  @media (hover: none) and (pointer: coarse), (max-width: 768px) {
    #server-strip {
      position: fixed; left: -48px; top: 0; bottom: 0;
      width: 48px; padding: 8px 0; gap: 6px;
      z-index: 101; transition: left 0.2s ease;
    }
    #sidebar.open ~ #main #server-strip,
    #server-strip.open {
      left: 0;
    }
    #sidebar {
      left: -288px !important; width: 240px;
    }
    #sidebar.open {
      left: 48px !important;
    }
    #server-strip .server-icon {
      width: 36px; height: 36px; font-size: 14px;
    }
    #server-strip .server-icon.active::before {
      left: -10px; height: 28px; width: 3px;
    }
    #mobile-server-select { display: none; }
    #sidebar {
      position: fixed; left: -240px; top: 0; bottom: 0;
      z-index: 100; transition: left 0.2s ease;
    }
    #sidebar.open { left: 0; box-shadow: 4px 0 20px rgba(0,0,0,0.5); }
    #overlay {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.5); z-index: 99;
    }
    #sidebar.open ~ #main #overlay { display: block; }
    /* hamburger always visible via default styles */
    #right-panel { display: none; }
    #right-panel.open {
      display: flex; position: fixed; right: 0; top: 48px; bottom: 0;
      z-index: 100; box-shadow: -4px 0 20px rgba(0,0,0,0.5);
    }
  }
  /* Terminal controls occupy layout space so they never cover output. */
  #content-area, #terminal-container { min-height: 0; }
  #terminal-container { touch-action: pinch-zoom; }
  #topbar.has-tabs { padding:0 6px; gap:4px; }
  #topbar.has-tabs .channel-name, #topbar.has-tabs .topbar-divider, #topbar.has-tabs .topbar-stats { display:none; }
  #topbar.has-tabs #status-label { position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); }
  #topbar.has-tabs .status-text { flex-shrink:0; }
  #topbar.has-tabs > button { flex-shrink:0; }
  #terminal-tabs { display:flex; flex:1; height:100%; min-width:0; }
  #terminal-tabs[hidden] { display:none; }
  #terminal-tab-list { display:flex; flex:1; min-width:0; overflow-x:auto; overflow-y:hidden; touch-action:pan-x; scrollbar-width:thin; }
  .terminal-tab { position:relative; display:flex; flex:0 0 auto; max-width:min(210px,100%); border-right:1px solid #3d3929; border-bottom:2px solid transparent; }
  .terminal-tab.dragging { opacity:0.45; }
  .terminal-tab.drop-before::before, .terminal-tab.drop-after::after { content:''; position:absolute; top:0; bottom:0; width:3px; background:#da7756; pointer-events:none; }
  .terminal-tab.drop-before::before { left:0; }
  .terminal-tab.drop-after::after { right:0; }
  .terminal-tab .terminal-tab-grip { flex:0 0 24px; padding:0; cursor:grab; touch-action:none; user-select:none; -webkit-user-select:none; -webkit-touch-callout:none; color:#8b8579; }
  .terminal-tab-grip:active { cursor:grabbing; }
  .terminal-tab.active { background:#1a1915; border-bottom-color:#da7756; }
  .terminal-tab button, #terminal-tab-add { border:0; background:transparent; color:#b4ada0; cursor:pointer; font:inherit; }
  .terminal-tab-select { display:flex; flex:1; flex-direction:column; justify-content:center; align-items:flex-start; min-width:0; max-width:150px; padding:3px 6px; text-align:left; }
  .terminal-tab.active .terminal-tab-select { color:#e8e4db; }
  .terminal-tab-name { max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; }
  .terminal-tab-server { max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#8b8579; font-size:10px; }
  .terminal-tab-close { width:28px; flex:0 0 28px; font-size:18px !important; }
  .terminal-tab-close:hover, #terminal-tab-add:hover { color:#da7756; background:#322f2a; }
  #terminal-tab-add { flex:0 0 44px; font-size:24px; border-left:1px solid #3d3929; }
  #mobile-controls { position:static; flex-shrink:0; flex-direction:row; align-items:center; padding:5px 6px; gap:6px; background:#1a1915; }
  #mobile-controls.visible { display:flex; }
  .quick-keys { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:4px; flex:1; max-width:260px; min-width:0; }
  #mobile-controls .mob-btn { height:36px; min-width:0; padding:0 4px; }
  #mobile-controls .arrow-cluster { grid-template-columns:repeat(3,32px); grid-template-rows:repeat(2,36px); gap:4px; margin-left:auto; }
  #mobile-controls .mob-enter { width:36px !important; height:76px !important; }
  .control-row { display:flex; align-items:center; gap:6px; min-width:0; }
  .mob-btn[aria-pressed="true"] { background:#da775633; color:#da7756; border-color:#da7756; }
  @media (max-width:350px) {
    #mobile-controls { gap:4px; padding:4px; }
    #mobile-controls .arrow-cluster { grid-template-columns:repeat(3,28px); gap:3px; }
    #mobile-controls .mob-enter { width:30px !important; }
    .quick-keys { gap:3px; }
  }
  button:disabled { opacity:0.45; cursor:default; }
  button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline:2px solid #da7756; outline-offset:2px; }
  #input-state { position:absolute; width:1px; height:1px; padding:0; overflow:hidden; clip-path:inset(50%); white-space:nowrap; }
  #jump-bottom { position:absolute; bottom:96px; right:8px; }
  #notice { display:none; position:fixed; bottom:100px; left:50%; transform:translateX(-50%); width:max-content; max-width:90vw; padding:12px 16px; border:1px solid #da7756; border-radius:8px; background:#23221e; color:#e8e4db; z-index:1000; font-size:14px; pointer-events:none; }
  #notice.visible { display:block; }
  dialog, #paste-modal { position:fixed; inset:0; margin:auto; padding:0; width:min(620px,94vw); max-width:94vw; max-height:90dvh; border:1px solid #3d3929; border-radius:12px; background:#23221e; color:#e8e4db; overflow:auto; }
  dialog:not([open]), #paste-modal:not([open]) { display:none; }
  dialog[open], #paste-modal[open] { display:block; }
  dialog::backdrop { background:rgba(0,0,0,.65); }
  .dialog-body { padding:18px; display:flex; flex-direction:column; gap:12px; }
  .dialog-body h2 { font-size:18px; }
  .dialog-body p, .dialog-body label { font-size:13px; line-height:1.5; color:#b4ada0; }
  .dialog-body button, .dialog-body select { min-height:44px; padding:8px 12px; background:#3d3929; color:#e8e4db; border:1px solid #3d3929; border-radius:6px; font:inherit; cursor:pointer; }
  .dialog-body input, .dialog-body textarea { background:#1a1915; color:#e8e4db; padding:10px; border:1px solid #3d3929; border-radius:6px; font-size:16px; user-select:text; -webkit-user-select:text; }
  #paste-area { width:100%; min-height:120px; font-size:16px; }
  .dialog-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; }
  #history-modal { width:min(1000px,96vw); }
  #history-modal .dialog-body { height:min(780px,90dvh); }
  #history-modal .dialog-actions { flex-shrink:0; }
  #history-text { flex:1; width:100%; min-height:100px; padding:12px; background:#1a1915; border:1px solid #3d3929; border-radius:6px; font-family:monospace; font-size:14px; line-height:20px; white-space:pre-wrap; overflow-wrap:anywhere; overflow:auto; user-select:text; -webkit-user-select:text; touch-action:pan-y pinch-zoom; -webkit-touch-callout:default; }
  #history-text *, #history-links a { user-select:text; -webkit-user-select:text; -webkit-touch-callout:default; }
  #history-text a, #history-links a { color:#9bbcff; text-decoration:underline; text-underline-offset:3px; }
  #history-links { flex:0 1 auto; min-height:34px; max-height:20dvh; overflow:auto; font-size:13px; }
  #history-links summary { cursor:pointer; padding:8px 0; }
  #history-links a { display:block; padding:10px 4px; overflow-wrap:anywhere; }
  #history-load-latest[hidden] { display:none; }
  #history-search { min-width:0; flex:1; }
  #shortcut-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px; max-height:45dvh; overflow-y:auto; }
  #shortcut-grid button { text-align:left; padding:10px 8px; background:#1a1915; }
  #shortcut-grid kbd { display:block; font:600 12px monospace; color:#da7756; }
  #shortcut-grid span { display:block; margin-top:5px; font-size:11px; line-height:1.3; color:#8b8579; }
  #conn-claim[aria-pressed="true"] { color:#5ba97b; }
  .file-item { cursor:default; }
  button.file-item { width:100%; text-align:left; background:none; border:0; color:inherit; cursor:pointer; }
  .session-item:focus-within .s-actions, .project-header:focus-within .p-actions { display:flex; }
  @media (hover:none) and (pointer:coarse), (max-width:768px) {
    .category-label .cat-add, .session-item .s-actions, .project-header .p-actions { display:flex; }
    .category-label .cat-add, .session-item .s-btn, .project-header .p-btn { width:28px; height:28px; }
    .category-label { padding-top:8px; }
    .session-item { min-height:34px; padding:0 4px; }
    .project-header { min-height:32px; }
    #sidebar { width:min(240px,calc(100vw - 48px)); }
    #sidebar.open { left:48px !important; }
  }

</style>
</head>
<body>

<div id="server-strip">
  <div class="server-icon" id="home-server" title="Home" onclick="showHome()">C</div>
  <div class="server-divider"></div>
  <div id="server-list"></div>
  <!-- Servers enroll via CLI: tmux-bridge add server --name X -->
</div>

<div id="sidebar">
  <div id="sidebar-header">
    <span>Tmux Bridge</span>
    <select id="mobile-server-select" onchange="switchServerMobile(this.value)"></select>
  </div>
  <div class="category-label"><span class="cat-text">General</span></div>
  <div id="welcome-item" class="session-item" onclick="goGeneral()">
    <span class="hash">#</span><span class="s-name">welcome</span>
  </div>
  <div id="welcome-viewers"></div>
  <div class="category-label"><span class="cat-text">Sessions</span><button class="cat-add" title="New Claude session" onclick="newClaudeSession()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l6-6-6-6"/><line x1="12" y1="19" x2="20" y2="19"/></svg></button><button class="cat-add" title="New dir" onclick="addProject()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button></div>
  <div id="session-list"></div>
  <div id="bottom-panel">
    <div id="conn-header">
      <div id="conn-header-row">
        <div id="conn-header-info">
          <div id="conn-header-title">
            <span class="signal-icon"><span></span><span></span><span></span></span>
            Connected
          </div>
          <div id="conn-header-sub"></div>
        </div>
        <button id="conn-claim" title="Claim control" onclick="claimControl()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>
          </svg>
        </button>
        <button id="conn-disconnect" title="Disconnect" onclick="disconnectSession()">
          <svg viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28-.28 0-.53-.11-.71-.29L.29 13.08a.956.956 0 0 1-.29-.7c0-.28.11-.53.29-.71C3.34 8.78 7.46 7 12 7s8.66 1.78 11.71 4.67c.18.18.29.43.29.71 0 .28-.11.53-.29.71l-2.48 2.48c-.18.18-.43.29-.71.29-.27 0-.52-.1-.7-.28a11.27 11.27 0 0 0-2.67-1.85.996.996 0 0 1-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/>
          </svg>
        </button>
      </div>
    </div>
    <div id="conn-divider"></div>
    <div id="user-row">
      <div class="avatar">T<span class="status-pip"></span></div>
      <div class="user-name-block">
        <div class="username">Operator</div>
      </div>
      <div class="user-btns">
        <button class="ctl-btn" id="mic-btn" title="Pause terminal input" aria-label="Pause terminal input" aria-pressed="false" onclick="toggleMic()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" y1="19" x2="12" y2="23"/>
            <line x1="8" y1="23" x2="16" y2="23"/>
          </svg>
          <span class="bar-line"></span>
        </button>
        <button class="ctl-btn" id="deafen-btn" title="Mute notification sounds" aria-label="Mute notification sounds" aria-pressed="false" onclick="toggleDeafen()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 18v-6a9 9 0 0 1 18 0v6"/>
            <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/>
            <path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>
          </svg>
          <span class="bar-line"></span>
        </button>
      </div>
    </div>
  </div>
</div>

<div id="main">
  <div id="topbar">
    <button id="hamburger" aria-label="Toggle sessions sidebar" onclick="toggleSidebar()">&#9776;</button>
    <div class="channel-name"><span class="hash">#</span><span id="chan-title">&mdash;</span></div>
    <div id="terminal-tabs" hidden aria-label="Open terminals">
      <div id="terminal-tab-list" role="tablist" aria-label="Terminal tabs"></div>
      <button id="terminal-tab-add" type="button" aria-label="Open another terminal" title="Choose another terminal" onclick="openTerminalPicker()">+</button>
    </div>
    <div class="topbar-divider"></div>
    <span class="status-text">
      <span class="conn-dot disconnected" id="conn-dot"></span>
      <span id="status-label">connecting</span>
    </span>
    <span id="topbar-stats" class="topbar-stats"></span>
    <button id="topbar-reload" title="Force reload" onclick="location.reload(true)">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
    </button>
    <button id="rpanel-toggle" onclick="toggleRightPanel()" title="Toggle stats panel">
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="15" y1="3" x2="15" y2="21"/></svg>
    </button>
  </div>
  <div id="content-wrap">
    <div id="content-area">
      <div id="home-view">
        <!-- Welcome tab -->
        <div id="tab-welcome" class="home-panel active">
          <h1>Tmux Bridge</h1>
          <p>Access and manage terminal sessions across all your machines from one place. Connect servers, share terminals, and monitor system health in real time.</p>
          <div class="welcome-features">
            <div class="welcome-feat">
              <div class="wf-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><circle cx="6" cy="6" r="1"/><circle cx="6" cy="18" r="1"/></svg>Multi-server</div>
              <div class="wf-desc">Connect servers from anywhere. Behind NAT, across networks — just one command to join.</div>
            </div>
            <div class="welcome-feat">
              <div class="wf-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>Live viewers</div>
              <div class="wf-desc">See who's watching each session. Share control with typing indicators and presence.</div>
            </div>
            <div class="welcome-feat">
              <div class="wf-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>System metrics</div>
              <div class="wf-desc">CPU, RAM, GPU, disk, and network metrics with real-time gauges and history graphs.</div>
            </div>
            <div class="welcome-feat">
              <div class="wf-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Secure</div>
              <div class="wf-desc">Cloudflare Tunnel keeps your servers off the public internet. No open ports needed.</div>
            </div>
          </div>
          <div class="welcome-cta">
            <button class="welcome-btn primary" onclick="switchHomeTab('dashboard')">Dashboard</button>
            <button class="welcome-btn secondary" onclick="switchHomeTab('specs')">Gateway Specs</button>
          </div>
        </div>

        <!-- Specs tab -->
        <div id="tab-specs" class="home-panel">
          <h2 style="color:#e8e4db; font-size:18px; font-weight:700; margin-bottom:16px;">Gateway Specs</h2>
          <div id="home-specs-grid" class="specs-grid-home"></div>
        </div>

        <!-- Dashboard tab -->
        <div id="tab-dashboard" class="home-panel">
          <h2 style="color:#e8e4db; font-size:18px; font-weight:700; margin-bottom:16px;">Servers</h2>
          <div class="server-cards" id="home-cards"></div>
        </div>
      </div>
      <div id="server-landing">
        <div class="landing-icon">>_</div>
        <h2 id="landing-server-name"></h2>
        <p>Select a session from the sidebar to connect</p>
        <div id="landing-session-count"></div>
        <div id="specs-grid"></div>
    </div>
      <div id="terminal-container" class="hidden"></div>
      <button id="jump-bottom" onclick="returnToLive()" title="Latest output" aria-label="Latest output">↓</button>
      <div id="mobile-controls" aria-label="Terminal controls">
        <div class="quick-keys">
          <button class="mob-btn" data-terminal-key="Escape" aria-label="Escape" title="Escape" style="color:#da7756;border-color:#da775644">ESC</button>
          <button class="mob-btn" id="kbd-btn" onclick="toggleKeyboard()" aria-label="Keyboard" title="Show / hide keyboard" aria-pressed="false">KB</button>
          <button class="mob-btn" onclick="showPasteModal()" aria-label="Paste" title="Paste text">PST</button>
          <button class="mob-btn" id="image-btn" onclick="triggerUpload()" aria-label="Image" title="Attach image">IMG</button>
          <button class="mob-btn" onclick="openShortcuts()" aria-label="Shortcuts" title="More shortcuts">⌘</button>
          <button class="mob-btn modifier" id="ctrl-key" aria-pressed="false" onclick="toggleModifier('ctrl')" title="Ctrl + next key">Ctrl</button>
          <button class="mob-btn modifier" id="alt-key" aria-pressed="false" onclick="toggleModifier('alt')" title="Alt + next key">Alt</button>
          <button class="mob-btn" data-terminal-key="Tab" title="Tab">Tab</button>
          <button class="mob-btn" onclick="showHistory()" aria-label="Select / History" title="Select text / History">SEL</button>
          <button class="mob-btn mob-bksp" id="mob-bksp" data-terminal-key="Backspace" aria-label="Backspace" title="Backspace"><svg viewBox="0 0 24 24"><path d="M21 4H8l-7 8 7 8h13a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z"/><path d="m18 9-6 6m0-6 6 6"/></svg></button>
        </div>
        <div class="arrow-cluster">
          <button class="mob-btn mob-arrow" data-terminal-key="ArrowUp" aria-label="Up arrow"><svg viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"/></svg></button>
          <button class="mob-btn mob-arrow" data-terminal-key="ArrowLeft" aria-label="Left arrow"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg></button>
          <button class="mob-btn mob-arrow" data-terminal-key="ArrowDown" aria-label="Down arrow"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></button>
          <button class="mob-btn mob-arrow" data-terminal-key="ArrowRight" aria-label="Right arrow"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>
        </div>
        <button class="mob-btn mob-enter" data-terminal-key="Enter" aria-label="Enter" title="Enter"><svg viewBox="0 0 24 24"><path d="M20 6v6a2 2 0 0 1-2 2H6"/><polyline points="10 18 6 14 10 10"/></svg></button>
        <input type="file" id="img-upload" accept="image/*" hidden onchange="handleUpload(this)">
        <div id="input-state" role="status"></div>
      </div>
      <span id="mob-esc" hidden></span>
      <dialog id="paste-modal" aria-labelledby="paste-title">
        <form method="dialog" class="dialog-body" onsubmit="event.preventDefault();sendPaste()">
          <h2 id="paste-title">Paste text</h2>
          <p id="paste-target"></p>
          <label for="paste-area">Review your text before inserting it. Newlines may run commands in some programs.</label>
          <textarea id="paste-area" rows="7" placeholder="Paste or type here" autocapitalize="off" autocomplete="off" spellcheck="false"></textarea>
          <p id="paste-status" role="status"></p>
          <div class="dialog-actions">
            <button type="button" onclick="closePasteModal()">Keep draft & close</button>
            <button type="submit" id="paste-send">Insert text</button>
          </div>
        </form>
      </dialog>
      <dialog id="history-modal" aria-labelledby="history-title">
        <div class="dialog-body">
          <h2 id="history-title">Select / History</h2>
          <p id="history-status" role="status"></p>
          <div class="control-row">
            <input id="history-search" type="search" aria-label="Find in history" placeholder="Find in history">
            <button onclick="findHistory()">Find next</button>
          </div>
          <details id="history-links">
            <summary id="history-links-title">Links in this text</summary>
            <div id="history-link-list"></div>
          </details>
          <pre id="history-text" tabindex="0" aria-label="Terminal history; select text to copy"></pre>
          <div class="dialog-actions">
            <button id="history-load-latest" hidden onclick="applyPendingHistory()">Load full history</button>
            <button onclick="copyHistory(false)">Copy selection</button>
            <button onclick="copyHistory(true)">Copy all loaded</button>
            <button onclick="closeHistory()">Done</button>
          </div>
        </div>
      </dialog>
      <dialog id="shortcut-modal" aria-labelledby="shortcut-title">
        <div class="dialog-body">
          <h2 id="shortcut-title">Terminal shortcuts</h2>
          <p>Tap a shortcut, or tap Ctrl / Alt below the terminal and type the next key. Modifiers reset after one key.</p>
          <div id="shortcut-grid"></div>
          <p>With a physical keyboard, terminal shortcuts work normally. Use ⌘/Ctrl+Shift+P for this panel and ⌘/Ctrl+Shift+F for history.</p>
          <div class="dialog-actions">
            <button onclick="changeFontSize(-1)" aria-label="Smaller terminal text">A−</button>
            <button onclick="changeFontSize(1)" aria-label="Larger terminal text">A+</button>
            <button onclick="closeDialog('shortcut-modal')">Done</button>
          </div>
        </div>
      </dialog>
      <dialog id="choice-modal" aria-labelledby="choice-title">
        <form method="dialog" class="dialog-body">
          <h2 id="choice-title"></h2>
          <label for="choice-select">Choose an option</label>
          <select id="choice-select"></select>
          <div class="dialog-actions"><button value="cancel">Cancel</button><button value="apply">Apply</button></div>
        </form>
      </dialog>
      <div id="notice" role="status" aria-live="polite"></div>
      <div id="overlay" onclick="toggleSidebar()"></div>
    </div>
    <div id="right-panel">
      <div id="rp-tabs">
        <button class="rp-tab active" onclick="switchRpTab('stats')">Stats</button>
        <button class="rp-tab" onclick="switchRpTab('files')">Files</button>
      </div>
      <div id="rp-content"></div>
      <div id="rp-files" style="display:none">
        <div id="file-path-bar"><span id="file-path-text">~</span></div>
        <div id="file-list"></div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js" integrity="sha384-J4qzUjBl1FxyLsl/kQPQIOeINsmp17OHYXDOMpMxlKX53ZfYsL+aWHpgArvOuof9" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js" integrity="sha384-XGqKrV8Jrukp1NITJbOEHwg01tNkuXr6uB6YEj69ebpYU3v7FvoGgEg23C1Gcehk" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/lib/addon-web-links.min.js" integrity="sha384-S1biLeI8L/bFduIVvCxbn/l4EtaG4nTqQjGF7qCYTbsGXGFe8KgIKXtw4+UWxprv" crossorigin="anonymous"></script>
<script>
// ── Sound effects ──
const AudioCtx = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;
function ensureAudio() {
  if (!audioCtx) audioCtx = new AudioCtx();
  return audioCtx;
}
function playTone(freq, duration, type, vol) {
  if (isDeafened) return;
  try {
    const ctx = ensureAudio();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type || 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(vol || 0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + duration);
  } catch(e) {}
}
function soundConnect() {
  playTone(880, 0.12, 'sine', 0.12);
  setTimeout(() => playTone(1108, 0.15, 'sine', 0.10), 80);
}
function soundDisconnect() {
  playTone(660, 0.12, 'sine', 0.12);
  setTimeout(() => playTone(440, 0.18, 'sine', 0.10), 80);
}
function soundViewerJoin() { playTone(1200, 0.08, 'sine', 0.06); }
function soundViewerLeave() { playTone(600, 0.08, 'sine', 0.06); }
function soundLostControl() {
  playTone(520, 0.1, 'triangle', 0.10);
  setTimeout(() => playTone(400, 0.15, 'triangle', 0.08), 100);
}

// ── Server management (backed by gateway API) ──
let currentServer = '';  // server name currently selected
let cachedServerList = [];  // from GET /api/servers

async function fetchServers() {
  try {
    const r = await fetch('/api/servers');
    cachedServerList = await r.json();
  } catch(e) { cachedServerList = []; }
  return cachedServerList;
}

function updateMobileServerSelect() {
  const sel = document.getElementById('mobile-server-select');
  if (!sel) return;
  sel.innerHTML = '';
  cachedServerList.forEach(srv => {
    const opt = document.createElement('option');
    opt.value = srv.name;
    opt.textContent = srv.name;
    if (srv.name === currentServer) opt.selected = true;
    sel.appendChild(opt);
  });
}
function switchServerMobile(name) {
  enterServer(name);
  document.getElementById('sidebar').classList.remove('open');
}

function renderServers() {
  const list = document.getElementById('server-list');
  list.innerHTML = '';
  updateMobileServerSelect();
  cachedServerList.forEach((srv) => {
    const div = document.createElement('div');
    const isActive = srv.name === currentServer;
    const connected = srv.connected;
    div.className = 'server-icon machine-server' + (isActive ? ' active' : '') + (connected ? '' : ' disconnected');
    div.title = srv.name + (connected ? ' (connected)' : ' (offline)');
    div.innerHTML = srv.name.charAt(0).toUpperCase() +
      (connected ? '' : '<span class="srv-offline"></span>');
    div.addEventListener('click', () => {
      if (connected) enterServer(srv.name);
    });
    list.appendChild(div);
  });
}

// Servers are enrolled via CLI: tmux-bridge add server --name X

let onServer = false;

let currentHomeTab = 'welcome';

function showHome() {
  closeConnection();
  currentSession = '';
  currentServer = '';
  saveTerminalTabs();
  renderTerminalTabs();
  onServer = false;
  document.getElementById('home-server').classList.add('active');
  document.querySelectorAll('.machine-server').forEach(el => el.classList.remove('active'));
  document.getElementById('home-view').classList.add('visible');
  document.getElementById('terminal-container').classList.add('hidden');
  document.getElementById('mobile-controls').classList.remove('visible');
  document.getElementById('jump-bottom').classList.remove('visible');
  document.getElementById('mob-esc').classList.remove('visible');
  document.getElementById('server-landing').classList.remove('visible');
  document.getElementById('sidebar').style.display = '';
  document.getElementById('conn-header').classList.remove('visible');
  document.getElementById('conn-divider').classList.remove('visible');
  connDot.className = 'conn-dot';
  statusLabel.textContent = '';
  renderHomeSidebar();
  // Default to dashboard if servers exist, welcome if not
  const defaultTab = cachedServerList.length > 0 ? 'dashboard' : 'welcome';
  switchHomeTab(currentHomeTab === 'welcome' && cachedServerList.length > 0 ? defaultTab : currentHomeTab);
}

const homeChannels = ['welcome', 'specs', 'dashboard'];

function switchHomeTab(tab) {
  currentHomeTab = tab;
  // Update sidebar active state
  document.querySelectorAll('#session-list .session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.channel === tab);
  });
  // Update topbar
  chanTitle.textContent = tab;
  // Update panels
  document.querySelectorAll('.home-panel').forEach(p => p.classList.remove('active'));
  const panel = document.getElementById('tab-' + tab);
  if (panel) panel.classList.add('active');
  // Load data for the active tab
  if (tab === 'specs') loadHomeSpecs();
  if (tab === 'dashboard') { renderHomeCards(); }
}

function renderHomeSidebar() {
  const header = document.getElementById('sidebar-header');
  const _hsel = header.querySelector('#mobile-server-select');
  header.innerHTML = '<span>Tmux Bridge</span>';
  if (_hsel) header.appendChild(_hsel);
  const list = document.getElementById('session-list');
  list.innerHTML = '';
  homeChannels.forEach(ch => {
    const div = document.createElement('div');
    div.className = 'session-item' + (ch === currentHomeTab ? ' active' : '');
    div.dataset.channel = ch;
    div.innerHTML = '<span class="hash">#</span><span class="s-name">' + ch + '</span>';
    div.addEventListener('click', () => {
      switchHomeTab(ch);
      closeSidebar();
    });
    list.appendChild(div);
  });
  // Hide the welcome-item and category labels meant for server view
  document.getElementById('welcome-item').style.display = 'none';
  document.querySelectorAll('.category-label').forEach(el => el.style.display = 'none');
}

function enterServer(serverName) {
  closeConnection();
  currentSession = '';
  currentServer = serverName;
  renderTerminalTabs();
  cachedSessions=[];presenceData={};
  document.getElementById('conn-header').classList.remove('visible');
  document.getElementById('conn-divider').classList.remove('visible');
  onServer = true;
  document.getElementById('home-server').classList.remove('active');
  document.getElementById('home-view').classList.remove('visible');
  document.getElementById('sidebar').style.display = '';
  // Restore server sidebar elements
  document.getElementById('welcome-item').style.display = '';
  document.querySelectorAll('.category-label').forEach(el => el.style.display = '');
  const _sh = document.getElementById('sidebar-header');
  const _sel = _sh.querySelector('#mobile-server-select');
  _sh.innerHTML = '<span>' + serverName + '</span>';
  if (_sel) _sh.appendChild(_sel);
  renderServers();
  document.getElementById('rp-content').replaceChildren();
  document.getElementById('file-list').replaceChildren();
  if(document.getElementById('rp-files').style.display!=='none')loadFiles(filePaths.get(serverName)||'~');
  showServerLanding();
  const loading=loadSessions();
  updateRightPanel();
  return loading;
}

function showServerLanding() {
  document.getElementById('terminal-container').classList.add('hidden');
  document.getElementById('mobile-controls').classList.remove('visible');
  document.getElementById('jump-bottom').classList.remove('visible');
  document.getElementById('mob-esc').classList.remove('visible');
  document.getElementById('server-landing').classList.add('visible');
  document.getElementById('landing-server-name').textContent = currentServer || 'Server';
  chanTitle.textContent = 'welcome';
  connDot.className = 'conn-dot';
  statusLabel.textContent = '';
  const count = cachedSessions.length;
  document.getElementById('landing-session-count').textContent =
    count ? count + ' session' + (count !== 1 ? 's' : '') + ' available' : 'No active sessions';
  document.getElementById('welcome-item').classList.add('active');
  loadServerSpecs();
}

function loadHomeSpecs() {
  fetch('/api/specs').then(r => r.json()).then(s => {
    renderSpecsGrid(s, 'home-specs-grid');
  }).catch(() => {});
}

function loadServerSpecs() {
  const server=currentServer;
  if (!server) return;
  document.getElementById('specs-grid').textContent='Loading…';
  fetch('/api/' + encodeURIComponent(server) + '/specs').then(r => {if(!r.ok)throw new Error('Unavailable');return r.json();}).then(s => {
    if(server!==currentServer)return;
    renderSpecsGrid(s, 'specs-grid');
  }).catch(() => {});
}

function renderSpecsGrid(s, targetId) {
  const g = document.getElementById(targetId || 'specs-grid');
  g.innerHTML = '';
  const ico = (d) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + d + '</svg>';
  const icons = {
    host:   ico('<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><circle cx="6" cy="6" r="1"/><circle cx="6" cy="18" r="1"/>'),
    os:     ico('<path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>'),
    kernel: ico('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'),
    uptime: ico('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
    cpu:    ico('<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/><rect x="8" y="8" width="8" height="8" rx="1"/>'),
    load:   ico('<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>'),
    mem:    ico('<path d="M6 19v-8a6 6 0 1 1 12 0v8"/><line x1="2" y1="19" x2="22" y2="19"/>'),
    disk:   ico('<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>'),
    gpu:    ico('<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h2M10 12h2M14 12h2"/>'),
  };
  function section(label) {
    const d = document.createElement('div');
    d.className = 'spec-section';
    d.textContent = label;
    g.appendChild(d);
  }
  function card(icon, label, value, sub, wide) {
    const d = document.createElement('div');
    d.className = 'spec-card' + (wide ? ' wide' : '');
    let h = '<div class="spec-label">' + icon + label + '</div><div class="spec-value">' + value + '</div>';
    if (sub) h += '<div class="spec-sub">' + sub + '</div>';
    d.innerHTML = h;
    g.appendChild(d);
  }

  section('System');
  card(icons.host, 'Hostname', s.hostname, null, true);
  card(icons.os, 'OS', s.os, s.arch);
  card(icons.kernel, 'Kernel', s.kernel);

  section('Processor');
  card(icons.cpu, 'CPU', s.cpu_model, s.cpu_cores + 'C / ' + s.cpu_threads + 'T' + (s.cpu_freq_mhz ? ' \u00b7 ' + s.cpu_freq_mhz + ' MHz' : ''), true);
  if (s.gpu) card(icons.gpu, 'GPU', s.gpu, null, true);

  section('Resources');
  card(icons.mem, 'Memory', s.memory_gb + ' GB');
  card(icons.load, 'Load avg', s.load);
  card(icons.uptime, 'Uptime', s.uptime);
  if (s.swap_gb > 0) card(icons.mem, 'Swap', s.swap_gb + ' GB');

  section('Storage');
  (s.disks || []).forEach(dk => {
    card(icons.disk, dk.mount, dk.total_gb + ' GB', dk.device + ' \u00b7 ' + dk.fs);
  });
}

// ── Live graphs ──
function drawArea(canvas, data, max, color) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  if (!data.length) return;
  const m = max || Math.max(...data, 1);
  ctx.strokeStyle = '#3d3929'; ctx.lineWidth = 0.5;
  for (let i = 1; i <= 3; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(0, h);
  data.forEach((v, i) => {
    ctx.lineTo((i / (data.length - 1)) * w, h - (v / m) * (h - 2));
  });
  ctx.lineTo(w, h);
  ctx.closePath();
  ctx.fillStyle = color.replace(')', ', 0.15)').replace('rgb', 'rgba');
  ctx.fill();
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - (v / m) * (h - 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function drawDual(canvas, d1, d2, color1, color2) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  if (!d1.length) return;
  const m = Math.max(...d1, ...d2, 1);
  ctx.strokeStyle = '#3d3929'; ctx.lineWidth = 0.5;
  for (let i = 1; i <= 3; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  [d1, d2].forEach((data, di) => {
    const col = di === 0 ? color1 : color2;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - (v / m) * (h - 2);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  });
}

// ── Right panel ──
let _rpTimer = null;
function toggleRightPanel() {
  const rp = document.getElementById('right-panel');
  const isMobile = isTouchDevice;
  if (isMobile) {
    rp.classList.toggle('open');
  } else {
    rp.classList.toggle('collapsed');
  }
  setTimeout(() => { if (typeof fitAddon !== 'undefined') fitAddon.fit(); }, 200);
  startRightPanel();
}
// Right panel tab switching
function switchRpTab(tab) {
  document.querySelectorAll('.rp-tab').forEach(t => t.classList.toggle('active', t.textContent.toLowerCase() === tab));
  document.getElementById('rp-content').style.display = tab === 'stats' ? '' : 'none';
  document.getElementById('rp-files').style.display = tab === 'files' ? '' : 'none';
  if (tab === 'files') loadFiles(filePaths.get(currentServer)||'~');
}

// File browser
let _currentFilePath='~',fileRequest=0;
const filePaths=new Map();
async function loadFiles(path){
 const server=currentServer,request=++fileRequest;
 const list=document.getElementById('file-list');
 if(!server){list.textContent='Choose a server to browse files.';return;}
 _currentFilePath=path;filePaths.set(server,path);
 document.getElementById('file-path-text').textContent=server+' · '+path;
 list.textContent='Loading…';
 try{
  const data=await checkedRequest('/api/'+encodeURIComponent(server)+'/files?path='+encodeURIComponent(path));
  if(server!==currentServer||request!==fileRequest)return;
  filePaths.set(server,data.path);_currentFilePath=data.path;
  document.getElementById('file-path-text').textContent=server+' · '+data.path;
  list.replaceChildren();
  function directory(label,target){
   const button=document.createElement('button');button.className='file-item';button.textContent=label;
   button.addEventListener('click',()=>loadFiles(target));list.appendChild(button);
  }
  if(data.path!=='/')directory('↑ Parent folder',data.path.split('/').slice(0,-1).join('/')||'/');
  data.entries.forEach(e=>{
   const full=data.path.replace(/\/$/,'')+'/'+e.name;
   if(e.type==='dir'){directory('📁 '+e.name,full);return;}
   const row=document.createElement('div');row.className='file-item';
   const name=document.createElement('span');name.className='fi-name';name.textContent=e.name;
   const copy=document.createElement('button');copy.textContent='Copy path';copy.setAttribute('aria-label','Copy path for '+e.name);copy.addEventListener('click',()=>copyText(full));
   row.append(name,copy);list.appendChild(row);
  });
  if(!data.entries.length)list.append('Empty directory');
 }catch(e){if(server===currentServer&&request===fileRequest)list.textContent='Could not load files: '+e.message;}
}

function startRightPanel() {
  if (_rpTimer) clearInterval(_rpTimer);
  updateRightPanel();
  _rpTimer = setInterval(updateRightPanel, 5000);
}
let _rpSpecs = null;  // cached specs for gauges
let metricRequest=0;
let _rpSpecsServer = '';  // which server we fetched specs for

function gaugeColor(pct) {
  if (pct < 60) return '#5ba97b';
  if (pct < 85) return '#d4a04a';
  return '#c94a3e';
}

function gaugeRing(id, pct) {
  const r = 21, circ = 2 * Math.PI * r;
  const offset = circ * (1 - pct / 100);
  const color = gaugeColor(pct);
  return '<div class="gauge-ring"><svg viewBox="0 0 52 52">' +
    '<circle class="gauge-bg" cx="26" cy="26" r="' + r + '"/>' +
    '<circle class="gauge-fg" id="gfg-' + id + '" cx="26" cy="26" r="' + r + '" ' +
    'stroke="' + color + '" stroke-dasharray="' + circ.toFixed(1) + '" ' +
    'stroke-dashoffset="' + offset.toFixed(1) + '"/>' +
    '</svg><div class="gauge-pct" id="gpct-' + id + '">' + Math.round(pct) + '%</div></div>';
}

function updateGauge(id, pct) {
  const r = 21, circ = 2 * Math.PI * r;
  const offset = circ * (1 - pct / 100);
  const color = gaugeColor(pct);
  const fg = document.getElementById('gfg-' + id);
  const label = document.getElementById('gpct-' + id);
  if (fg) { fg.style.strokeDashoffset = offset.toFixed(1); fg.style.stroke = color; }
  if (label) label.textContent = Math.round(pct) + '%';
}

function updateRightPanel() {
  const rp = document.getElementById('rp-content');
  const panel = document.getElementById('right-panel');
  if (panel.classList.contains('collapsed') || (isTouchDevice && !panel.classList.contains('open'))) return;
  if (!currentServer) return;

  const server=currentServer,request=++metricRequest;
  const base = '/api/' + encodeURIComponent(server);

  // Fetch specs if we haven't for this server
  if (_rpSpecsServer !== currentServer) {
    _rpSpecs = null;
    _rpSpecsServer = server;
    fetch(base + '/specs').then(r => {if(!r.ok)throw new Error('Unavailable');return r.json();}).then(s => {
      if(server!==currentServer)return;
      _rpSpecs = s;
      updateRightPanel();
    }).catch(() => {if(server===currentServer && request===metricRequest)rp.textContent='Metrics unavailable. Retrying…';});
  }

  fetch(base + '/metrics').then(r => {if(!r.ok)throw new Error('Unavailable');return r.json();}).then(m => {
    if(server!==currentServer||request!==metricRequest)return;
    const ico = (d) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + d + '</svg>';
    const cpuI = ico('<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/><rect x="8" y="8" width="8" height="8" rx="1"/>');
    const memI = ico('<path d="M6 19v-8a6 6 0 1 1 12 0v8"/><line x1="2" y1="19" x2="22" y2="19"/>');
    const gpuI = ico('<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h2M10 12h2M14 12h2"/>');
    const netI = ico('<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1"/>');
    const diskI = ico('<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>');

    const c = m.current;
    const builtFor = rp.dataset.builtFor || '';
    const sessNames = Object.keys(m.sessions || {});
    const specsKey = _rpSpecs ? _rpSpecs.hostname : '';
    const buildKey = currentSession + '|' + sessNames.join(',') + '|' + specsKey + '|' + (m.has_gpu ? '1' : '0');
    const needsBuild = !rp.querySelector('#gfg-cpu') || builtFor !== buildKey;

    if (needsBuild) {
      // Preserve accordion collapsed state
      const sysAccOpen = rp.querySelector('#acc-sys') ? !rp.querySelector('#acc-sys').classList.contains('collapsed') : false;
      const sessAccOpen = rp.querySelector('#acc-sess') ? !rp.querySelector('#acc-sess').classList.contains('collapsed') : false;

      let h = '';

      // ── Gauges ──
      const cpuPct = c.cpu || 0;
      const memPct = c.memory || 0;
      h += '<div class="gauge-grid">';
      h += '<div class="gauge-card">' + gaugeRing('cpu', cpuPct) +
        '<div class="gauge-label">CPU</div></div>';
      h += '<div class="gauge-card">' + gaugeRing('mem', memPct) +
        '<div class="gauge-label">RAM</div></div>';

      if (m.has_gpu) {
        const gpuPct = c.gpu_util || 0;
        h += '<div class="gauge-card">' + gaugeRing('gpu', gpuPct) +
          '<div class="gauge-label">GPU</div>' +
          '<div class="gauge-sub" id="gpu-sub">' + (c.gpu_temp ? c.gpu_temp.toFixed(0) + '\u00b0C' : '') + '</div></div>';
      }

      // Storage gauges — one per disk from specs
      if (_rpSpecs && _rpSpecs.disks) {
        _rpSpecs.disks.forEach((dk, i) => {
          const pct = dk.percent || 0;
          const label = dk.mount === '/' ? 'Root' : dk.mount.split('/').pop() || dk.mount;
          h += '<div class="gauge-card">' + gaugeRing('disk' + i, pct) +
            '<div class="gauge-label">' + label + '</div>' +
            '<div class="gauge-sub">' + dk.used_gb + '/' + dk.total_gb + ' GB</div></div>';
        });
      }
      h += '</div>';

      // ── Over-time accordion: System ──
      h += '<div class="rp-divider"></div>';
      h += '<div class="rp-accordion' + (sysAccOpen ? '' : ' collapsed') + '" id="acc-sys" onclick="this.classList.toggle(\'collapsed\')"><span class="rp-arrow">&#9660;</span> System over time</div>';
      h += '<div class="rp-accordion-body">';
      function graph(id, icon, label, legend) {
        h += '<div class="rp-graph"><div class="rp-row"><div class="rp-label">' + icon + label + '</div><div class="rp-val" id="rpv-' + id + '"></div></div>';
        h += '<canvas class="rp-canvas" id="rpc-' + id + '"></canvas>';
        if (legend) h += '<div class="rp-legend">' + legend + '</div>';
        h += '</div>';
      }
      graph('cpu', cpuI, 'CPU');
      graph('mem', memI, 'Memory');
      if (m.has_gpu) graph('gpu', gpuI, 'GPU', '<span class="leg-primary">util</span><span class="leg-secondary">temp</span>');
      graph('net', netI, 'Network', '<span class="leg-primary">rx</span><span class="leg-secondary">tx</span>');
      graph('disk', diskI, 'Disk I/O', '<span class="leg-primary">read</span><span class="leg-secondary">write</span>');
      h += '</div>';

      // ── Over-time accordion: Sessions ──
      if (sessNames.length) {
        h += '<div class="rp-divider"></div>';
        h += '<div class="rp-accordion' + (sessAccOpen ? '' : ' collapsed') + '" id="acc-sess" onclick="this.classList.toggle(\'collapsed\')"><span class="rp-arrow">&#9660;</span> Sessions (' + sessNames.length + ')</div>';
        h += '<div class="rp-accordion-body">';
        // Current session first
        if (currentSession && m.sessions && m.sessions[currentSession]) {
          h += '<div class="rp-graph"><div class="rp-row"><div class="rp-label"><span style="color:#8b8579">#</span> ' + currentSession + '</div><div class="rp-val" id="rpsv-' + currentSession + '"></div></div>';
          h += '<canvas class="rp-canvas" id="rpsc-' + currentSession + '"></canvas>';
          h += '<div class="rp-legend"><span class="leg-primary">cpu</span><span class="leg-secondary">mem</span></div></div>';
        }
        sessNames.filter(n => n !== currentSession).forEach(name => {
          h += '<div class="rp-graph"><div class="rp-row"><div class="rp-label"><span style="color:#8b8579">#</span> ' + name + '</div><div class="rp-val" id="rpsv-' + name + '"></div></div>';
          h += '<canvas class="rp-canvas" id="rpsc-' + name + '"></canvas>';
          h += '<div class="rp-legend"><span class="leg-primary">cpu</span><span class="leg-secondary">mem</span></div></div>';
        });
        h += '</div>';
      }

      rp.innerHTML = h;
      rp.dataset.builtFor = buildKey;
    } else {
      // Just update gauge values (no rebuild)
      updateGauge('cpu', c.cpu || 0);
      updateGauge('mem', c.memory || 0);
      if (m.has_gpu) {
        updateGauge('gpu', c.gpu_util || 0);
        const gpuSub = document.getElementById('gpu-sub');
        if (gpuSub) gpuSub.textContent = (c.gpu_temp ? c.gpu_temp.toFixed(0) + '\u00b0C' : '');
      }
    }

    // Update over-time charts (only if accordion is open)
    const sysBody = document.getElementById('acc-sys');
    if (sysBody && !sysBody.classList.contains('collapsed')) {
      const el = (id) => document.getElementById(id);
      if (el('rpv-cpu')) el('rpv-cpu').textContent = c.cpu.toFixed(1) + '%';
      if (el('rpv-mem')) el('rpv-mem').textContent = c.memory.toFixed(1) + '%';
      if (el('rpv-net')) el('rpv-net').textContent = '\u2193' + c.net_rx + ' \u2191' + c.net_tx;
      if (el('rpv-disk')) el('rpv-disk').textContent = 'R ' + c.disk_read + ' W ' + c.disk_write;
      if (el('rpc-cpu')) drawArea(el('rpc-cpu'), m.cpu, 100, 'rgb(218, 119, 86)');
      if (el('rpc-mem')) drawArea(el('rpc-mem'), m.memory, 100, 'rgb(218, 119, 86)');
      if (m.has_gpu && el('rpc-gpu')) {
        if (el('rpv-gpu')) el('rpv-gpu').textContent = c.gpu_util.toFixed(0) + '% \u00b7 ' + c.gpu_temp.toFixed(0) + '\u00b0C';
        drawDual(el('rpc-gpu'), m.gpu_util, m.gpu_temp, 'rgb(218, 119, 86)', 'rgb(91, 169, 123)');
      }
      if (el('rpc-net')) drawDual(el('rpc-net'), m.net_rx, m.net_tx, 'rgb(218, 119, 86)', 'rgb(91, 169, 123)');
      if (el('rpc-disk')) drawDual(el('rpc-disk'), m.disk_read, m.disk_write, 'rgb(218, 119, 86)', 'rgb(91, 169, 123)');
    }

    // Session charts
    const sessBody = document.getElementById('acc-sess');
    if (sessBody && !sessBody.classList.contains('collapsed')) {
      Object.keys(m.sessions || {}).forEach(name => {
        const sd = m.sessions[name];
        const valEl = document.getElementById('rpsv-' + name);
        const canvasEl = document.getElementById('rpsc-' + name);
        if (valEl) valEl.textContent = sd.current_cpu.toFixed(1) + '% \u00b7 ' + sd.current_mem.toFixed(0) + ' MB';
        if (canvasEl) drawDual(canvasEl, sd.cpu, sd.mem, 'rgb(218, 119, 86)', 'rgb(91, 169, 123)');
      });
    }
  }).catch(() => {if(server===currentServer && request===metricRequest)rp.textContent='Metrics unavailable. Retrying…';});
}

setTimeout(startRightPanel, 2000);

function goGeneral() {
  const oldSession = currentSession;
  closeConnection();
  currentSession = '';
  saveTerminalTabs();
  renderTerminalTabs();
  prevConnected = false;
  if (oldSession && presenceData[oldSession]) {
    presenceData[oldSession].viewers = presenceData[oldSession].viewers.filter(v => v.id !== clientId);
  }
  term.clear(); term.reset();
  document.getElementById('conn-header').classList.remove('visible');
  document.getElementById('conn-divider').classList.remove('visible');
  document.querySelector('.status-pip').className = 'status-pip';
  isMuted = false;
  document.getElementById('mic-btn').classList.remove('barred');
  renderSidebar();
  showServerLanding();
  closeSidebar();
}
function hideServerLanding() {
  document.getElementById('server-landing').classList.remove('visible');
  document.getElementById('terminal-container').classList.remove('hidden');
  document.getElementById('welcome-item').classList.remove('active');
  document.getElementById('mobile-controls').classList.add('visible');
  requestAnimationFrame(() => { fitAddon.fit(); updateJumpButton(); });
}

function renderHomeCards() {
  const cards = document.getElementById('home-cards');
  cards.innerHTML = '';
  cachedServerList.forEach(srv => {
    const card = document.createElement('div');
    card.className = 'server-card' + (srv.name === currentServer ? ' current' : '') + (srv.connected ? '' : ' offline');
    card.innerHTML = '<div class="sc-name">' + srv.name +
      ' <span style="font-size:10px;color:' + (srv.connected ? '#5ba97b' : '#c94a3e') + '">' +
      (srv.connected ? 'connected' : 'offline') + '</span></div>' +
      '<div class="sc-url">' + (srv.sessions ? srv.sessions.length + ' sessions' : 'no sessions') + '</div>';
    if (srv.connected) card.addEventListener('click', () => enterServer(srv.name));
    else card.style.opacity = '0.5';
    cards.appendChild(card);
  });
}

function safeTerminalURL(value) {
  try {
    const url=new URL(value.startsWith('www.')?'https://'+value:value);
    return ['http:','https:'].includes(url.protocol)?url.href:null;
  } catch (_) {return null;}
}
function openTerminalLink(event, value) {
  if(event.button!==0 || displayPointer?.moved || displayPointer?.forwarded || displayPointer?.opened)return;
  const url=safeTerminalURL(value);
  if(!url){notifyUser('Only web links can be opened here.');return;}
  event.preventDefault();
  if(displayPointer)displayPointer.opened=true;
  window.open(url,'_blank','noopener,noreferrer');
}
function openPlainTerminalLink(event) {
  const url=displayURLAt(event.clientX,event.clientY);
  if(url && url===displayPointer?.url)openTerminalLink(event,url);
}
function hoverTerminalLink(event, value) {
  document.getElementById('terminal-container').title=safeTerminalURL(value)||'Unsupported link';
}
function leaveTerminalLink(){document.getElementById('terminal-container').removeAttribute('title');}
function hoverOSCLink(event,value){displayHoveredOSC=safeTerminalURL(value);hoverTerminalLink(event,value);}
function leaveOSCLink(){displayHoveredOSC=null;leaveTerminalLink();}
const term = new Terminal({
  theme: {
    background: '#1a1915', foreground: '#e8e4db',
    cursor: '#da7756', cursorAccent: '#1a1915',
    selectionBackground: 'rgba(218,119,86,0.45)',
    selectionInactiveBackground: 'rgba(218,119,86,0.35)',
    black: '#1a1915', red: '#c94a3e', green: '#5ba97b', yellow: '#d4a04a',
    blue: '#6b8cce', magenta: '#b07eb5', cyan: '#5ba9a9', white: '#e8e4db',
    brightBlack: '#8b8579', brightRed: '#da7756', brightGreen: '#6bc28e',
    brightYellow: '#e0b85c', brightBlue: '#839dda', brightMagenta: '#c490c8',
    brightCyan: '#6dc0c0', brightWhite: '#f5f0e8',
  },
  fontFamily: '"DejaVu Sans Mono", "Fira Code", "SF Mono", monospace',
  fontSize: Math.max(10, Math.min(24, Number(localStorage.getItem('terminalFontSize')) || 14)), cursorBlink: true, allowProposedApi: true,
  scrollback: 5000, convertEol: false, scrollOnUserInput: true,
  macOptionClickForcesSelection: true,
  altClickMovesCursor: false,
  linkHandler: {activate:openTerminalLink,hover:hoverOSCLink,leave:leaveOSCLink},
});

const fitAddon = new FitAddon.FitAddon();
term.loadAddon(fitAddon);
term.loadAddon(new WebLinksAddon.WebLinksAddon(openPlainTerminalLink,{hover:hoverTerminalLink,leave:leaveTerminalLink}));
term.open(document.getElementById('terminal-container'));
fitAddon.fit();

// Browser selection owns ordinary mouse gestures, including in mouse-aware
// apps. Use xterm's supported force-selection modifiers on the same DOM event
// so word/line selection, wide characters, autoscroll, and native Copy survive.
// Alt/Option deliberately passes a gesture to the application instead.
let displayPointer=null,displayHover=null,displayPlainHover=null,displayHoveredOSC=null,refreshingDisplayLink=false;
const displayIsMac=/Mac/.test(navigator.platform);
function displayMouseModifiers(event,forwarded=false) {
  if(term.modes.mouseTrackingMode==='none')return;
  Object.defineProperties(event,{
    altKey:{configurable:true,value:!forwarded&&displayIsMac},
    shiftKey:{configurable:true,value:!forwarded&&!displayIsMac},
  });
}
function displayURLAt(x,y) {
  const screen=document.querySelector('.xterm-screen'),rect=screen.getBoundingClientRect();
  if(x<rect.left||x>=rect.right||y<rect.top||y>=rect.bottom)return null;
  const buffer=term.buffer.active,row=buffer.viewportY+Math.floor((y-rect.top)/rect.height*term.rows);
  const col=Math.min(term.cols-1,Math.floor((x-rect.left)/rect.width*term.cols));
  let first=row,last=row;
  while(first>0 && row-first<16 && buffer.getLine(first)?.isWrapped)first--;
  while(last+1<buffer.length && last-row<16 && buffer.getLine(last+1)?.isWrapped)last++;
  let text='',hit=0;
  for(let i=first;i<=last;i++){
    const line=buffer.getLine(i);if(!line)return null;
    if(i===row)hit=text.length+line.translateToString(false,0,col).length;
    text+=line.translateToString(false);
  }
  for(const match of text.matchAll(/\b(?:https?:\/\/|www\.)[^\s<>"'\x00-\x1f]+/gi)){
    let label=match[0].replace(/[.,;:!?]+$/,'');
    while(label.endsWith(')')&&(label.match(/\)/g)||[]).length>(label.match(/\(/g)||[]).length)label=label.slice(0,-1);
    if(hit>=match.index&&hit<match.index+label.length)return safeTerminalURL(label);
  }
  return null;
}
const displayContainer=document.getElementById('terminal-container');
function refreshDisplayLink(event) {
  // xterm 5.5 caches even empty link results on a row. Refresh its hover on
  // press, before it records the click target, using DOM events only.
  // A pressed button suppresses the app's hover-motion reporting; no app drag
  // listeners have been installed yet. Our gesture bookkeeping ignores these.
  const screen=document.querySelector('.xterm-screen'),rect=screen.getBoundingClientRect();
  const height=rect.height/term.rows,row=Math.floor((event.clientY-rect.top)/height);
  if(event.clientX<rect.left||event.clientX>=rect.right||row<0||row>=term.rows)return;
  refreshingDisplayLink=true;displayHoveredOSC=null;
  try {
    for(const y of [rect.top+((row+1)%term.rows+.5)*height,event.clientY]){
      screen.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:event.clientX,clientY:y,buttons:1}));
    }
  } finally {refreshingDisplayLink=false;}
}
displayContainer.addEventListener('mousedown',event=>{
  if(event.button===2 && term.hasSelection()){displayMouseModifiers(event);return;}
  if(event.button!==0)return;
  displayPointer={x:event.clientX,y:event.clientY,active:true,moved:false,forwarded:event.altKey,opened:false,
    url:displayURLAt(event.clientX,event.clientY)};
  if(!displayPointer.forwarded){refreshDisplayLink(event);displayPointer.oscURL=displayHoveredOSC;}
  displayMouseModifiers(event,displayPointer.forwarded);
},true);
document.addEventListener('mousemove',event=>{
  if(refreshingDisplayLink || !displayPointer?.active || !(event.buttons&1))return;
  if(Math.hypot(event.clientX-displayPointer.x,event.clientY-displayPointer.y)>4)displayPointer.moved=true;
  displayMouseModifiers(event,displayPointer.forwarded);
},true);
document.addEventListener('mouseup',event=>{
  if(event.button!==0 || !displayPointer?.active)return;
  if(Math.hypot(event.clientX-displayPointer.x,event.clientY-displayPointer.y)>4)displayPointer.moved=true;
  displayMouseModifiers(event,displayPointer.forwarded);
  displayPointer.active=false;
  const url=displayURLAt(event.clientX,event.clientY);
  if(displayPointer.oscURL)openTerminalLink(event,displayPointer.oscURL);
  else if(url && url===displayPointer.url)openTerminalLink(event,url);
},true);
displayContainer.addEventListener('click',event=>{
  if(event.button!==0 || !displayPointer || displayPointer.moved || displayPointer.forwarded || displayPointer.opened)return;
  const url=displayURLAt(event.clientX,event.clientY);
  if(url && url===displayPointer.url){event.stopImmediatePropagation();openTerminalLink(event,url);}
},true);
function updateDisplayHover() {
  if(!displayHover)return;
  const url=displayURLAt(displayHover.x,displayHover.y);
  const screen=document.querySelector('.xterm-screen');
  screen.style.cursor=url?'pointer':'';
  if(url)displayContainer.title=url;
  else if(displayContainer.title===displayPlainHover)leaveTerminalLink();
  displayPlainHover=url;
}
displayContainer.addEventListener('mousemove',event=>{
  if(refreshingDisplayLink)return;
  displayHover={x:event.clientX,y:event.clientY};updateDisplayHover();
},{passive:true});
displayContainer.addEventListener('mouseleave',()=>{
  displayHover=null;document.querySelector('.xterm-screen').style.cursor='';leaveTerminalLink();
});
term.onWriteParsed(updateDisplayHover);
window.addEventListener('blur',()=>{displayPointer=null;});

// Shared terminal input and accessible controls.
let modifiers = {ctrl: false, alt: false};
let kbdOpen = false;
let noticeTimer = null;
let cancelTerminalGesture = () => {};
const draftBySession = new Map();
let pasteTarget = null;
let historyRequest = 0;
const dialogFocus = new Map();
const sessionKey = () => currentServer + '/' + currentSession;
const terminalLinks = new Map();
term.parser.registerOscHandler(8,data=>{
  const split=data.indexOf(';'),url=split<0?null:safeTerminalURL(data.slice(split+1));
  if(url && currentSession){
    const key=sessionKey(),links=terminalLinks.get(key)||new Set();
    links.delete(url);links.add(url);
    if(links.size>100)links.delete(links.values().next().value);
    terminalLinks.delete(key);terminalLinks.set(key,links);
    if(terminalLinks.size>20)terminalLinks.delete(terminalLinks.keys().next().value);
  }
  return false; // Preserve xterm's normal OSC-8 rendering and activation.
});
function notifyUser(message) {
  const el = document.getElementById('notice');
  el.textContent = message; el.classList.add('visible');
  if(document.getElementById('history-modal').open)document.getElementById('history-status').textContent=message;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => el.classList.remove('visible'), 5000);
}
function canSend() { return !!(ws && ws.readyState === WebSocket.OPEN && hasInput && currentSession); }
function sendKey(seq, quiet = false) {
  if (!canSend()) {
    if (!quiet) notifyUser(!ws || ws.readyState !== WebSocket.OPEN ? 'Not connected. Your input was not sent.' : 'Viewing only. Take control to type.');
    return false;
  }
  ws.send(seq); return true;
}
function updateInputState() {
  const connected = !!(ws && ws.readyState === WebSocket.OPEN && currentSession);
  const label = connected ? (hasInput ? 'You have control' : 'Viewing only — take control to type') : 'Disconnected';
  document.getElementById('input-state').textContent = label;
  document.querySelectorAll('[data-terminal-key],#ctrl-key,#alt-key,#kbd-btn').forEach(b => b.disabled = !connected || !hasInput);
  const claim = document.getElementById('conn-claim');
  claim.title = hasInput ? 'Release control' : 'Take control';
  claim.setAttribute('aria-label', claim.title);
  claim.disabled = !connected;
  claim.setAttribute('aria-pressed', String(hasInput && connected));
  document.getElementById('mic-btn').setAttribute('aria-pressed', String(isMuted));
  if (!hasInput || !connected) { resetModifiers(); cancelTerminalGesture(); }
}
function resetModifiers() {
  modifiers = {ctrl:false, alt:false};
  ['ctrl','alt'].forEach(k=>document.getElementById(k+'-key').setAttribute('aria-pressed','false'));
}
function toggleModifier(key) {
  modifiers[key] = !modifiers[key];
  document.getElementById(key+'-key').setAttribute('aria-pressed',String(modifiers[key]));
  if (modifiers[key]) notifyUser(key.toUpperCase() + ' applies to the next key');
  if (kbdOpen) term.focus();
}
function applyModifiers(data) {
  if (modifiers.ctrl && data.length === 1) {
    const ch = data.toUpperCase().charCodeAt(0);
    if (ch >= 64 && ch <= 95) data = String.fromCharCode(ch - 64);
    else if (data === ' ') data = '\x00';
    else if (data === '?') data = '\x7f';
  }
  if (modifiers.alt) data = '\x1b' + data;
  resetModifiers(); return data;
}
function sendTerminalKey(key) {
  const app = term.modes.applicationCursorKeysMode;
  const arrows = {ArrowUp:'A',ArrowDown:'B',ArrowRight:'C',ArrowLeft:'D',Home:'H',End:'F'};
  const modified = modifiers.ctrl || modifiers.alt;
  let seq;
  if (arrows[key]) {
    const mod = 1 + (modifiers.alt ? 2 : 0) + (modifiers.ctrl ? 4 : 0);
    seq = modified ? '\x1b[1;' + mod + arrows[key] : '\x1b' + (app ? 'O' : '[') + arrows[key];
    resetModifiers();
  } else {
    seq = {Escape:'\x1b',Tab:'\t',ShiftTab:'\x1b[Z',Enter:'\r',Backspace:'\x7f',Delete:'\x1b[3~',PageUp:'\x1b[5~',PageDown:'\x1b[6~'}[key];
    seq = applyModifiers(seq || key);
  }
  sendKey(seq);
}
function toggleKeyboard() {
  if (!canSend()) return;
  if (document.activeElement === term.textarea) term.blur(); else term.focus();
}
term.textarea.setAttribute('autocapitalize','off');
term.textarea.setAttribute('autocomplete','off');
term.textarea.setAttribute('autocorrect','off');
term.textarea.spellcheck = false;
term.textarea.addEventListener('focus',()=>{kbdOpen=true;document.getElementById('kbd-btn').setAttribute('aria-pressed','true');});
term.textarea.addEventListener('blur',()=>{kbdOpen=false;document.getElementById('kbd-btn').setAttribute('aria-pressed','false');});
function checkEscOverflow() { /* The compact grid keeps all keys within the viewport. */ }
function openDialog(id) {
  cancelTerminalGesture();
  const dialog = document.getElementById(id);
  dialogFocus.set(id, document.activeElement);
  if (!dialog.open) dialog.showModal();
}
function closeDialog(id) { document.getElementById(id).close(); }
document.querySelectorAll('dialog').forEach(dialog=>{
  dialog.addEventListener('close',()=>{
    if(dialog.open)return; // A queued close must not cancel a newly opened view.
    if (dialog.id === 'paste-modal') savePasteDraft();
    if (dialog.id === 'history-modal') historyRequest++;
    const previous=dialogFocus.get(dialog.id);
    if(previous?.isConnected && (document.activeElement===document.body || dialog.contains(document.activeElement)))previous.focus({preventScroll:true});
  });
});
function savePasteDraft() {
  if (pasteTarget) draftBySession.set(pasteTarget.key,document.getElementById('paste-area').value);
}
function showPasteModal() {
  if (!currentSession) {notifyUser('Choose a session first.');return;}
  savePasteDraft();
  pasteTarget={key:sessionKey(),server:currentServer,session:currentSession};
  document.getElementById('paste-area').value=draftBySession.get(pasteTarget.key)||'';
  document.getElementById('paste-target').textContent=currentServer+' / '+currentSession;
  document.getElementById('paste-status').textContent='';
  openDialog('paste-modal');document.getElementById('paste-area').focus();
}
function closePasteModal(){savePasteDraft();closeDialog('paste-modal');}
function sendPaste() {
  savePasteDraft();
  const status=document.getElementById('paste-status');
  if (!pasteTarget || pasteTarget.key!==sessionKey()) {status.textContent='This draft belongs to another session. Return to it before inserting.';return;}
  if (!canSend()) {status.textContent='Draft saved. Reconnect or take control before inserting.';return;}
  const text=document.getElementById('paste-area').value;
  if (!text) {status.textContent='Enter text first.';return;}
  resetModifiers();term.paste(text);
  document.getElementById('paste-area').value='';savePasteDraft();closeDialog('paste-modal');
  notifyUser('Text inserted.');
}
document.getElementById('paste-area').addEventListener('input',savePasteDraft);
async function copyText(text) {
  if (!text) {notifyUser('Select some text first.');return false;}
  try {await navigator.clipboard.writeText(text);notifyUser('Copied.');return true;}
  catch (_) {
    // Older browsers and private HTTP access may lack the Clipboard API.
    const previous=document.activeElement,selection=window.getSelection(),ranges=[];
    for(let i=0;i<selection.rangeCount;i++)ranges.push(selection.getRangeAt(i).cloneRange());
    const area=document.createElement('textarea');area.value=text;area.setAttribute('readonly','');
    area.style.cssText='position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;';
    (document.querySelector('dialog[open]')||document.body).appendChild(area);
    let copied=false;
    try {area.select();copied=document.execCommand('copy');} catch (_) {}
    finally {
      area.remove();if(previous?.isConnected)previous.focus({preventScroll:true});
      selection.removeAllRanges();ranges.forEach(r=>selection.addRange(r));
    }
    notifyUser(copied?'Copied.':'Use ⌘C / Ctrl+C or your device’s Copy action on the selected text.');
    return copied;
  }
}
function localHistory() {
  const b=term.buffer.active;const lines=[];
  for(let i=0;i<b.length;i++){
    const line=b.getLine(i);if(!line)continue;
    const text=line.translateToString(true);
    if(line.isWrapped && lines.length)lines[lines.length-1]+=text;else lines.push(text);
  }
  return lines.join('\n');
}
let historyText='',historyInteracted=false,pendingHistoryText=null,historyFindEnd=0,historyFindQuery='',historyExtraLinks=[];
function renderHistory(text) {
  historyText=text;historyFindEnd=0;historyFindQuery='';
  const area=document.getElementById('history-text'),list=document.getElementById('history-link-list');
  area.replaceChildren();list.replaceChildren();
  const links=new Map();let offset=0;
  const pattern=/\b(?:https?:\/\/|www\.)[^\s<>"'\x00-\x1f]+/gi;
  for(const match of text.matchAll(pattern)){
    let label=match[0].replace(/[.,;:!?]+$/,'');
    while(label.endsWith(')') && (label.match(/\)/g)||[]).length>(label.match(/\(/g)||[]).length)label=label.slice(0,-1);
    const url=safeTerminalURL(label);if(!url)continue;
    area.append(document.createTextNode(text.slice(offset,match.index)));
    const a=document.createElement('a');a.textContent=label;a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.title=url;
    area.append(a);offset=match.index+label.length;links.set(url,label);
  }
  area.append(document.createTextNode(text.slice(offset)));
  historyExtraLinks.forEach(url=>{if(!links.has(url))links.set(url,url);});
  for(const [url,label] of links){
    const a=document.createElement('a');a.href=url;a.textContent=label;a.target='_blank';a.rel='noopener noreferrer';a.title=url;list.append(a);
  }
  if(!links.size)list.textContent='No web links in the loaded text.';
  document.getElementById('history-links-title').textContent='Links from this session ('+links.size+')';
}
function historySelection() {
  const area=document.getElementById('history-text'),s=window.getSelection();
  return s && area.contains(s.anchorNode) && area.contains(s.focusNode)?s.toString():'';
}
function applyPendingHistory() {
  if(pendingHistoryText===null)return;
  renderHistory(pendingHistoryText);pendingHistoryText=null;historyInteracted=false;
  document.getElementById('history-load-latest').hidden=true;
  document.getElementById('history-text').scrollTop=document.getElementById('history-text').scrollHeight;
  document.getElementById('history-status').textContent='Full pane snapshot loaded. Drag to select, or press and hold on touch screens. Click a link to open it.';
}
async function showHistory(fromSwipe=false,linksOnly=false) {
  if (!currentSession) {notifyUser('Choose a session first.');return;}
  const request=++historyRequest,key=sessionKey(),server=currentServer,session=currentSession;
  const area=document.getElementById('history-text'),status=document.getElementById('history-status');
  historyInteracted=false;pendingHistoryText=null;historyExtraLinks=[...(terminalLinks.get(key)||[])];renderHistory(localHistory());document.getElementById('history-search').value='';
  document.getElementById('history-load-latest').hidden=true;
  document.getElementById('history-links').open=linksOnly;
  document.getElementById('history-title').textContent='Select & copy — '+session;
  status.textContent='Drag to select, or press and hold on touch screens. Loading full history…';openDialog('history-modal');
  // Keep the software keyboard closed while reading a read-only transcript.
  document.querySelector('#history-modal .dialog-actions button:last-child').focus();
  const position=()=>{area.scrollTop=Math.max(0,area.scrollHeight-area.clientHeight-(fromSwipe?area.clientHeight*0.5:0));};
  position();
  try {
    const r=await fetch('/api/'+encodeURIComponent(server)+'/history/'+encodeURIComponent(session));
    const data=await r.json();
    if(request!==historyRequest || key!==sessionKey() || !document.getElementById('history-modal').open)return;
    if(!r.ok || !data.ok)throw new Error(data.error||'unavailable');
    if(historyInteracted || historySelection()){
      pendingHistoryText=data.text;document.getElementById('history-load-latest').hidden=false;
      status.textContent='Your selection is kept. Full history is ready when you want to load it.';
    }else{
      renderHistory(data.text);status.textContent='Drag to select, or press and hold on touch screens. Click a link to open it. This snapshot stays still while you read.';position();
    }
  } catch (_) {
    if(request===historyRequest && key===sessionKey()) status.textContent='Showing locally received output. Older pane history needs the updated server.';
  }
}
function closeHistory(){closeDialog('history-modal');updateJumpButton();}
function copyHistory(all){copyText(all?historyText:historySelection());}
function findHistory(){
  const a=document.getElementById('history-text'),q=document.getElementById('history-search').value;
  if(!q)return;
  if(q!==historyFindQuery){historyFindEnd=0;historyFindQuery=q;}
  let index=historyText.toLowerCase().indexOf(q.toLowerCase(),historyFindEnd);
  if(index<0)index=historyText.toLowerCase().indexOf(q.toLowerCase());
  if(index<0){document.getElementById('history-status').textContent='No matches.';return;}
  historyFindEnd=index+q.length;historyInteracted=true;
  const range=document.createRange(),walker=document.createTreeWalker(a,NodeFilter.SHOW_TEXT);
  let offset=0,node,started=false;
  while(node=walker.nextNode()){
    const end=offset+node.length;
    if(!started && index<end){range.setStart(node,index-offset);started=true;}
    if(started && historyFindEnd<=end){range.setEnd(node,historyFindEnd-offset);break;}
    offset=end;
  }
  const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
  a.scrollTop+=range.getBoundingClientRect().top-a.getBoundingClientRect().top-a.clientHeight/3;
}
['pointerdown','touchstart','keydown'].forEach(type=>document.getElementById('history-text').addEventListener(type,()=>{historyInteracted=true;},{passive:true}));
document.querySelector('#history-modal .dialog-actions').addEventListener('mousedown',e=>{if(e.target.closest('button'))e.preventDefault();});
document.getElementById('history-search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();findHistory();}});
function openShortcuts(){openDialog('shortcut-modal');}
const shortcuts=[
 ['Ctrl+C','Interrupt','\x03'],['Ctrl+D','End input / exit','\x04'],['Ctrl+Z','Suspend','\x1a'],
 ['Ctrl+L','Clear screen','\x0c'],['Ctrl+R','Search command history','\x12'],['Ctrl+A','Start of line','\x01'],
 ['Ctrl+E','End of line','\x05'],['Ctrl+W','Delete previous word','\x17'],['Ctrl+U','Delete to line start','\x15'],
 ['Ctrl+K','Delete to line end','\x0b'],['Ctrl+B','tmux prefix','\x02'],['Ctrl+P','Previous command','\x10'],
 ['Ctrl+N','Next command','\x0e'],['Alt+B','Previous word','\x1bb'],['Alt+F','Next word','\x1bf'],
 ['Alt+Enter','Alternate Enter','\x1b\r'],['Shift+Tab','Previous completion','\x1b[Z'],
 ['Page Up','Page up',null,'PageUp'],['Page Down','Page down',null,'PageDown'],
 ['Home','Home',null,'Home'],['End','End',null,'End'],['Delete','Delete forward',null,'Delete'],
];
shortcuts.forEach(([label,description,seq,key])=>{
 const button=document.createElement('button');button.type='button';button.setAttribute('aria-label',label+' — '+description);
 const keycap=document.createElement('kbd');keycap.textContent=label;
 const hint=document.createElement('span');hint.textContent=description;button.append(keycap,hint);
 button.addEventListener('click',()=>{if(!canSend()){notifyUser('Take control of a connected session first.');return;}resetModifiers();if(key)sendTerminalKey(key);else sendKey(seq);closeDialog('shortcut-modal');});
 document.getElementById('shortcut-grid').appendChild(button);
});
function changeFontSize(delta){term.options.fontSize=Math.max(10,Math.min(24,term.options.fontSize+delta));localStorage.setItem('terminalFontSize',term.options.fontSize);fitAddon.fit();}
term.attachCustomKeyEventHandler(e=>{
 if(e.type!=='keydown')return true;
 const key=e.key.toLowerCase();
 if((e.metaKey||e.ctrlKey)&&!e.altKey&&key==='c'&&term.hasSelection()){
  e.preventDefault();copyText(term.getSelection());return false;
 }
 if((e.metaKey||e.ctrlKey)&&e.shiftKey&&['p','f','c','v'].includes(key)){
  e.preventDefault();
  if(key==='p')openShortcuts();else if(key==='f')showHistory();else if(key==='c')copyText(term.getSelection());else showPasteModal();
  return false;
 }
 return true;
});
// Preserve terminal focus when accessory keys are pressed.
const controls=document.getElementById('mobile-controls');
controls.addEventListener('pointerdown',e=>{if(e.target.closest('[data-terminal-key],.modifier,#kbd-btn') && kbdOpen)e.preventDefault();});
// Safari can still blur a textarea after a cancelled pointerdown. Cancel the
// native touch activation and click the same key on release to retain focus.
let touchedKey=null;
controls.addEventListener('touchstart',e=>{
 const button=e.target.closest('[data-terminal-key],.modifier,#kbd-btn');
 if(!button || button.disabled || e.touches.length!==1)return;
 e.preventDefault();touchedKey=button;
},{passive:false});
controls.addEventListener('touchend',e=>{
 if(!touchedKey)return;
 e.preventDefault();const button=touchedKey;touchedKey=null;stopKeyRepeat();
 const touch=e.changedTouches[0];
 if(touch && button.contains(document.elementFromPoint(touch.clientX,touch.clientY)))button.click();
},{passive:false});
controls.addEventListener('touchcancel',()=>{touchedKey=null;stopKeyRepeat();});
let repeatTimer=null,repeatInterval=null,repeated=false;
function stopKeyRepeat(){clearTimeout(repeatTimer);clearInterval(repeatInterval);repeatTimer=repeatInterval=null;}
document.querySelectorAll('[data-terminal-key]').forEach(button=>{
 button.addEventListener('click',()=>{if(repeated){repeated=false;return;}sendTerminalKey(button.dataset.terminalKey);});
 if(button.dataset.terminalKey!=='Backspace')return;
 button.addEventListener('pointerdown',e=>{
  if(e.button!==0 || !canSend())return;repeated=false;stopKeyRepeat();button.setPointerCapture(e.pointerId);
  repeatTimer=setTimeout(()=>{repeated=true;sendTerminalKey('Backspace');repeatInterval=setInterval(()=>sendKey('\x7f',true),70);},400);
 });
 ['pointerup','pointercancel','lostpointercapture'].forEach(type=>button.addEventListener(type,stopKeyRepeat));
});
window.addEventListener('blur',()=>{stopKeyRepeat();cancelTerminalGesture();resetModifiers();});
function triggerUpload(){if(!canSend()){notifyUser('Take control of a connected session before attaching an image.');return;}document.getElementById('img-upload').click();}
async function handleUpload(input){
 const file=input.files[0];if(!file)return;
 const target={server:currentServer,session:currentSession,key:sessionKey(),socket:ws};
 if(!canSend()){notifyUser('Take control before uploading.');input.value='';return;}
 const button=document.getElementById('image-btn');button.disabled=true;button.setAttribute('aria-busy','true');button.title='Uploading image…';
 try{
  const dataURL=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('Could not read image'));reader.readAsDataURL(file);});
  const r=await fetch('/api/'+encodeURIComponent(target.server)+'/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:file.name,data:dataURL.split(',')[1]})});
  const data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||'Upload failed');
  const reference='Attached image: '+data.path;
  if(target.key===sessionKey()&&target.socket===ws&&canSend()){
   resetModifiers();term.paste(reference);notifyUser('Image path inserted. Press Enter when ready.');
  }else{
   draftBySession.set(target.key,(draftBySession.get(target.key)||'')+reference);
   notifyUser('Image uploaded. Its reference is saved in Paste for '+target.session+'.');
  }
 }catch(e){notifyUser(e.message);}finally{input.value='';button.disabled=false;button.removeAttribute('aria-busy');button.title='Attach image';}
}
// Send one scroll RPC at a time and coalesce gestures while it is in flight.
let touchScrollPending=null,touchScrollInFlight=false,touchScrollTimer=null,remoteHistoryPane=null;
let wheelScrollDelta=0,wheelScrollKey='',wheelScrollTime=0;
function resetTouchScroll(){
 touchScrollPending=null;touchScrollInFlight=false;clearTimeout(touchScrollTimer);remoteHistoryPane=null;
 wheelScrollDelta=0;wheelScrollKey='';wheelScrollTime=0;
}
function flushTouchScroll(){
 if(touchScrollInFlight||!touchScrollPending||!canSend())return;
 const pending=touchScrollPending;touchScrollPending=null;
 if(pending.key!==sessionKey())return;
 const lines=Math.max(-24,Math.min(24,pending.lines));
 if(!lines)return;
 if(lines!==pending.lines)touchScrollPending={...pending,lines:pending.lines-lines};
 touchScrollInFlight=true;
 ws.send(JSON.stringify({type:'scroll',lines,col:pending.col,row:pending.row}));
 touchScrollTimer=setTimeout(()=>{
  touchScrollInFlight=false;touchScrollPending=null;
  notifyUser('Live scrolling is unavailable. Select / History can still show captured output.');
 },12000);
}
function queueTouchScroll(lines,x,y){
 const rect=document.querySelector('.xterm-screen').getBoundingClientRect();
 const col=Math.max(1,Math.min(term.cols,Math.floor((x-rect.left)/rect.width*term.cols)+1));
 const row=Math.max(1,Math.min(term.rows,Math.floor((y-rect.top)/rect.height*term.rows)+1));
 if(touchScrollPending && touchScrollPending.key===sessionKey()){
  touchScrollPending.lines=Math.max(-48,Math.min(48,touchScrollPending.lines+lines));
 }else touchScrollPending={key:sessionKey(),lines,col,row};
 flushTouchScroll();
}
function handleTouchScrollResult(msg){
 clearTimeout(touchScrollTimer);touchScrollInFlight=false;
 if(!msg.ok){touchScrollPending=null;notifyUser(msg.error||'Live scrolling failed. Try Select / History.');return;}
 remoteHistoryPane=msg.copy_mode?msg.pane:null;updateJumpButton();flushTouchScroll();
}
// Own desktop wheel events before xterm can translate an alternate-buffer
// scroll into Up/Down keys (which recall previous prompts in terminal apps).
document.getElementById('terminal-container').addEventListener('wheel',e=>{
 if(document.querySelector('dialog[open]'))return;
 e.stopImmediatePropagation();
 // Trackpad pinch zoom belongs to the browser, never to the terminal app.
 if(e.ctrlKey)return;
 if(e.cancelable)e.preventDefault();
 if(!e.deltaY||Math.abs(e.deltaX)>Math.abs(e.deltaY))return;
 const screen=document.querySelector('.xterm-screen');
 const rect=screen.getBoundingClientRect(),rowHeight=Math.max(12,rect.height/term.rows);
 const now=performance.now(),key=sessionKey();
 const delta=e.deltaY*(e.deltaMode===1?rowHeight:e.deltaMode===2?rect.height:1);
 if(key!==wheelScrollKey||now-wheelScrollTime>250||wheelScrollDelta*delta<0)wheelScrollDelta=0;
 wheelScrollKey=key;wheelScrollTime=now;wheelScrollDelta+=delta;
 const lines=Math.trunc(wheelScrollDelta/rowHeight);
 if(!lines)return;
 wheelScrollDelta-=lines*rowHeight;
 const remote=term.buffer.active.type==='alternate'||term.modes.mouseTrackingMode!=='none'||remoteHistoryPane!==null;
 if(remote){
  if(canSend())queueTouchScroll(lines,e.clientX,e.clientY);
  else if(lines<0&&currentSession)showHistory(true);
 }else term.scrollLines(lines);
},{capture:true,passive:false});
// One owner for touch scrolling, including Safari. RPCs select the pane's real mode.
(function(){
 const container=document.getElementById('terminal-container');let gesture=null;
 cancelTerminalGesture=()=>{gesture=null;touchScrollPending=null;stopKeyRepeat();};
 container.addEventListener('touchstart',e=>{
  if(e.touches.length!==1||document.querySelector('dialog[open]')){gesture=null;return;}
  const t=e.touches[0];gesture={x:t.clientX,y:t.clientY,startY:t.clientY,delta:0,key:sessionKey()};
  e.stopPropagation();
 },{capture:true,passive:true});
 container.addEventListener('touchmove',e=>{
  if(!gesture||e.touches.length!==1||gesture.key!==sessionKey()){gesture=null;return;}
  const t=e.touches[0];const dy=gesture.y-t.clientY;gesture.y=t.clientY;
  if(Math.abs(t.clientY-gesture.startY)<8)return;
  if(e.cancelable)e.preventDefault();e.stopImmediatePropagation();
  const remote=term.buffer.active.type==='alternate'||term.modes.mouseTrackingMode!=='none';
  if(remote && !canSend()){
   if(t.clientY>gesture.startY){gesture=null;showHistory(true);}return;
  }
  gesture.delta+=dy;
  const rowHeight=Math.max(12,document.querySelector('.xterm-screen').getBoundingClientRect().height/term.rows);
  const rows=Math.trunc(gesture.delta/rowHeight);
  if(rows){
   if(remote)queueTouchScroll(rows,gesture.x,t.clientY);else term.scrollLines(rows);
   gesture.delta-=rows*rowHeight;
  }
 },{capture:true,passive:false});
 ['touchend','touchcancel'].forEach(type=>container.addEventListener(type,()=>{gesture=null;},{passive:true}));
})();
const jumpBtn=document.getElementById('jump-bottom');
function updateJumpButton(){
 const bottom=document.getElementById('mobile-controls').offsetHeight+12;
 jumpBtn.style.bottom=bottom+'px';document.getElementById('notice').style.bottom=bottom+'px';
 const b=term.buffer.active;const visible=(b.viewportY<b.baseY||remoteHistoryPane!==null)&&!document.getElementById('terminal-container').classList.contains('hidden');
 jumpBtn.classList.toggle('visible',visible);
}
function jumpToBottom(){cancelTerminalGesture();term.scrollToBottom();updateJumpButton();}
function returnToLive(){
 jumpToBottom();
 if(canSend() && (term.buffer.active.type==='alternate'||remoteHistoryPane))ws.send(JSON.stringify({type:'return_live',pane:remoteHistoryPane}));
}
term.onScroll(updateJumpButton);term.onWriteParsed(updateJumpButton);
async function chooseOption(title,options){
 const dialog=document.getElementById('choice-modal');document.getElementById('choice-title').textContent=title;
 const select=document.getElementById('choice-select');select.replaceChildren();
 options.forEach(({value,label})=>{const option=document.createElement('option');option.value=value;option.textContent=label;select.appendChild(option);});
 dialog.returnValue='cancel';openDialog('choice-modal');
 return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='apply'?select.value:null),{once:true}));
}

let ws = null;
let currentSession = '';
let hasInput = true;
let reconnectTimer = null;
let cachedSessions = [];
let presenceData = {};

// Tabs remember destinations only; tmux owns the running processes and history.
const terminalTabKey = (server,session) => JSON.stringify([server,session]);
const terminalTabScroll = new Map();
let terminalTabs = [];
try {
  const saved=JSON.parse(localStorage.getItem('terminalTabs')||'[]');
  if(Array.isArray(saved))saved.slice(0,100).forEach(tab=>{
    if(tab && typeof tab.server==='string' && tab.server && typeof tab.session==='string' && tab.session &&
       !terminalTabs.some(t=>t.server===tab.server && t.session===tab.session))terminalTabs.push({server:tab.server,session:tab.session});
  });
} catch(_) {}
function saveTerminalTabs(){
  try {localStorage.setItem('terminalTabs',JSON.stringify(terminalTabs));
    localStorage.setItem('activeTerminalTab',currentSession?terminalTabKey(currentServer,currentSession):'');
  } catch(_) {}
}
let cancelTerminalTabDrag=null;
function moveTerminalTab(tab,to){
  const from=terminalTabs.indexOf(tab);
  if(from<0)return;
  to=Math.max(0,Math.min(terminalTabs.length-1,to));
  terminalTabs.splice(from,1);terminalTabs.splice(to,0,tab);
  saveTerminalTabs();renderTerminalTabs();
  const item=document.getElementById('terminal-tab-list').children[to];
  item.querySelector('.terminal-tab-grip').focus({preventScroll:true});
  revealTerminalTab(item);
}
function startTerminalTabDrag(e,tab,item){
  if(!e.isPrimary || e.button!==0)return;
  e.preventDefault();
  cancelTerminalTabDrag?.();
  const list=document.getElementById('terminal-tab-list'),pointer=e.pointerId;
  let x=e.clientX,y=e.clientY,frame=0,moved=false,slot=terminalTabs.indexOf(tab);
  const startX=x,startY=y;
  list.setPointerCapture(pointer);
  function clearMarks(){for(const child of list.children)child.classList.remove('drop-before','drop-after');}
  function preview(){
    clearMarks();
    const bounds=list.getBoundingClientRect();
    if(!moved || y<bounds.top-40 || y>bounds.bottom+40)return;
    const children=[...list.children];
    slot=children.findIndex(child=>{const r=child.getBoundingClientRect();return x<r.left+r.width/2;});
    if(slot<0)slot=children.length;
    const target=children[Math.min(slot,children.length-1)];
    target?.classList.add(slot===children.length?'drop-after':'drop-before');
  }
  function tick(){
    if(moved){
      const r=list.getBoundingClientRect();
      if(y>=r.top-40 && y<=r.bottom+40){
        const speed=x<r.left+24?-Math.min(12,(r.left+24-x)/2):x>r.right-24?Math.min(12,(x-r.right+24)/2):0;
        if(speed){list.scrollLeft+=speed;preview();}
      }
    }
    frame=requestAnimationFrame(tick);
  }
  function move(event){
    if(event.pointerId!==pointer)return;
    x=event.clientX;y=event.clientY;
    moved ||= Math.hypot(x-startX,y-startY)>5;
    item.classList.toggle('dragging',moved);preview();
  }
  function cleanup(){
    cancelAnimationFrame(frame);clearMarks();item.classList.remove('dragging');
    list.removeEventListener('pointermove',move);list.removeEventListener('pointerup',finish);
    list.removeEventListener('pointercancel',cancel);list.removeEventListener('lostpointercapture',cancel);
    document.removeEventListener('keydown',escape,true);
    if(list.hasPointerCapture(pointer))list.releasePointerCapture(pointer);
    cancelTerminalTabDrag=null;
  }
  function cancel(){cleanup();}
  function escape(event){if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();cleanup();}}
  function finish(event){
    if(event.pointerId!==pointer)return;
    x=event.clientX;y=event.clientY;preview();
    const bounds=list.getBoundingClientRect(),from=terminalTabs.indexOf(tab);
    const valid=moved && y>=bounds.top-40 && y<=bounds.bottom+40;
    cleanup();
    if(valid)moveTerminalTab(tab,slot>from?slot-1:slot);
  }
  list.addEventListener('pointermove',move);list.addEventListener('pointerup',finish);
  list.addEventListener('pointercancel',cancel);list.addEventListener('lostpointercapture',cancel);
  document.addEventListener('keydown',escape,true);
  cancelTerminalTabDrag=cleanup;frame=requestAnimationFrame(tick);
}
function renderTerminalTabs(){
  cancelTerminalTabDrag?.();
  const bar=document.getElementById('terminal-tabs'),list=document.getElementById('terminal-tab-list');
  const left=list.scrollLeft;list.replaceChildren();bar.hidden=terminalTabs.length===0;
  document.getElementById('topbar').classList.toggle('has-tabs',!bar.hidden);
  terminalTabs.forEach((tab,index)=>{
    const active=tab.server===currentServer && tab.session===currentSession;
    const item=document.createElement('div');item.className='terminal-tab'+(active?' active':'');item.setAttribute('role','presentation');
    const grip=document.createElement('button');grip.type='button';grip.className='terminal-tab-grip';grip.textContent='⠿';
    grip.setAttribute('aria-label','Reorder '+tab.session+' on '+tab.server);
    grip.title='Drag to reorder; Left/Right keys move this tab';
    grip.addEventListener('pointerdown',e=>startTerminalTabDrag(e,tab,item));
    grip.addEventListener('contextmenu',e=>e.preventDefault());
    grip.addEventListener('keydown',e=>{
      if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
        e.preventDefault();e.stopPropagation();
        moveTerminalTab(tab,e.key==='Home'?0:e.key==='End'?terminalTabs.length-1:index+(e.key==='ArrowRight'?1:-1));
      }
    });
    const button=document.createElement('button');button.type='button';button.className='terminal-tab-select';
    button.setAttribute('role','tab');button.setAttribute('aria-selected',String(active));button.setAttribute('aria-controls','terminal-container');
    button.setAttribute('aria-label',tab.session+' on '+tab.server);button.tabIndex=active||(!currentSession&&index===0)?0:-1;
    button.title=tab.server+' / '+tab.session+' · Alt+Shift+←/→ to switch';
    const name=document.createElement('span');name.className='terminal-tab-name';name.textContent=tab.session;
    const server=document.createElement('span');server.className='terminal-tab-server';server.textContent=tab.server;button.append(name,server);
    button.addEventListener('click',()=>activateTerminalTab(tab));
    button.addEventListener('keydown',e=>{
      let next;
      if(e.key==='ArrowRight')next=(index+1)%terminalTabs.length;
      else if(e.key==='ArrowLeft')next=(index+terminalTabs.length-1)%terminalTabs.length;
      else if(e.key==='Home')next=0;else if(e.key==='End')next=terminalTabs.length-1;
      if(next!==undefined){e.preventDefault();activateTerminalTab(terminalTabs[next]);document.querySelector('#terminal-tab-list [aria-selected="true"]')?.focus({preventScroll:true});}
      else if(e.key==='Delete'){e.preventDefault();closeTerminalTab(tab);document.querySelector('#terminal-tab-list [aria-selected="true"]')?.focus({preventScroll:true});}
    });
    const close=document.createElement('button');close.type='button';close.className='terminal-tab-close';close.textContent='×';
    close.setAttribute('aria-label','Close tab '+tab.session+' on '+tab.server);close.title='Close tab; keep session running';
    close.addEventListener('click',()=>closeTerminalTab(tab));item.append(grip,button,close);list.appendChild(item);
  });
  list.scrollLeft=left;
}
function revealActiveTerminalTab(){
  revealTerminalTab(document.querySelector('#terminal-tab-list .active'));
}
function revealTerminalTab(active){
  const list=document.getElementById('terminal-tab-list');
  if(!active)return;
  const a=active.getBoundingClientRect(),b=list.getBoundingClientRect();
  if(a.left<b.left)list.scrollLeft-=b.left-a.left;
  else if(a.right>b.right)list.scrollLeft+=a.right-b.right;
}
function rememberTerminalTab(){
  if(!terminalTabs.some(t=>t.server===currentServer && t.session===currentSession))terminalTabs.push({server:currentServer,session:currentSession});
  saveTerminalTabs();renderTerminalTabs();revealActiveTerminalTab();
}
function activateTerminalTab(tab){
  if(!tab)return;
  const keepKeyboard=kbdOpen;
  if(tab.server!==currentServer)enterServer(tab.server);
  connect(tab.session);
  if(keepKeyboard)term.focus();
}
function closeTerminalTab(tab){
  const index=terminalTabs.findIndex(t=>t.server===tab.server && t.session===tab.session);
  if(index<0)return;
  const active=tab.server===currentServer && tab.session===currentSession;
  terminalTabs.splice(index,1);
  if(active){
    const next=terminalTabs[Math.min(index,terminalTabs.length-1)];
    if(next)activateTerminalTab(next);else disconnectSession();
  }
  terminalTabScroll.delete(terminalTabKey(tab.server,tab.session));
  saveTerminalTabs();renderTerminalTabs();revealActiveTerminalTab();handleResize();
  if(kbdOpen && currentSession)term.focus();
}
function openTerminalPicker(){
  if(isTouchDevice){sidebar.classList.add('open');document.getElementById('server-strip').classList.add('open');}
  else sidebar.classList.remove('desktop-collapsed');
  handleResize();
}
function cycleTerminalTab(direction){
  if(!terminalTabs.length)return;
  const current=terminalTabs.findIndex(t=>t.server===currentServer && t.session===currentSession);
  activateTerminalTab(terminalTabs[(current+direction+terminalTabs.length)%terminalTabs.length]);
}
document.addEventListener('keydown',e=>{
  if(e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.isComposing &&
     ['ArrowLeft','ArrowRight'].includes(e.key) && !document.querySelector('dialog[open]')){
    e.preventDefault();e.stopImmediatePropagation();cycleTerminalTab(e.key==='ArrowRight'?1:-1);
  }
},true);

let clientId = sessionStorage.getItem('clientId');
if (!clientId) {
  clientId = Math.random().toString(36).substring(2, 10);
  sessionStorage.setItem('clientId', clientId);
}

function getDeviceLabel() {
  const ua = navigator.userAgent;
  let browser = 'browser';
  if (ua.includes('Firefox')) browser = 'firefox';
  else if (ua.includes('Edg/')) browser = 'edge';
  else if (ua.includes('Chrome')) browser = 'chrome';
  else if (ua.includes('Safari')) browser = 'safari';
  if (/iPhone|iPad|iPod/i.test(ua)) return 'iphone';
  if (/Android/i.test(ua)) return 'android';
  return browser;
}
const deviceLabel = getDeviceLabel();

let isMuted = false;
let isDeafened = false;

function claimControl() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (hasInput) {
    // Release control
    ws.send(JSON.stringify({type: 'release_input'}));
  } else {
    // Claim control
    ws.send(JSON.stringify({type: 'take_input'}));
    isMuted = false;
    document.getElementById('mic-btn').classList.remove('barred');
  }
}

function updateClaimButton() {
  const btn = document.getElementById('conn-claim');
  if (!btn) return;
  btn.classList.toggle('has-control', hasInput);
  btn.title = hasInput ? 'Release control' : 'Take control';
  updateInputState();
}

function toggleMic() {
  const btn = document.getElementById('mic-btn');
  if (!ws || ws.readyState !== WebSocket.OPEN) { notifyUser('Choose a connected session first.'); return; }
  isMuted = !isMuted;
  btn.classList.toggle('barred', isMuted);
  if (isMuted) {
    hasInput = false;
    term.options.cursorStyle = 'underline';
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'release_input'}));
    }
  } else {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({type: 'take_input'}));
    }
  }
  updateInputState();
}

function toggleDeafen() {
  const btn = document.getElementById('deafen-btn');
  isDeafened = !isDeafened;
  btn.classList.toggle('barred', isDeafened);
  btn.setAttribute('aria-pressed', String(isDeafened));
}

function disconnectSession() {
  const oldSession = currentSession;
  closeConnection();
  currentSession = '';
  saveTerminalTabs();
  renderTerminalTabs();
  prevConnected = false;
  if (oldSession && presenceData[oldSession]) {
    presenceData[oldSession].viewers = presenceData[oldSession].viewers.filter(v => v.id !== clientId);
  }
  term.clear(); term.reset();
  document.getElementById('conn-header').classList.remove('visible');
  document.getElementById('conn-divider').classList.remove('visible');
  document.querySelector('.status-pip').className = 'status-pip';
  isMuted = false;
  document.getElementById('mic-btn').classList.remove('barred');
  renderSidebar();
  showServerLanding();
}

const sidebar = document.getElementById('sidebar');
const mobileLayout = window.matchMedia('(hover: none) and (pointer: coarse), (max-width: 768px)');
let isTouchDevice = mobileLayout.matches;
mobileLayout.addEventListener('change', () => {
  isTouchDevice = mobileLayout.matches;
  sidebar.classList.remove('open', 'desktop-collapsed');
  document.getElementById('server-strip').classList.remove('open');
  document.getElementById('right-panel').classList.remove('open', 'collapsed');
  handleResize();
});
const connDot = document.getElementById('conn-dot');
const statusLabel = document.getElementById('status-label');
const chanTitle = document.getElementById('chan-title');
const sessionList = document.getElementById('session-list');

function closeSidebar() {
  // Only auto-close on mobile (touch devices). Desktop sidebar stays open.
  if (isTouchDevice) {
    sidebar.classList.remove('open');
    document.getElementById('server-strip').classList.remove('open');
  }
}
function toggleSidebar() {
  if (isTouchDevice) {
    sidebar.classList.toggle('open');
    document.getElementById('server-strip').classList.toggle('open', sidebar.classList.contains('open'));
  } else {
    sidebar.classList.toggle('desktop-collapsed');
    setTimeout(() => fitAddon.fit(), 200);
  }
}

let prevConnected = false;
function setConnected(on) {
  connDot.className = 'conn-dot ' + (on ? 'connected' : 'disconnected');
  statusLabel.textContent = on ? 'connected' : 'reconnecting...';
  if (on && !prevConnected) soundConnect();
  else if (!on && prevConnected) soundDisconnect();
  prevConnected = on;
  updateBottomPanel();
  updateInputState();
}
function updateBottomPanel() {
  const header = document.getElementById('conn-header');
  const divider = document.getElementById('conn-divider');
  const sub = document.getElementById('conn-header-sub');
  const pip = document.querySelector('.status-pip');
  if (currentSession && prevConnected) {
    header.classList.add('visible');
    divider.classList.add('visible');
    sub.textContent = '#' + currentSession;
    pip.className = 'status-pip online';
  } else {
    header.classList.remove('visible');
    divider.classList.remove('visible');
    pip.className = 'status-pip';
  }
}

// ── Directories (server-persisted layout) ──
let _layout = {projects: [], assignments: {}, sessionOrder: [], collapsed: {}};
function getProjects() { return _layout.projects || []; }
function getAssignments() { return _layout.assignments || {}; }
function getCollapsed() { return _layout.collapsed || {}; }
function saveProjects(p) { _layout.projects = p; _saveLayout(); }
function saveAssignments(a) { _layout.assignments = a; _saveLayout(); }
function saveCollapsed(c) { _layout.collapsed = c; _saveLayout(); }
let _saveTimer = null;
function _saveLayout() {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    fetch('/api/layout', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_layout)}).then(r=>{if(!r.ok)throw new Error('Save failed');}).catch(()=>notifyUser('Layout could not be saved. Your changes are still visible locally.'));
  }, 300);
}
async function _loadLayout() {
  try {
    const r = await fetch('/api/layout');
    _layout = await r.json();
  } catch(e) {}
}

async function newClaudeSession() {
  const server=currentServer;
  const name = prompt('Session name:');
  if (!name || !name.trim()) return;
  const sname = name.trim().replace(/[^a-zA-Z0-9_-]/g, '-');
  try {
    const r = await fetch('/api/' + encodeURIComponent(server) + '/sessions/' + sname, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: 'claude'}),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      if(server!==currentServer){notifyUser('Session created on '+server);return;}
      await loadSessions();
      connect(sname);
      closeSidebar();
    } else {
      alert('Failed: ' + (data.error || 'unknown error'));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

function addProject() {
  const name = prompt('Dir name:');
  if (!name || !name.trim()) return;
  const projects = getProjects();
  if (projects.includes(name.trim())) return;
  projects.push(name.trim());
  saveProjects(projects);
  renderSidebar();
}
async function checkedRequest(url,options){
 const r=await fetch(url,options);const data=await r.json();
 if(!r.ok || data.ok===false || data.error)throw new Error(data.error||'Request failed');
 return data;
}
async function killSession(name){
 const server=currentServer;
 if(!confirm('Kill session "'+name+'" and stop its running programs?'))return;
 try{
  await checkedRequest('/api/'+encodeURIComponent(server)+'/sessions/'+encodeURIComponent(name),{method:'DELETE'});
  closeTerminalTab({server,session:name});
  if(server!==currentServer)return;
  if(currentSession===name)disconnectSession();
  await loadSessions();notifyUser('Session ended.');
 }catch(e){notifyUser('Session was not removed: '+e.message);}
}
async function deleteProject(name){
 const server=currentServer,projects=getProjects().slice(),assign={...getAssignments()};
 const sessions=Object.keys(assign).filter(k=>assign[k]===name);
 const others=projects.filter(p=>p!==name);
 const options=[{value:'ungroup',label:'Remove group; keep its sessions'}];
 others.forEach((p,i)=>options.push({value:'move:'+i,label:'Move sessions to '+p+' and remove group'}));
 if(sessions.length)options.push({value:'kill',label:'Kill all '+sessions.length+' sessions and remove group'});
 const choice=await chooseOption('Remove group “'+name+'”',options);
 if(choice===null || server!==currentServer)return;
 if(choice==='kill'){
  const results=await Promise.allSettled(sessions.map(s=>checkedRequest('/api/'+encodeURIComponent(server)+'/sessions/'+encodeURIComponent(s),{method:'DELETE'})));
  if(server!==currentServer)return;
  const failures=results.filter(r=>r.status==='rejected');
  if(currentSession && sessions.some((s,i)=>s===currentSession && results[i].status==='fulfilled'))disconnectSession();
  if(failures.length){notifyUser(failures.length+' sessions could not be stopped. Group retained.');await loadSessions();return;}
 }
 const target=choice.startsWith('move:')?others[Number(choice.slice(5))]:null;
 if(choice.startsWith('move:')&&!target)return;
 const next={...getAssignments()};
 sessions.forEach(s=>{if(target)next[s]=target;else delete next[s];});
 saveProjects(getProjects().filter(p=>p!==name));saveAssignments(next);renderSidebar();await loadSessions();
}
async function moveSession(sessionName){
 const server=currentServer,projects=getProjects().slice();
 const choice=await chooseOption('Move “'+sessionName+'”',[
  {value:'ungrouped',label:'Ungrouped'},...projects.map((p,i)=>({value:String(i),label:p}))
 ]);
 if(choice===null||server!==currentServer)return;
 const assign={...getAssignments()};
 if(choice==='ungrouped')delete assign[sessionName];
 else if(projects[Number(choice)] && getProjects().includes(projects[Number(choice)]))assign[sessionName]=projects[Number(choice)];
 else return;
 saveAssignments(assign);renderSidebar();
}
function toggleProject(name) {
  const c = getCollapsed();
  c[name] = !c[name];
  saveCollapsed(c);
  renderSidebar();
}

// ── Drag state ──
let dragState = null;
function getSessionOrder() { return _layout.sessionOrder || []; }
function saveSessionOrder(o) { _layout.sessionOrder = o; _saveLayout(); }
function getTopOrder() { return _layout.topOrder || []; }
function saveTopOrder(o) { _layout.topOrder = o; _saveLayout(); }
function sortSessions(sessions) {
  const order = getSessionOrder();
  return sessions.slice().sort((a, b) => {
    let ai = order.indexOf(a.name), bi = order.indexOf(b.name);
    if (ai === -1) ai = 9999;
    if (bi === -1) bi = 9999;
    return ai - bi;
  });
}
let _lastDrop = null;
function clearDropIndicators() {
  document.querySelectorAll('.drop-above,.drop-below,.drop-target').forEach(el => {
    el.classList.remove('drop-above', 'drop-below', 'drop-target');
  });
  _lastDrop = null;
}
function setDropIndicator(el, clientY) {
  const rect = el.getBoundingClientRect();
  const ratio = (clientY - rect.top) / rect.height;
  let cls;
  if (_lastDrop && _lastDrop.el === el) {
    if (_lastDrop.cls === 'drop-above' && ratio > 0.65) cls = 'drop-below';
    else if (_lastDrop.cls === 'drop-below' && ratio < 0.35) cls = 'drop-above';
    else cls = _lastDrop.cls;
  } else {
    cls = ratio < 0.5 ? 'drop-above' : 'drop-below';
  }
  if (_lastDrop && _lastDrop.el === el && _lastDrop.cls === cls) return;
  clearDropIndicators();
  el.classList.add(cls);
  _lastDrop = {el, cls};
}

let prevViewerCount = 0;

function renderSidebar() {
  if (dragState) return;
  let totalViewers = 0;
  Object.values(presenceData).forEach(pd => {
    totalViewers += pd.viewers.length + pd.tmux_clients.length;
  });
  if (prevViewerCount > 0) {
    if (totalViewers > prevViewerCount) soundViewerJoin();
    else if (totalViewers < prevViewerCount) soundViewerLeave();
  }
  prevViewerCount = totalViewers;

  // Welcome viewers — show self when on welcome
  const wv = document.getElementById('welcome-viewers');
  wv.innerHTML = '';
  if (!currentSession) {
    const vlist = document.createElement('div');
    vlist.className = 'viewer-list';
    const ve = document.createElement('div');
    ve.className = 'viewer-entry';
    ve.innerHTML = '<span class="v-dot"></span><span class="v-label">' + deviceLabel + ' (you)</span>';
    vlist.appendChild(ve);
    wv.appendChild(vlist);
  }

  sessionList.innerHTML = '';
  const projects = getProjects();
  const assign = getAssignments();
  const collapsed = getCollapsed();

  const grouped = {};
  let ungrouped = [];
  cachedSessions.forEach(s => {
    const proj = assign[s.name];
    if (proj && projects.includes(proj)) {
      if (!grouped[proj]) grouped[proj] = [];
      grouped[proj].push(s);
    } else {
      ungrouped.push(s);
    }
  });
  Object.keys(grouped).forEach(k => { grouped[k] = sortSessions(grouped[k]); });
  ungrouped = sortSessions(ungrouped);

  const moveSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
  const closeSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

  function renderSessionItem(s, container) {
    const div = document.createElement('div');
    div.className = 'session-item' + (s.name === currentSession ? ' active' : '');
    div.dataset.session = s.name;
    div.draggable = true;
    div.innerHTML = '<span class="hash">#</span><span class="s-name">' + s.name +
      '</span><span class="s-actions">' +
      '<button class="s-btn s-move" title="Move to dir">' + moveSvg + '</button>' +
      '<button class="s-btn s-close" title="Kill session">' + closeSvg + '</button></span>';
    div.querySelector('.s-name').addEventListener('click', () => {
      connect(s.name); closeSidebar();
    });
    div.querySelector('.hash').addEventListener('click', () => {
      connect(s.name); closeSidebar();
    });
    div.querySelector('.s-move').addEventListener('click', (e) => {
      e.stopPropagation(); moveSession(s.name);
    });
    div.querySelector('.s-close').addEventListener('click', e => { e.stopPropagation(); killSession(s.name); });
    div.tabIndex=0;
    div.addEventListener('keydown',e=>{if(e.target===div && (e.key==='Enter'||e.key===' ')){e.preventDefault();connect(s.name);closeSidebar();}});
    div.addEventListener('click',e=>{if(e.target===div){connect(s.name);closeSidebar();}});
    div.addEventListener('dragstart', (e) => {
      dragState = {type: 'session', name: s.name};
      e.dataTransfer.setData('text/plain', s.name);
      e.dataTransfer.effectAllowed = 'move';
      setTimeout(() => div.classList.add('dragging'), 0);
    });
    div.addEventListener('dragend', () => { div.classList.remove('dragging'); dragState = null; clearDropIndicators(); renderSidebar(); });
    div.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragState || dragState.name === s.name) return;
      e.stopPropagation();
      if (dragState.type === 'session' || (dragState.type === 'project' && !assign[s.name])) {
        setDropIndicator(div, e.clientY);
      }
    });
    div.addEventListener('dragleave', () => { div.classList.remove('drop-above', 'drop-below'); });
    div.addEventListener('drop', (e) => {
      e.preventDefault(); e.stopPropagation();
      clearDropIndicators();
      if (!dragState) return;
      if (dragState.type === 'session') {
        const dragged = dragState.name;
        if (dragged === s.name) return;
        const a = getAssignments();
        const targetProj = a[s.name] || null;
        if (targetProj) { a[dragged] = targetProj; } else { delete a[dragged]; }
        saveAssignments(a);
        const order = getSessionOrder();
        const fi = order.indexOf(dragged);
        if (fi !== -1) order.splice(fi, 1);
        let ti = order.indexOf(s.name);
        if (ti === -1) { order.push(s.name); ti = order.indexOf(s.name); }
        const rect = div.getBoundingClientRect();
        if (e.clientY >= rect.top + rect.height / 2) ti++;
        order.splice(ti, 0, dragged);
        saveSessionOrder(order);
        renderSidebar();
      } else if (dragState.type === 'project' && !assign[s.name]) {
        reorderInTopOrder('p:' + dragState.name, 's:' + s.name, div, e);
      }
    });
    container.appendChild(div);
    renderViewers(s.name, container);
  }

  function renderViewers(name, container) {
    const pd = presenceData[name];
    if (!pd || (pd.viewers.length === 0 && pd.tmux_clients.length === 0)) return;
    const vlist = document.createElement('div');
    vlist.className = 'viewer-list';
    pd.viewers.forEach(v => {
      const isMe = v.id === clientId;
      const ve = document.createElement('div');
      let cls = 'viewer-entry';
      if (v.loading) cls += ' loading';
      else if (v.active) cls += ' has-control';
      ve.className = cls;
      const dots = v.typing ? '<span class="typing-dots"><span></span><span></span><span></span></span>' : '';
      ve.innerHTML =
        '<span class="v-dot"></span>' +
        '<span class="v-label">' + v.label + (isMe ? ' (you)' : '') + '</span>' + dots;
      if (isMe && name === currentSession) {
        if (hasInput && !v.active) soundLostControl();
        hasInput = isMuted ? false : v.active;
        term.options.cursorStyle = (hasInput && v.active) ? 'bar' : 'underline';
        updateClaimButton();
      }
      vlist.appendChild(ve);
    });
    const hasWebControl = pd.viewers.some(v => v.active);
    pd.tmux_clients.forEach(c => {
      const ve = document.createElement('div');
      ve.className = 'viewer-entry' + (hasWebControl ? '' : ' has-control');
      ve.innerHTML = '<span class="v-dot"></span><span class="v-label">terminal</span>';
      vlist.appendChild(ve);
    });
    container.appendChild(vlist);
  }

  const delSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

  function renderProjectGroup(proj) {
    const sessions = grouped[proj] || [];
    const isCollapsed = collapsed[proj];
    const group = document.createElement('div');
    group.className = 'project-group' + (isCollapsed ? ' collapsed' : '');
    group.dataset.project = proj;

    const hdr = document.createElement('div');
    hdr.className = 'project-header' + (isCollapsed ? ' collapsed' : '');
    hdr.draggable = true;
    hdr.dataset.project = proj;
    hdr.innerHTML =
      '<span class="arrow">&#9660;</span>' +
      '<span class="p-name">' + proj + '</span>' +
      '<span class="p-actions">' +
      '<button class="p-btn p-del" title="Delete dir">' + delSvg + '</button></span>';
    hdr.querySelector('.arrow').addEventListener('click', () => toggleProject(proj));
    hdr.querySelector('.p-name').addEventListener('click', () => toggleProject(proj));
    hdr.querySelector('.p-del').addEventListener('click', (e) => {
      e.stopPropagation(); deleteProject(proj);
    });

    hdr.addEventListener('dragstart', (e) => {
      e.stopPropagation();
      dragState = {type: 'project', name: proj};
      e.dataTransfer.setData('text/plain', proj);
      e.dataTransfer.effectAllowed = 'move';
      setTimeout(() => group.classList.add('dragging'), 0);
    });
    hdr.addEventListener('dragend', () => {
      group.classList.remove('dragging'); dragState = null; clearDropIndicators(); renderSidebar();
    });
    group.appendChild(hdr);

    sessions.forEach(s => renderSessionItem(s, group));

    group.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!dragState) return;
      if (dragState.type === 'project' && dragState.name !== proj) {
        setDropIndicator(group, e.clientY);
      } else if (dragState.type === 'session') {
        clearDropIndicators();
        group.classList.add('drop-target');
      }
    });
    group.addEventListener('dragleave', (e) => {
      if (!group.contains(e.relatedTarget)) {
        group.classList.remove('drop-target', 'drop-above', 'drop-below');
      }
    });
    group.addEventListener('drop', (e) => {
      e.preventDefault(); e.stopPropagation();
      clearDropIndicators();
      if (!dragState) return;
      if (dragState.type === 'project' && dragState.name !== proj) {
        reorderInTopOrder('p:' + dragState.name, 'p:' + proj, group, e);
      } else if (dragState.type === 'session') {
        const a = getAssignments(); a[dragState.name] = proj; saveAssignments(a); renderSidebar();
      }
    });

    sessionList.appendChild(group);
  }

  function reorderInTopOrder(draggedKey, targetKey, el, e) {
    const order = buildTopOrder();
    const fi = order.indexOf(draggedKey);
    if (fi !== -1) order.splice(fi, 1);
    let ti = order.indexOf(targetKey);
    if (ti === -1) return;
    const rect = el.getBoundingClientRect();
    if (e.clientY >= rect.top + rect.height / 2) ti++;
    order.splice(ti, 0, draggedKey);
    saveTopOrder(order);
    const p = order.filter(k => k.startsWith('p:')).map(k => k.slice(2));
    saveProjects(p);
    renderSidebar();
  }

  function buildTopOrder() {
    const stored = getTopOrder();
    const validProjects = new Set(projects);
    const validUngrouped = new Set(ungrouped.map(s => s.name));
    const allValid = new Set();
    projects.forEach(p => allValid.add('p:' + p));
    ungrouped.forEach(s => allValid.add('s:' + s.name));
    const result = stored.filter(k => allValid.has(k));
    const seen = new Set(result);
    projects.forEach(p => { const k = 'p:' + p; if (!seen.has(k)) result.push(k); });
    ungrouped.forEach(s => { const k = 's:' + s.name; if (!seen.has(k)) result.push(k); });
    return result;
  }

  const topItems = buildTopOrder();
  const ungroupedMap = {};
  ungrouped.forEach(s => { ungroupedMap[s.name] = s; });

  topItems.forEach(key => {
    if (key.startsWith('p:')) {
      renderProjectGroup(key.slice(2));
    } else if (key.startsWith('s:')) {
      const s = ungroupedMap[key.slice(2)];
      if (s) renderSessionItem(s, sessionList);
    }
  });

  sessionList.addEventListener('dragover', (e) => e.preventDefault());
  sessionList.addEventListener('drop', (e) => {
    if (e.target !== sessionList) return;
    e.preventDefault();
    clearDropIndicators();
    if (!dragState) return;
    if (dragState.type === 'session') {
      const a = getAssignments(); delete a[dragState.name]; saveAssignments(a); renderSidebar();
    }
  });
}

async function loadSessions() {
  const server=currentServer;
  if (!server) return [];
  try {
    const r = await fetch('/api/' + encodeURIComponent(server) + '/sessions');
    const data=await r.json();
    if(server!==currentServer)return [];
    if(!r.ok || !Array.isArray(data))throw new Error(data.error || 'Could not load sessions');
    cachedSessions = data;
    document.getElementById('landing-session-count').textContent=data.length+' sessions available';
    cachedSessions.forEach(s => {
      const hasViewers = s.viewers && s.viewers.length > 0;
      const hasTmux = s.tmux_clients && s.tmux_clients.length > 0;
      if (hasViewers || hasTmux) {
        // Merge: keep existing web viewers from presence WS, add tmux_clients from API
        const existing = presenceData[s.name] || {};
        presenceData[s.name] = {
          viewers: s.viewers || existing.viewers || [],
          tmux_clients: s.tmux_clients || [],
        };
      }
    });
    renderSidebar();
    return cachedSessions;
  } catch(e) { return []; }
}

function closeConnection() {
  if(currentServer && currentSession)terminalTabScroll.set(terminalTabKey(currentServer,currentSession),term.buffer.active.baseY-term.buffer.active.viewportY);
  clearTimeout(reconnectTimer); reconnectTimer=null;
  cancelTerminalGesture();resetTouchScroll();resetModifiers();savePasteDraft();
  document.querySelectorAll('dialog[open]').forEach(d=>d.close());
  const old=ws;ws=null;
  if(old){old.onopen=old.onclose=old.onerror=old.onmessage=null;old.close();}
  hasInput=false;prevConnected=false;updateInputState();
}
function connect(session) {
  if(currentSession===session && ws && ws.readyState===WebSocket.OPEN){closeSidebar();if(!isTouchDevice)term.focus();return;}
  closeConnection();currentSession=session;isMuted=false;
  rememberTerminalTab();
  document.getElementById('mic-btn').classList.remove('barred');
  hideServerLanding();chanTitle.textContent=session||'—';
  term.reset();fitAddon.fit();
  openSessionSocket(currentServer,session,terminalTabScroll.get(terminalTabKey(currentServer,session))||0);
  // Desktop navigation should leave normal typing in the terminal. Touch
  // devices still open their keyboard only through an explicit terminal tap.
  if(!isTouchDevice)term.focus();
}
function reconnect(session) {
  if(!currentServer || currentSession!==session)return;
  const distance=term.buffer.active.baseY-term.buffer.active.viewportY;
  openSessionSocket(currentServer,session,distance);
}
function openSessionSocket(server,session,distance) {
  clearTimeout(reconnectTimer);reconnectTimer=null;
  const old=ws;if(old){old.onclose=null;old.onmessage=null;old.close();}
  const proto=location.protocol==='https:'?'wss':'ws';
  const socket=new WebSocket(proto+'://'+location.host+'/ws/'+encodeURIComponent(server)+'/'+encodeURIComponent(session)+
    '?cols='+term.cols+'&rows='+term.rows+'&device='+encodeURIComponent(deviceLabel)+'&client_id='+clientId+'&ui_version=2026-10-22-terminal-focus');
  ws=socket;socket.binaryType='arraybuffer';hasInput=false;updateInputState();
  const current=()=>ws===socket && currentServer===server && currentSession===session;
  let replayPending=true;
  socket.onopen=()=>{if(current()){setConnected(true);fitAddon.fit();}};
  socket.onclose=()=>{
    if(!current())return;
    hasInput=false;cancelTerminalGesture();resetTouchScroll();setConnected(false);
    reconnectTimer=setTimeout(()=>{if(current())reconnect(session);},3000);
  };
  socket.onerror=()=>{};
  socket.onmessage=e=>{
    if(!current())return;
    if(replayPending && e.data instanceof ArrayBuffer){
      replayPending=false;term.reset();
      term.write(new Uint8Array(e.data),()=>{if(current()){if(distance)term.scrollToLine(Math.max(0,term.buffer.active.baseY-distance));updateJumpButton();}});
    }else handleMessage(e);
  };
}

function handleMessage(e) {
  if (e.data instanceof ArrayBuffer) {
    term.write(new Uint8Array(e.data));
  } else {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'presence') {
        const me = (msg.sessions[currentSession]?.viewers || []).find(v => v.id === clientId);
        hasInput = !!me?.active && !isMuted;
        updateClaimButton();
        // Merge: update viewers from WS, but preserve tmux_clients from last API fetch
        Object.keys(msg.sessions).forEach(k => {
          const existing = presenceData[k] || {};
          presenceData[k] = {
            viewers: msg.sessions[k].viewers || [],
            tmux_clients: existing.tmux_clients || msg.sessions[k].tmux_clients || [],
          };
        });
        // Remove sessions no longer in presence (viewer left)
        Object.keys(presenceData).forEach(k => {
          if (!msg.sessions[k]) {
            presenceData[k] = { viewers: [], tmux_clients: presenceData[k].tmux_clients || [] };
          }
        });
        renderSidebar();
        return;
      }
      if (msg.type === 'pong') return;
      if (msg.type === 'return_live_result') { if (!msg.ok) notifyUser('Remote history could not be closed.'); else { remoteHistoryPane=null; updateJumpButton(); } return; }
      if (msg.type === 'scroll_result') { handleTouchScrollResult(msg); return; }
      if (msg.type === 'no_control') {
        hasInput = false;
        updateClaimButton();
        term.options.cursorStyle = 'underline';
        return;
      }
      if (msg.type === 'error') {
        term.write('\r\n  \x1b[31m' + msg.msg + '\x1b[0m\r\n');
        return;
      }
    } catch(_) {}
    term.write(e.data);
  }
}

// Keepalive
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'ping'}));
  }
}, 25000);

term.onData(data => sendKey(applyModifiers(data), true));
term.onBinary(data => {
  if (canSend()) ws.send(Uint8Array.from(data, ch => ch.charCodeAt(0) & 255));
});

term.onResize(({cols, rows}) => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({type: 'resize', cols, rows}));
  }
});

// Handle resize + mobile keyboard open/close
let _resizeTimer = null;

function handleResize() {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    // Sync body to visualViewport on all touch devices when keyboard is open
    if (window.visualViewport && isTouchDevice) {
      document.body.style.height = window.visualViewport.height + 'px';
    }
    fitAddon.fit();
    updateJumpButton();
  }, 100);
}
window.addEventListener('resize', () => { handleResize(); checkEscOverflow(); });
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', () => { handleResize(); checkEscOverflow(); });
  window.visualViewport.addEventListener('scroll', () => {
    window.scrollTo(0, 0);
  });
}

// Browser pinch zoom remains available.

// Start: load servers + layout, then show home or enter first connected server
_loadLayout().then(async () => {
  await fetchServers();
  renderServers();
  renderTerminalTabs();
  let last='';try{last=localStorage.getItem('activeTerminalTab')||'';}catch(_){}
  const restored=terminalTabs.find(t=>terminalTabKey(t.server,t.session)===last &&
    cachedServerList.some(s=>s.name===t.server && s.connected));
  if(restored){
    // The server overview can lag newly-created sessions; query the live list.
    const sessions=await enterServer(restored.server);
    if(currentServer===restored.server && !currentSession && sessions.some(s=>s.name===restored.session))connect(restored.session);
    return;
  }
  const connected = cachedServerList.find(s => s.connected);
  if (connected) {
    enterServer(connected.name);
  } else {
    showHome();
  }
});

// Refresh server list periodically to detect connections/disconnections
setInterval(async () => {
  if (!onServer) {
    await fetchServers();
    renderServers();
    renderHomeCards();
  }
}, 10000);

setInterval(() => { if (onServer && currentServer) loadSessions(); }, 15000);

</script>
</body>
</html>"""


if __name__ == "__main__":
    import socket
    import uvicorn
    hosts = os.environ.get("TMUX_BRIDGE_BIND_HOSTS", "127.0.0.1").split(",")
    sockets = []
    try:
        for host in hosts:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host.strip(), 4601))
            listener.setblocking(False)
        uvicorn.Server(uvicorn.Config(app, port=4601, log_level="info")).run(sockets=sockets)
    finally:
        for listener in sockets:
            listener.close()
