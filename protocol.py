"""
Shared protocol for tmux-bridge: message types, binary framing, control messages.

Binary frame format: [2-byte channel_id (big-endian)][terminal data]
JSON frames: control messages (register, connect, disconnect, resize, rpc)
"""
from __future__ import annotations

import json
import struct

# ── Message types ──

# Server → Gateway (on connect)
MSG_AUTH = "auth"
MSG_REGISTER = "register"

# Gateway → Server
MSG_CONNECT = "connect"       # open terminal channel
MSG_DISCONNECT = "disconnect"  # close terminal channel
MSG_RESIZE = "resize"
MSG_RPC_REQUEST = "rpc_request"

# Server → Gateway
MSG_SESSION_UPDATE = "session_update"
MSG_CHANNEL_CLOSED = "channel_closed"  # terminal attachment exited unexpectedly
MSG_RPC_RESPONSE = "rpc_response"

# Both directions (keepalive)
MSG_PING = "ping"
MSG_PONG = "pong"

# Channel 0 is reserved for control messages
CONTROL_CHANNEL = 0
MAX_CHANNEL = 65535


# ── Binary framing ──

def pack_data(channel_id: int, data: bytes) -> bytes:
    """Pack terminal data with channel ID header."""
    return struct.pack("!H", channel_id) + data


def unpack_data(frame: bytes) -> tuple[int, bytes]:
    """Unpack binary frame into (channel_id, data)."""
    if len(frame) < 2:
        raise ValueError("Frame too short")
    channel_id = struct.unpack("!H", frame[:2])[0]
    return channel_id, frame[2:]


# ── JSON control messages ──

def make_connect(channel: int, session: str, cols: int = 120, rows: int = 40) -> str:
    return json.dumps({
        "type": MSG_CONNECT,
        "channel": channel,
        "session": session,
        "cols": cols,
        "rows": rows,
    })


def make_disconnect(channel: int) -> str:
    return json.dumps({"type": MSG_DISCONNECT, "channel": channel})


def make_resize(channel: int, cols: int, rows: int) -> str:
    return json.dumps({
        "type": MSG_RESIZE,
        "channel": channel,
        "cols": cols,
        "rows": rows,
    })


def make_rpc_request(request_id: str, method: str, path: str, body: dict | None = None) -> str:
    msg = {
        "type": MSG_RPC_REQUEST,
        "id": request_id,
        "method": method,
        "path": path,
    }
    if body is not None:
        msg["body"] = body
    return json.dumps(msg)


def make_rpc_response(request_id: str, status: int, body) -> str:
    return json.dumps({
        "type": MSG_RPC_RESPONSE,
        "id": request_id,
        "status": status,
        "body": body,
    })


def make_register(name: str, sessions: list[dict]) -> str:
    return json.dumps({
        "type": MSG_REGISTER,
        "name": name,
        "sessions": sessions,
    })


def make_session_update(sessions: list[dict]) -> str:
    return json.dumps({
        "type": MSG_SESSION_UPDATE,
        "sessions": sessions,
    })


def make_ping() -> str:
    return json.dumps({"type": MSG_PING})


def make_pong() -> str:
    return json.dumps({"type": MSG_PONG})


def parse_message(text: str) -> dict:
    """Parse a JSON control message."""
    return json.loads(text)
