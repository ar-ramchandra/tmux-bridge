"""Exercise the real MCP stdio handshake and tools, with no model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from activity import ActivityStore,new_event

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession=None


@unittest.skipUnless(ClientSession,'Install the mcp optional dependency')
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_discovery_scoped_reads_and_completion_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'activity.sqlite3';store=ActivityStore(path)
            store.receive('private',new_event('claude','private-thread','running',session='work'))
            event,_=store.receive('allowed',new_event('codex','thread','turn_finished',session='work'))
            params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve().parents[1]/'activity_mcp.py'),'--database',str(path),'--allow','allowed/work'],env={k:v for k,v in os.environ.items() if k!='PYTHONPATH'})
            async with stdio_client(params) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    names={t.name for t in (await client.list_tools()).tools}
                    self.assertEqual(names,{'list_sessions','get_prompt_status','get_events','wait_for_events'})
                    result=await client.call_tool('get_events',{'after_cursor':0})
                    self.assertFalse(result.is_error)
                    body=json.loads(result.content[0].text)
                    self.assertEqual(len(body['events']),1)
                    self.assertEqual(body['events'][0]['label'],'Response finished')
                    self.assertNotIn('private-thread',json.dumps(body))
                    result=await client.call_tool('wait_for_events',{'after_cursor':event['seq'],'timeout_seconds':0})
                    self.assertEqual(json.loads(result.content[0].text)['events'],[])
