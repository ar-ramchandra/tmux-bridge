"""Explicit, expiring loopback previews owned by a tmux session.

Local CLI and MCP write the same private SQLite registry as the node agent.
No Cloudflare credential or gateway administrator token is needed here.
"""
from __future__ import annotations
from contextlib import contextmanager
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import time

ID_RE = re.compile(r'[a-f0-9]{32}\Z')
# These are bridge/control services, not application preview targets.
BLOCKED_PORTS = {2375, 2376, 4601, 4603, 5900, 5901, 9222, 14610}


def session_identity(name):
    if not isinstance(name, str) or not name or len(name) > 128:
        raise ValueError('Choose an existing tmux session')
    result = subprocess.run(['tmux', 'display-message', '-p', '-t', '='+name,
                             '#{session_name}\t#{session_created}'],
                            capture_output=True, text=True, timeout=3)
    parts = result.stdout.strip().split('\t')
    if result.returncode or len(parts) != 2 or parts[0] != name:
        raise ValueError('The owning tmux session does not exist')
    return int(parts[1])


class PreviewRegistry:
    def __init__(self, root, identity=session_identity):
        self.root, self.identity = Path(root), identity

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root/'previews.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        db = sqlite3.connect(path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS previews (id TEXT PRIMARY KEY, session TEXT, created INTEGER, port INTEGER, title TEXT, expires REAL)')
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        try:
            data = json.loads((self.root/'previews.json').read_text())
            suffix = data.get('host_suffix', '')
            if data.get('enabled') is True and re.fullmatch(r'-preview\.[a-z0-9.-]+', suffix):
                return suffix
        except (OSError, ValueError, AttributeError):
            pass
        raise ValueError('Private previews are not configured on this device')

    def listing(self):
        suffix = self.settings()
        with self.db() as db:
            db.execute('DELETE FROM previews WHERE expires <= ?', (time.time(),))
            rows = [dict(r) for r in db.execute('SELECT * FROM previews ORDER BY expires')]
            live = []
            for row in rows:
                try:
                    valid = self.identity(row['session']) == row['created']
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    valid = False
                if valid:
                    row['url'] = 'https://'+row['id']+suffix+'/'
                    live.append(row)
                else:
                    db.execute('DELETE FROM previews WHERE id = ?', (row['id'],))
            return live

    def create(self, session, port, title='', ttl=7200):
        self.settings()
        if type(port) is not int or not 1024 <= port <= 65535 or port in BLOCKED_PORTS:
            raise ValueError('Choose an application port between 1024 and 65535; control service ports are excluded')
        if type(ttl) is not int or not 60 <= ttl <= 86400:
            raise ValueError('Expiry must be between 60 seconds and 24 hours')
        if not isinstance(title, str) or len(title) > 80 or any(ord(c) < 32 for c in title):
            raise ValueError('Title must be at most 80 printable characters')
        created = self.identity(session)
        live = self.listing()
        # Repeated clicks reuse the same live preview without extending its life.
        for row in live:
            if row['session'] == session and row['port'] == port:
                return row
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM previews').fetchone()[0] >= 16:
                raise ValueError('Stop an existing preview first (16 per device)')
            ident = secrets.token_hex(16)
            db.execute('INSERT INTO previews VALUES (?,?,?,?,?,?)',
                       (ident, session, created, port, title or 'Port '+str(port), time.time()+ttl))
        return next(p for p in self.listing() if p['id'] == ident)

    def get(self, ident):
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            raise ValueError('Invalid preview identifier')
        for row in self.listing():
            if row['id'] == ident:
                return row
        raise ValueError('Preview has expired or stopped')

    def stop(self, ident):
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            raise ValueError('Invalid preview identifier')
        with self.db() as db:
            db.execute('DELETE FROM previews WHERE id = ?', (ident,))
        return {'stopped': ident}


def main():
    CONFIG_DIR = Path(os.environ.get('TMUX_BRIDGE_AGENT_DIR', str(Path.home()/'.tmux-bridge')))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config-dir', type=Path, default=CONFIG_DIR)
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('create')
    c.add_argument('--session', required=True)
    c.add_argument('--port', type=int, required=True)
    c.add_argument('--title', default='')
    c.add_argument('--ttl', type=int, default=7200)
    sub.add_parser('list')
    sub.add_parser('stop').add_argument('id')
    args = p.parse_args()
    reg = PreviewRegistry(args.config_dir)
    try:
        result = reg.create(args.session, args.port, args.title, args.ttl) if args.action == 'create' else reg.stop(args.id) if args.action == 'stop' else reg.listing()
    except (ValueError, OSError) as e:
        p.error(str(e))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
