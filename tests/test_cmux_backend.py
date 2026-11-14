"""Native adapter tests against an isolated Unix protocol fixture, without cmux."""
import asyncio
import base64
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
import uuid

from cmux_backend import CmuxClient, CmuxError, CmuxManager, screen_frame, session_name, target
from server import ServerAgent

WINDOW, WORKSPACE, SURFACE = [str(uuid.uuid4()) for _ in range(3)]
NAME = session_name(WORKSPACE, SURFACE)


def grid(text="café 日本語 🧪"):
    return {"render_grid": {"format": "cmux.render-grid.v1", "full": True,
            "columns": 80, "rows": 24, "styles": [{"id": 1, "bold": True, "foreground": "#ff0000"}],
            "row_spans": [{"row": 0, "column": 0, "style_id": 1, "text": text}],
            "cursor": {"row": 1, "column": 2, "visible": True},
            "modes": [{"code": 2004, "ansi": False, "on": True}]}}


class NativeBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cb-")
        self.path = str(Path(self.temp.name) / "s")
        self.requests = []
        self.handlers = set()
        self.input_received = asyncio.Event()
        self.reply_id = None
        self.hold = None
        self.unsupported = False
        self.server = await asyncio.start_unix_server(self.handle, path=self.path)
        self.manager = CmuxManager({"cmux_socket": self.path})

    async def asyncTearDown(self):
        await self.manager.stop_all()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        self.temp.cleanup()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            request = json.loads(await reader.readline())
            self.requests.append(request)
            if self.hold:
                await self.hold.wait()
            method, params = request["method"], request["params"]
            result = {}
            if method == "window.list":
                result = {"windows": [{"id": WINDOW}]}
            elif method == "workspace.list":
                result = {"workspaces": [{"id": WORKSPACE, "title": "日本語 <workspace>"}]}
            elif method == "surface.list":
                result = {"surfaces": [{"id": SURFACE, "title": "café", "type": "terminal"},
                                        {"id": str(uuid.uuid4()), "type": "browser"}]}
            elif method == "terminal.replay":
                result = {} if self.unsupported else grid()
            elif method == "terminal.input":
                self.input_received.set()
            elif method == "workspace.create":
                result = {"workspace_id": WORKSPACE, "surface_id": SURFACE}
            elif method == "surface.read_text":
                result = {"text": "history 🧪"}
            payload = json.dumps({"id": self.reply_id or request["id"], "ok": True, "result": result}, ensure_ascii=False).encode() + b"\n"
            # Real stream fragmented inside every UTF-8 character. No timer sleeps.
            for byte in payload:
                writer.write(bytes([byte]))
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass  # Deadline test deliberately closes its peer before replying.
        finally:
            writer.close()
            with suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()
            self.handlers.discard(task)

    async def test_discovery_stable_targets_and_unicode_labels(self):
        sessions = await self.manager.refresh()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["name"], NAME)
        self.assertEqual(sessions[0]["label"], "日本語 <workspace> / café")
        self.assertEqual(self.requests[1]["params"], {"window_id": WINDOW})
        self.assertEqual(self.requests[2]["params"], {"workspace_id": WORKSPACE})

    async def test_mismatched_reply_is_rejected(self):
        self.reply_id = "other-request"
        with self.assertRaisesRegex(CmuxError, "response ID"):
            await self.manager.refresh()

    async def test_deadline_releases_connection(self):
        self.hold = asyncio.Event()
        client = CmuxClient(self.path, timeout=.05)
        with self.assertRaises(CmuxError):
            await client.call("window.list")
        self.hold.set()

    async def test_attach_input_resize_and_detach_keep_process_alive(self):
        frames, delivered = [], asyncio.Event()
        async def send(frame):
            frames.append(json.loads(frame))
            delivered.set()
        session = await self.manager.open_channel(7, NAME, send, 80, 24)
        await asyncio.wait_for(delivered.wait(), 1)
        self.assertEqual(frames[0]["channel"], 7)
        self.assertIn("日本語", base64.b64decode(frames[0]["data"]).decode())
        encoded = "🧪\r".encode()
        self.assertTrue(session.write_input(encoded[:2]))
        self.assertTrue(session.write_input(encoded[2:]))
        await asyncio.wait_for(self.input_received.wait(), 1)
        inputs = [r for r in self.requests if r["method"] == "terminal.input"]
        self.assertEqual(inputs[0]["params"], {"workspace_id": WORKSPACE, "surface_id": SURFACE, "text": "🧪\r"})
        session.resize(95, 31)
        await session._viewport()
        await self.manager.close_channel(7)
        self.assertFalse(self.manager.channels)
        self.assertNotIn("workspace.close", [r["method"] for r in self.requests])
        self.assertNotIn("surface.close", [r["method"] for r in self.requests])
        release = self.requests[-1]["params"]
        self.assertTrue(release["clear"])
        self.assertGreater(release["viewport_generation"], 1)

    async def test_failed_attach_releases_viewport_and_channel(self):
        self.unsupported = True
        with self.assertRaisesRegex(CmuxError, "render-grid"):
            await self.manager.open_channel(2, NAME, AsyncMock())
        self.assertFalse(self.manager.channels)
        self.assertTrue(self.requests[-1]["params"]["clear"])

    async def test_workspace_management_and_close_confirmation(self):
        denied = await self.manager.rpc("POST", "/api/cmux/close", {"workspace_id": WORKSPACE})
        self.assertEqual(denied["status"], 409)
        self.assertFalse(self.requests)
        operation = str(uuid.uuid4())
        created = await self.manager.rpc("POST", "/api/cmux/create", {
            "title": "Unicode 🧪", "window_id": WINDOW, "operation_id": operation})
        self.assertEqual(created["body"]["workspace_id"], WORKSPACE)
        self.assertEqual(self.requests[-1]["params"]["operation_id"], operation)
        selected = await self.manager.rpc("POST", "/api/cmux/select", {"workspace_id": WORKSPACE})
        self.assertEqual(selected["status"], 200)
        closed = await self.manager.rpc("POST", "/api/cmux/close", {"workspace_id": WORKSPACE, "confirmed": True})
        self.assertEqual(closed["status"], 200)
        self.assertEqual(self.requests[-1]["params"], {"workspace_id": WORKSPACE, "force": True})

    async def test_split_and_close_use_explicit_terminal_ids(self):
        split = await self.manager.rpc("POST", "/api/cmux/split", {"session": NAME, "direction": "down"})
        self.assertEqual(split["status"], 200)
        self.assertEqual(self.requests[-1]["params"]["surface_id"], SURFACE)
        self.assertEqual(self.requests[-1]["params"]["workspace_id"], WORKSPACE)
        closed = await self.manager.rpc("DELETE", "/api/sessions/" + NAME, {})
        self.assertEqual(closed["status"], 200)
        self.assertEqual(self.requests[-1]["method"], "surface.close")
        self.assertEqual(self.requests[-1]["params"]["surface_id"], SURFACE)

    async def test_invalid_ids_never_fall_back_to_focused_terminal(self):
        for invalid in ("focused", "cmux::", None, "cmux:1:2"):
            result = await self.manager.rpc("POST", "/api/cmux/split", {"session": invalid})
            self.assertEqual(result["status"], 400)
        self.assertFalse(self.requests)

    async def test_agent_routes_native_requests_and_history(self):
        agent = ServerAgent({"name": "fixture", "gateway": "http://localhost", "backend": "cmux", "cmux_socket": self.path})
        ws = AsyncMock()
        await agent._handle_rpc(ws, {"id": "request", "method": "GET", "path": "/api/sessions"})
        reply = json.loads(ws.send.call_args.args[0])
        self.assertEqual(reply["body"][0]["name"], NAME)
        await agent._handle_rpc(ws, {"id": "history", "method": "GET", "path": "/api/history", "body": {"session": NAME}})
        reply = json.loads(ws.send.call_args.args[0])
        self.assertEqual(reply["body"]["text"], "history 🧪")

    async def test_input_backlog_is_bounded(self):
        from cmux_backend import CmuxSession
        session = CmuxSession(self.manager, NAME, 1, AsyncMock(), 80, 24)
        self.assertFalse(session.write_input(b"a" * (256 * 1024 + 1)))
        for _ in range(64):
            self.assertTrue(session.write_input(b"x"))
        self.assertFalse(session.write_input(b"x"))

    async def test_agent_gateway_native_frame_and_input_pipeline(self):
        import gateway
        agent = ServerAgent({"name": "fixture", "gateway": "http://localhost", "backend": "cmux", "cmux_socket": self.path})
        inbox, frames = asyncio.Queue(), asyncio.Queue()
        class AgentSocket:
            def __aiter__(self):
                return self
            async def __anext__(self):
                return await inbox.get()
            async def send(self, message):
                frame = json.loads(message)
                if frame["type"] == "native_frame":
                    proxy.receive_native_frame(frame)
        class GatewaySocket:
            async def send_text(self, message):
                await inbox.put(message)
            async def send_bytes(self, message):
                await inbox.put(message)
        class Viewer:
            async def send_text(self, message):
                await frames.put(json.loads(message))
        connection = gateway.ServerConnection(GatewaySocket(), "fixture")
        proxy = gateway.ProxySession(connection, NAME)
        viewer = Viewer()
        proxy.viewers[viewer] = gateway.ViewerInfo(viewer, "viewer", "browser", "browser", NAME, 80, 24)
        proxy.active_input = viewer
        loop = asyncio.create_task(agent._message_loop(AgentSocket()))
        try:
            await proxy.ensure_channel(80, 24)
            frame = await asyncio.wait_for(frames.get(), 1)
            self.assertEqual(frame["type"], "native_frame")
            self.assertNotIn("channel", frame)
            self.assertFalse(proxy.buffer)
            self.assertTrue(await proxy.write_input(viewer, "🧪".encode()))
            await asyncio.wait_for(self.input_received.wait(), 1)
            self.assertEqual([r for r in self.requests if r["method"] == "terminal.input"][-1]["params"]["text"], "🧪")
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            if proxy._native_task:
                await proxy._native_task
            await agent.manager.stop_all()

    async def test_gateway_coalesces_snapshots_while_a_viewer_is_slow(self):
        import gateway
        entered, release = asyncio.Event(), asyncio.Event()
        received = []
        class Viewer:
            async def send_text(self, message):
                received.append(json.loads(message)["data"])
                if len(received) == 1:
                    entered.set()
                    await release.wait()
        proxy = gateway.ProxySession(None, NAME)
        proxy.viewers[Viewer()] = None
        frame = {"type": "native_frame", "cols": 80, "rows": 24, "data": "first"}
        proxy.receive_native_frame(frame)
        await entered.wait()
        task = proxy._native_task
        for i in range(1000):
            proxy.receive_native_frame({**frame, "data": str(i)})
        self.assertIs(proxy._native_task, task)
        release.set()
        await task
        self.assertEqual(received, ["first", "999"])
        self.assertFalse(proxy.buffer)


