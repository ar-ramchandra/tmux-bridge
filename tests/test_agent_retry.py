import unittest
from unittest.mock import AsyncMock, patch

import server


class AgentRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_registration_resets_backoff_even_after_abrupt_disconnect(self):
        agent=server.ServerAgent({'gateway':'http://localhost','name':'test'})
        # Two failed attempts, then two real connections ending abruptly.
        registered=iter([False,False,True,True])
        delays=[]
        async def connect():
            agent._registered=next(registered)
            raise OSError('simulated connection loss')
        async def sleep(delay):
            delays.append(delay)
            if len(delays)==4:agent._running=False
        with patch.object(agent.manager,'start',new=AsyncMock()), \
             patch.object(agent,'_connect',side_effect=connect), \
             patch.object(server,'_collect_metrics',new=AsyncMock()), \
             patch.object(server.asyncio,'sleep',side_effect=sleep):
            await agent.run()
        initial=server.RECONNECT_DELAY
        self.assertEqual(delays,[initial,initial*2,initial,initial])
