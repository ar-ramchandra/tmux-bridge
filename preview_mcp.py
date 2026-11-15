"""Node-local preview tools, explicitly scoped to one tmux session."""
import argparse
import os
import asyncio
from pathlib import Path
from preview_registry import PreviewRegistry


def build_server(registry, session):
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    mcp = MCPServer('tmux-bridge-previews')

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def preview_create(port: int, title: str = '', ttl_seconds: int = 7200) -> dict:
        """Make this session's loopback web app available to authorized gateway users until expiry."""
        return await asyncio.to_thread(registry.create, session, port, title, ttl_seconds)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def preview_list() -> dict:
        """List this session's live previews."""
        return {'previews': [r for r in await asyncio.to_thread(registry.listing) if r['session'] == session]}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def preview_stop(preview_id: str) -> dict:
        """Revoke preview access. Leaves the development server and source files running/intact."""
        row = await asyncio.to_thread(registry.get, preview_id)
        if row['session'] != session:
            raise ValueError('Preview belongs to another session')
        return await asyncio.to_thread(registry.stop, preview_id)
    return mcp


def main():
    CONFIG_DIR = Path(os.environ.get('TMUX_BRIDGE_AGENT_DIR', str(Path.home()/'.tmux-bridge')))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config-dir', type=Path, default=CONFIG_DIR)
    p.add_argument('--session', required=True)
    args = p.parse_args()
    registry = PreviewRegistry(args.config_dir)
    registry.settings()
    registry.identity(args.session)
    build_server(registry, args.session).run(transport='stdio')


if __name__ == '__main__':
    main()