class ScreenTests(unittest.TestCase):
    def test_repaint_encodes_styles_cursor_and_modes(self):
        frame = screen_frame(grid())
        text = base64.b64decode(frame["data"]).decode()
        self.assertIn("\x1b[0;1;38;2;255;0;0m", text)
        self.assertIn("café 日本語 🧪", text)
        self.assertIn("\x1b[2;3H", text)
        self.assertIn("\x1b[?2004h", text)
        self.assertEqual((frame["cols"], frame["rows"]), (80, 24))

    def test_text_cannot_inject_terminal_controls(self):
        result = screen_frame(grid("safe\x1b]52;c;secret\x07\x9b31m"))
        payload = base64.b64decode(result["data"]).decode()
        self.assertNotIn("\x1b]52", payload)
        self.assertNotIn("\x07", payload)
        self.assertNotIn("\x9b", payload)

    def test_reject_partial_and_out_of_bounds_frames(self):
        for key, value in (("full", False), ("columns", 999999), ("format", "future")):
            fixture = grid()
            fixture["render_grid"][key] = value
            with self.assertRaises(CmuxError):
                screen_frame(fixture)
        fixture = grid()
        fixture["render_grid"]["row_spans"][0]["row"] = 24
        with self.assertRaises(CmuxError):
            screen_frame(fixture)
