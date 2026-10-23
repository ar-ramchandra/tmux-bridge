"""Reconnects and slow viewers must not interrupt the terminal input owner."""
import asyncio
import time
import unittest
from unittest.mock import patch

from starlette.websockets import WebSocketState
import gateway


class Socket:
    client_state = WebSocketState.CONNECTED

    def __init__(self):
        self.messages = []
        self.closed = False
        self.block = None

    async def send_text(self, text):
        if self.block is not None:
            await self.block.wait()
        self.messages.append(text)

    async def close(self, **kwargs):
        self.closed = True


class Connection:
    server_name = 'hpc'

    def __init__(self):
        self.inputs = []
        self.controls = []

    def allocate_channel(self):
        return 1

    async def send_text(self, text):
        self.controls.append(text)

    async def send_bytes(self, data):
        self.inputs.append(data)


class InputReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = gateway.GatewayManager()
        self.conn = Connection()
        self.proxy = gateway.ProxySession(self.conn, 'test')
        self.manager.proxy_sessions['hpc/test'] = self.proxy
        self.patcher = patch.object(gateway, 'gw_manager', self.manager)
        self.patcher.start()

    async def asyncTearDown(self):
        task = self.manager._presence_broadcast_task
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.patcher.stop()

    async def join(self, identity):
        ws = Socket()
        info = gateway.ViewerInfo(ws=ws, viewer_id=identity, device='test', label='test',
                                  connected_at=time.time()-5, ready=True)
        await self.proxy.add_viewer(ws, info)
        return ws

    async def test_reconnect_transfers_control_before_old_socket_disconnects(self):
        old = await self.join('same-browser')
        new = await self.join('same-browser')
        self.assertIs(self.proxy.active_input, new)
        self.assertEqual(list(self.proxy.viewers), [new])
        self.assertTrue(await self.proxy.write_input(new, b'abc'))
        self.assertFalse(await self.proxy.write_input(old, b'old'))
        await self.proxy.remove_viewer(old)
        self.assertIs(self.proxy.active_input, new)
        self.assertTrue(self.proxy._channel_open)
        await asyncio.sleep(.01)
        self.assertTrue(old.closed)
        self.assertEqual(self.conn.inputs, [gateway.pack_data(1, b'abc')])

    async def test_reconnecting_spectator_does_not_steal_control(self):
        owner = await self.join('owner')
        old = await self.join('spectator')
        new = await self.join('spectator')
        self.assertIs(self.proxy.active_input, owner)
        self.assertNotIn(old, self.proxy.viewers)
        self.assertFalse(await self.proxy.write_input(new, b'x'))
        self.assertTrue(await self.proxy.write_input(owner, b'y'))

    async def test_agent_replacement_reconnects_existing_browser_and_restores_input(self):
        owner = await self.join('owner')
        self.proxy.buffer.extend(b'stale terminal replay')
        self.conn.ws = Socket()
        self.manager.server_connections['hpc'] = self.conn
        replacement = Connection()

        # The new agent can register before the old transport notices its loss.
        await self.manager.register_server('hpc', replacement)
        await asyncio.sleep(.02)
        self.assertTrue(owner.closed, 'Browser must not remain on a dead channel')
        self.assertFalse(self.proxy.buffer)
        self.assertFalse(self.proxy._channel_open)

        reconnected = await self.join('owner')
        self.assertTrue(await self.proxy.write_input(reconnected, b'after reconnect'))
        self.assertEqual(replacement.inputs, [gateway.pack_data(1, b'after reconnect')])
        self.assertFalse(self.conn.inputs)

    async def test_agent_replacement_does_not_disconnect_another_node(self):
        viewer = Socket()
        other = gateway.ProxySession(Connection(), 'other')
        other.viewers[viewer] = None
        other._channel_open = True
        other.channel = 8
        self.manager.proxy_sessions['other-node/other'] = other
        await self.manager.register_server('hpc', Connection())
        await asyncio.sleep(.02)
        self.assertFalse(viewer.closed)
        self.assertTrue(other._channel_open)
        self.assertEqual(other.channel, 8)

    async def test_slow_presence_send_does_not_block_consecutive_keystrokes(self):
        owner = await self.join('owner')
        slow = await self.join('slow-viewer')
        slow.block = asyncio.Event()
        for key in (b'a', b'b', b'c'):
            self.assertTrue(await asyncio.wait_for(self.proxy.write_input(owner, key), .1))
        self.assertEqual(self.conn.inputs, [gateway.pack_data(1, c) for c in (b'a', b'b', b'c')])
        slow.block.set()

    async def test_slow_viewer_does_not_delay_other_viewers_presence(self):
        slow = await self.join('slow-viewer')
        healthy = await self.join('healthy-viewer')
        slow.block = asyncio.Event()
        healthy.messages.clear()
        task = asyncio.create_task(self.manager.broadcast_presence())
        try:
            for _ in range(20):
                if healthy.messages:
                    break
                await asyncio.sleep(.005)
            self.assertTrue(healthy.messages)
            self.assertFalse(task.done(), 'Slow viewer should still be waiting')
            await asyncio.wait_for(task, 1.5)
        finally:
            slow.block.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
