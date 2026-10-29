"""Read-only MCP access to gateway activity. Run on the gateway, locally or over SSH."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

from activity import ActivityStore, LABELS


class ActivityReader:
    def __init__(self, path, scopes):
        self.store=ActivityStore(path)
        # Exact node/session pairs; a node with session '*' grants all its sessions.
        self.scopes=scopes

    def allowed(self, event):
        return any((server=='*' or event['server']==server) and
                   (session=='*' or event['session']==session) for server,session in self.scopes)

    def sessions(self):
        return [{**e,'label':LABELS[e['kind']],'outcome_verified':False}
                for e in self.store.snapshot() if self.allowed(e)]

    def events(self, after=0):
        page=self.store.page(after)
        page['events']=[{**e,'label':LABELS[e['kind']],'outcome_verified':False}
                        for e in page['events'] if self.allowed(e)]
        return page


def build_server(reader):
    from mcp.server import MCPServer
    mcp=MCPServer('Tmux Bridge activity',instructions='Read-only prompt lifecycle metadata. Response finished does not verify the user goal. Poll get_events using next_cursor; notifications do not automatically start an agent turn.')

    @mcp.tool()
    async def list_sessions() -> dict:
        """List observed provider threads in permitted sessions; no terminal text or credentials."""
        return {'sessions':await asyncio.to_thread(reader.sessions)}

    @mcp.tool()
    async def get_prompt_status(server: str, session: str, thread_id: str = '') -> dict:
        """Read the latest observed prompt status; transport silence never means completion."""
        sessions=await asyncio.to_thread(reader.sessions)
        return {'sessions':[e for e in sessions if e['server']==server and e['session']==session and (not thread_id or e['thread_id']==thread_id)]}

    @mcp.tool()
    async def get_events(after_cursor: int = 0) -> dict:
        """Read durable events. Save next_cursor even for empty pages; history_gap means older events expired."""
        return await asyncio.to_thread(reader.events,after_cursor)

    @mcp.tool()
    async def wait_for_events(after_cursor: int = 0, timeout_seconds: int = 20) -> dict:
        """Wait up to 25 seconds for permitted events; repeat with next_cursor to keep observing."""
        deadline=time.monotonic()+max(0,min(25,timeout_seconds))
        while True:
            page=await asyncio.to_thread(reader.events,after_cursor)
            if page['events'] or page['history_gap'] or time.monotonic()>=deadline:return page
            after_cursor=page['next_cursor']
            await asyncio.sleep(0.5)
    return mcp


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path.home()/'.config/tmux-bridge/activity/activity.sqlite3')
    parser.add_argument('--allow',action='append',required=True,metavar='NODE/SESSION',help='Exact node/session, NODE/*, or explicit */*. Repeat for more scopes.')
    args=parser.parse_args()
    scopes=[]
    for value in args.allow:
        pair=value.split('/',1)
        if len(pair)!=2 or not all(pair):parser.error('--allow must be NODE/SESSION')
        scopes.append(tuple(pair))
    if not args.database.is_file():parser.error('Activity database does not exist; start the gateway first.')
    build_server(ActivityReader(args.database,scopes)).run(transport='stdio')


if __name__=='__main__':main()
