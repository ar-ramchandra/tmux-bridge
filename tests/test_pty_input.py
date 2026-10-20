"""Large pastes must survive partial writes and terminal backpressure."""
import asyncio
import os
import pty
import tty
import unittest
from unittest.mock import patch

import server


class PTYInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.master,self.slave=pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master,False)
        os.set_blocking(self.slave,False)
        self.session=server.TmuxSession('isolated-input-test',1,None)
        self.session.master_fd=self.master

    async def asyncTearDown(self):
        with patch.object(server.subprocess,'run'):
            await self.session.stop()
        os.close(self.slave)

    async def read_bytes(self,count):
        received=bytearray()
        while len(received)<count:
            try:
                received.extend(os.read(self.slave,8192))
            except BlockingIOError:
                await asyncio.sleep(.001)
        return bytes(received)

    async def test_large_paste_and_following_keys_preserve_every_byte_in_order(self):
        payload=bytes(range(256))*1024
        self.assertTrue(self.session.write_input(payload))
        # Let the real PTY fill before the application begins reading.
        ticks=[]
        for _ in range(5):
            await asyncio.sleep(.01)
            ticks.append(True)
        self.assertTrue(self.session.write_input(b'FOLLOWING-KEYS'))
        expected=payload+b'FOLLOWING-KEYS'
        actual=await asyncio.wait_for(self.read_bytes(len(expected)),3)
        self.assertEqual(actual,expected)
        self.assertEqual(len(ticks),5)

    async def test_stop_removes_pending_writer_and_disables_further_input(self):
        self.assertTrue(self.session.write_input(b'x'*262144))
        with patch.object(server.subprocess,'run'):
            await self.session.stop()
        await asyncio.sleep(.02)
        self.assertFalse(self.session.write_input(b'closed'))
        self.assertIsNone(self.session._input_writer_fd)
        self.assertEqual(self.session._input_buffer,b'')

    async def test_queue_limit_rejects_excess_instead_of_silently_truncating(self):
        self.assertFalse(self.session.write_input(b'x'*(2**22+1)))


if __name__=='__main__':
    unittest.main()
