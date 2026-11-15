"""Independent adapter for cmux's local JSON socket protocol.

The Mac owns the terminals. Channels are viewers and never own their processes.
Only explicit management RPCs close surfaces or workspaces.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import json
import os
from pathlib import Path
import re
import uuid


class CmuxError(Exception):
    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.code = code


def socket_path(config):
    explicit = config.get("cmux_socket") or os.environ.get("CMUX_SOCKET_PATH")
    if explicit:
        return str(Path(explicit).expanduser())
    # Never auto-select a debug instance or a last-used socket belonging to it.
    paths = [Path.home() / ".local/state/cmux/cmux.sock",
             Path.home() / "Library/Application Support/cmux/cmux.sock", Path("/tmp/cmux.sock")]
    return str(next((p for p in paths if p.exists()), paths[0]))


class CmuxClient:
    def __init__(self, path, timeout=3):
        self.path, self.timeout = path, timeout
        self._slots = asyncio.Semaphore(4)

    async def call(self, method, **params):
        # A separate connection per request permits input while a replay is pending.
        # Never retry mutations: a lost reply does not establish that they failed.
        async def request():
            async with self._slots:
                reader, writer = await asyncio.open_unix_connection(self.path, limit=2**21)
                try:
                    request_id = str(uuid.uuid4())
                    writer.write((json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode())
                    await writer.drain()
                    line = await reader.readline()
                    if not line.endswith(b"\n"):
                        raise CmuxError("cmux closed the socket before replying")
                    reply = json.loads(line)
                    if not isinstance(reply, dict) or reply.get("id") != request_id:
                        raise CmuxError("Invalid cmux response ID")
                    if reply.get("ok") is not True:
                        error = reply.get("error") or {}
                        raise CmuxError(error.get("message", "cmux request failed"), error.get("code", "error"))
                    result = reply.get("result")
                    if not isinstance(result, dict):
                        raise CmuxError("Invalid cmux result")
                    return result
                finally:
                    writer.close()
                    await writer.wait_closed()
        try:
            return await asyncio.wait_for(request(), self.timeout)
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            raise CmuxError(f"cmux socket request failed: {exc}") from exc


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise CmuxError("A stable cmux UUID is required", "invalid_params") from None


def session_name(workspace, surface):
    return "cmux:" + identifier(workspace) + ":" + identifier(surface)


def target(name):
    if not isinstance(name, str):
        raise CmuxError("Invalid terminal ID", "invalid_params")
    parts = name.split(":")
    if len(parts) != 3 or parts[0] != "cmux":
        raise CmuxError("Invalid terminal ID", "invalid_params")
    return {"workspace_id": identifier(parts[1]), "surface_id": identifier(parts[2])}


def screen_frame(result):
    """Translate a full render-grid to a bounded, inert VT screen repaint.

    Text never becomes terminal control sequences. Only known styles and input
    modes are encoded; links, clipboard operations and arbitrary OSC are excluded.
    """
    grid = result.get("render_grid") or {}
    if grid.get("format") != "cmux.render-grid.v1" or grid.get("full") is not True:
        raise CmuxError("This cmux version did not return a full render-grid snapshot", "unsupported")
    cols, rows = grid.get("columns"), grid.get("rows")
    if type(cols) is not int or type(rows) is not int or not (1 <= cols <= 500 and 1 <= rows <= 200):
        raise CmuxError("Invalid cmux screen dimensions")
    styles = {s["id"]: s for s in grid.get("styles", []) if isinstance(s, dict) and type(s.get("id")) is int}
    out = ["\x1b[?25l\x1b[?7l\x1b[0m\x1b[H\x1b[2J"]
    for span in grid.get("row_spans", []):
        row, col = span.get("row"), span.get("column")
        if type(row) is not int or type(col) is not int or not (0 <= row < rows and 0 <= col < cols):
            raise CmuxError("Invalid cmux screen span")
        text = span.get("text", "")
        if not isinstance(text, str) or len(text) > cols * 16:
            raise CmuxError("Invalid cmux screen text")
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)
        style = styles.get(span.get("style_id"), {})
        sgr = ["0"]
        for key, code in (("bold", 1), ("faint", 2), ("italic", 3), ("underline", 4),
                          ("blink", 5), ("inverse", 7), ("invisible", 8), ("strikethrough", 9), ("overline", 53)):
            if style.get(key) is True:
                sgr.append(str(code))
        for key, code in (("foreground", 38), ("background", 48)):
            color = style.get(key)
            if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                sgr.append(f"{code};2;" + ";".join(str(int(color[i:i+2], 16)) for i in (1, 3, 5)))
        out.append(f"\x1b[{row+1};{col+1}H\x1b[{';'.join(sgr)}m{text}")
    # Reapply a small set of input modes on every full snapshot, including resets.
    modes = {m.get("code") for m in grid.get("modes", []) if m.get("ansi") is False and m.get("on") is True}
    for code in (1, 1004, 2004):
        out.append(f"\x1b[?{code}{'h' if code in modes else 'l'}")
    # Mouse delivery needs cmux's structured mouse API, not legacy non-UTF8
    # terminal reports. This first adapter supports keyboard control only.
    for code in (1000, 1002, 1003, 1006):
        out.append(f"\x1b[?{code}l")
    cursor = grid.get("cursor") or {}
    row, col = cursor.get("row", 0), cursor.get("column", 0)
    if type(row) is not int or type(col) is not int:
        raise CmuxError("Invalid cmux cursor")
    out.append(f"\x1b[0m\x1b[{max(1, min(rows, row+1))};{max(1, min(cols, col+1))}H")
    if cursor.get("visible") is True:
        out.append("\x1b[?25h")
    payload = "".join(out).encode()
    if len(payload) > 1024 * 1024:
        raise CmuxError("cmux screen exceeds bridge limit")
    return {"type": "native_frame", "cols": cols, "rows": rows,
            "data": base64.b64encode(payload).decode()}


class CmuxSession:
    def __init__(self, manager, name, channel, send_callback, cols, rows):
        self.manager, self.name, self.channel = manager, name, channel
        self.send_callback = send_callback
        self.params = target(name)
        self.client_id = "tmux-bridge-" + str(uuid.uuid4())
        self.generation = 0
        self.alive = True
        self._tasks = []
        self._queue = asyncio.Queue(maxsize=64)
        self._pending_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.resize(cols, rows)

    async def start(self):
        # Fail attachment before accepting keyboard input if replay isn't supported.
        await self._viewport()
        initial = await self._snapshot()
        self._tasks = [asyncio.create_task(self._frames(initial)), asyncio.create_task(self._input())]

    def resize(self, cols, rows):
        self.cols = max(20, min(240, int(cols)))
        self.rows = max(5, min(120, int(rows)))
        self.generation += 1

    async def _viewport(self):
        generation = self.generation
        await self.manager.client.call("terminal.viewport", **self.params, client_id=self.client_id,
                                       viewport_columns=self.cols, viewport_rows=self.rows,
                                       viewport_generation=generation)
        self._applied_generation = generation

    async def _snapshot(self):
        return screen_frame(await self.manager.client.call("terminal.replay", **self.params,
                                                            anchor="screen", max_scrollback_rows=0))

    async def _frames(self, initial):
        previous = None
        try:
            frame = initial
            while self.alive:
                if frame != previous:
                    await self.send_callback(json.dumps({**frame, "channel": self.channel}))
                    previous = frame
                # Local cmux sockets currently lack a screen-grid event subscription.
                # One in-flight bounded snapshot at 5 Hz; no accumulating output tasks.
                await asyncio.sleep(.2)
                if self._applied_generation != self.generation:
                    await self._viewport()
                frame = await self._snapshot()
        except (CmuxError, OSError) as exc:
            await self._failed(exc)

    def write_input(self, data):
        if not self.alive or len(data) + self._pending_bytes > 256 * 1024:
            return False
        try:
            self._queue.put_nowait(data)
            self._pending_bytes += len(data)
            return True
        except asyncio.QueueFull:
            return False

    async def _input(self):
        try:
            while self.alive:
                data = await self._queue.get()
                self._pending_bytes -= len(data)
                text = self._decoder.decode(data)
                if text:
                    await self.manager.client.call("terminal.input", **self.params, text=text)
        except (CmuxError, UnicodeError) as exc:
            await self._failed(exc)

    async def _failed(self, exc):
        print(f"[CMUX] Channel {self.channel}: {exc}", flush=True)
        await self.manager.channel_exited(self.channel)

    def get_replay(self):
        return b""  # Gateway retains the latest atomic native frame.

    async def stop(self):
        self.alive = False
        tasks = [t for t in self._tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.manager.client.call("terminal.viewport", **self.params, client_id=self.client_id,
                                           viewport_generation=self.generation + 1, clear=True)
        except CmuxError as exc:
            print(f"[CMUX] Viewport release: {exc}", flush=True)


class CmuxManager:
    def __init__(self, config, on_channel_exit=None):
        self.client = CmuxClient(socket_path(config))
        self.channels = {}
        self.on_channel_exit = on_channel_exit
        self.sessions, self.workspaces, self.windows = [], [], []
        self._refresh_lock = asyncio.Lock()

    async def start(self):
        pass

    def list_sessions(self):
        return self.sessions

    async def refresh(self):
        async with self._refresh_lock:
            async def discover():
                windows = (await self.client.call("window.list")).get("windows", [])
                workspaces, sessions = [], []
                for window in windows:
                    window_id = identifier(window["id"])
                    listing = await self.client.call("workspace.list", window_id=window_id)
                    for ws in listing.get("workspaces", []):
                        wid = identifier(ws["id"])
                        workspaces.append({"id": wid, "title": ws.get("title", wid), "window_id": window_id})
                        surfaces = await self.client.call("surface.list", workspace_id=wid)
                        for surface in surfaces.get("surfaces", []):
                            if surface.get("type") != "terminal":
                                continue
                            sid = identifier(surface["id"])
                            name = session_name(wid, sid)
                            sessions.append({"name": name, "label": f"{ws.get('title', wid)} / {surface.get('title', sid)}",
                                             "backend": "cmux", "workspace_id": wid, "surface_id": sid,
                                             "created": 0, "windows": 1, "attached": 1,
                                             "bridged": any(s.name == name and s.alive for s in self.channels.values()),
                                             "tmux_clients": []})
                self.windows, self.workspaces, self.sessions = windows, workspaces, sessions
                return sessions
            try:
                return await asyncio.wait_for(discover(), 7)
            except (KeyError, TypeError, asyncio.TimeoutError) as exc:
                raise CmuxError(f"Could not discover cmux workspaces: {exc}") from exc

    async def open_channel(self, channel, name, send_callback, cols=120, rows=40):
        for old_channel, session in list(self.channels.items()):
            if old_channel == channel or session.name == name:
                await self.close_channel(old_channel)
        session = CmuxSession(self, name, channel, send_callback, cols, rows)
        self.channels[channel] = session
        try:
            await session.start()
        except (CmuxError, ValueError):
            await self.close_channel(channel)
            raise
        return session

    async def close_channel(self, channel):
        session = self.channels.pop(channel, None)
        if session:
            await session.stop()

    async def channel_exited(self, channel):
        await self.close_channel(channel)
        if self.on_channel_exit:
            await self.on_channel_exit(channel)

    async def stop_all(self):
        await asyncio.gather(*(self.close_channel(ch) for ch in list(self.channels)))

    async def rpc(self, method, path, body):
        try:
            return {"status": 200, "body": await self._rpc(method, path, body or {})}
        except (CmuxError, ValueError, TypeError) as exc:
            code = getattr(exc, "code", "invalid_params")
            status = {"not_found": 404, "invalid_params": 400, "unsupported": 501,
                      "confirmation_required": 409, "protected": 409}.get(code, 503)
            return {"status": status, "body": {"ok": False, "error": str(exc), "code": code}}

    async def _rpc(self, method, path, body):
        if path == "/api/sessions" and method == "GET":
            return await self.refresh()
        if path == "/api/cmux/workspaces" and method == "GET":
            await self.refresh()
            return {"backend": "cmux", "windows": self.windows, "workspaces": self.workspaces}
        if path == "/api/cmux/create" and method == "POST":
            title = body.get("title", "")
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise CmuxError("Workspace title must contain 1–200 characters", "invalid_params")
            return {"ok": True, **await self.client.call("workspace.create", title=title,
                      window_id=identifier(body.get("window_id")), focus=False,
                      operation_id=identifier(body.get("operation_id")))}
        if path in ("/api/cmux/select", "/api/cmux/close") and method == "POST":
            params = {"workspace_id": identifier(body.get("workspace_id"))}
            if path.endswith("close"):
                if body.get("confirmed") is not True:
                    raise CmuxError("Confirm closing the workspace and its programs", "confirmation_required")
                params["force"] = True
            result = await self.client.call("workspace.close" if path.endswith("close") else "workspace.select", **params)
            return {"ok": True, **result}
        if path == "/api/cmux/split" and method == "POST":
            direction = body.get("direction", "right")
            if direction not in ("left", "right", "up", "down"):
                raise CmuxError("Invalid split direction", "invalid_params")
            return {"ok": True, **await self.client.call("surface.split", **target(body.get("session")),
                                                         direction=direction, type="terminal", focus=False)}
        if path.startswith("/api/sessions/") and method == "DELETE":
            params = target(path.rsplit("/", 1)[-1])
            await self.client.call("surface.close", **params, force=True)
            return {"ok": True}
        if path.startswith("/api/sessions/") and method == "POST":
            raise CmuxError("Use New cmux workspace to create native terminals", "unsupported")
        if path == "/api/history" and method == "GET":
            result = await self.client.call("surface.read_text", **target(body.get("session")), scrollback=True, lines=2000)
            return {"ok": True, "text": result.get("text", ""), "pane": body["session"]}
        if path in ("/api/scroll", "/api/history/live"):
            # The first adapter repaints the live screen. History is exposed by
            # the existing transcript endpoint, not by moving the Mac's viewport.
            raise CmuxError("Native cmux scroll gestures are not available yet; use History", "unsupported")
        raise CmuxError("Unknown cmux operation", "not_found")
