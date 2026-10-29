"""Versioned, bounded activity records. No terminal scraping or prompt storage."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager

LABELS = {
    'running': 'Working', 'waiting': 'Needs input', 'turn_finished': 'Response finished',
    'failed': 'Failed', 'interrupted': 'Interrupted', 'idle': 'Idle',
    'task_completed': 'Task reported complete', 'goal_completed': 'Goal reported complete',
    'stopped': 'Session ended',
}
ALERT_KINDS = {'waiting', 'turn_finished', 'failed', 'task_completed', 'goal_completed'}


def clean_text(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('Invalid activity text')
    if required and not value.strip():
        raise ValueError('Required activity field is empty')
    return value


def validate_event(data):
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Unsupported activity event')
    event = {'version': 1}
    for key, limit in [('id', 128), ('provider', 32), ('thread_id', 200),
                       ('session', 200), ('turn_id', 200), ('summary', 500), ('evidence', 2000)]:
        event[key] = clean_text(data.get(key, ''), limit, key in ('id', 'provider', 'thread_id'))
    if data.get('kind') not in LABELS:
        raise ValueError('Unknown activity kind')
    event['kind'] = data['kind']
    stamp = data.get('time')
    if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError('Invalid activity time')
    event['time'] = stamp
    if event['kind'] == 'goal_completed' and not (event['summary'].strip() and event['evidence'].strip()):
        raise ValueError('A goal completion needs a summary and verification evidence')
    return event


def new_event(provider, thread_id, kind, **fields):
    return validate_event(dict(version=1, id=str(uuid.uuid4()), time=time.time(),
                               provider=provider, thread_id=thread_id, kind=kind, **fields))


class ActivityStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT NOT NULL,
                    event_id TEXT NOT NULL, body TEXT NOT NULL, received REAL NOT NULL,
                    UNIQUE(server,event_id));
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings (
                    provider TEXT, thread_id TEXT, session TEXT NOT NULL,
                    PRIMARY KEY(provider,thread_id));
                CREATE TABLE IF NOT EXISTS current_activity (
                    server TEXT, provider TEXT, thread_id TEXT, body TEXT, seq INTEGER,
                    PRIMARY KEY(server,provider,thread_id));
                CREATE TABLE IF NOT EXISTS turns (
                    provider TEXT, thread_id TEXT, turn_id TEXT,
                    PRIMARY KEY(provider,thread_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, provider, thread_id, session):
        for val, limit in [(provider,32),(thread_id,200),(session,200)]:
            clean_text(val, limit, True)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO bindings VALUES (?,?,?)', (provider,thread_id,session))

    def binding(self, provider, thread_id):
        with self.connect() as db:
            row = db.execute('SELECT session FROM bindings WHERE provider=? AND thread_id=?', (provider,thread_id)).fetchone()
            return row[0] if row else ''

    def enqueue(self, event):
        event = validate_event(event)
        with self.connect() as db:
            if db.execute('SELECT count(*) FROM outbox').fetchone()[0] >= 10000:
                raise ValueError('Activity outbox full; check node connectivity')
            db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?)', (event['id'],json.dumps(event)))
        return event

    def correlate(self, event, new_turn=False):
        """Give providers without native turn IDs a stable local prompt identity."""
        with self.connect() as db:
            row = db.execute('SELECT turn_id FROM turns WHERE provider=? AND thread_id=?',
                             (event['provider'],event['thread_id'])).fetchone()
            if not event['turn_id']:
                event['turn_id'] = str(uuid.uuid4()) if new_turn or (event['kind']=='running' and not row) else (row[0] if row else '')
            if event['turn_id']:
                db.execute('INSERT OR REPLACE INTO turns VALUES (?,?,?)',
                           (event['provider'],event['thread_id'],event['turn_id']))
        return event

    def pending(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT body FROM outbox ORDER BY rowid LIMIT 50')]

    def acknowledge(self, ids):
        if not isinstance(ids, list) or len(ids) > 50 or not all(isinstance(x,str) for x in ids):
            raise ValueError('Invalid activity acknowledgement')
        with self.connect() as db:
            db.executemany('DELETE FROM outbox WHERE id=?', [(x,) for x in ids])

    def receive(self, server, event):
        event = validate_event(event)
        clean_text(server, 128, True)
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO events(server,event_id,body,received) VALUES (?,?,?,?)',
                                (server,event['id'],json.dumps(event),time.time()))
            inserted = bool(cursor.rowcount)
            row = db.execute('SELECT seq FROM events WHERE server=? AND event_id=?', (server,event['id'])).fetchone()
            if inserted:
                previous = db.execute('SELECT body FROM current_activity WHERE server=? AND provider=? AND thread_id=?',
                                      (server,event['provider'],event['thread_id'])).fetchone()
                if not previous or json.loads(previous[0])['time'] <= event['time']:
                    db.execute('INSERT OR REPLACE INTO current_activity VALUES (?,?,?,?,?)',
                               (server,event['provider'],event['thread_id'],json.dumps(event),row[0]))
            # Bounded history; clients receive an explicit gap indication after expiry.
            db.execute('DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq),0)-100000 FROM events) OR (received < ? AND seq < (SELECT COALESCE(MAX(seq),0)-2000 FROM events))', (time.time()-30*86400,))
            db.execute('DELETE FROM current_activity WHERE seq < (SELECT COALESCE(MAX(seq),0)-100000 FROM events)')
        return {**event, 'server': server, 'seq': row[0]}, inserted

    def snapshot(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM current_activity ORDER BY seq').fetchall()
            return [{**json.loads(r['body']), 'server':r['server'], 'seq':r['seq']} for r in rows]

    def page(self, after=0, limit=100):
        after=max(0,int(after))
        with self.connect() as db:
            first=db.execute('SELECT COALESCE(MIN(seq),0) FROM events').fetchone()[0]
            rows=db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?',
                            (after,min(200,max(1,int(limit))))).fetchall()
            events=[{**json.loads(r['body']),'server':r['server'],'seq':r['seq']} for r in rows]
            return {'events':events,'next_cursor':events[-1]['seq'] if events else after,
                    'history_gap':bool(after and first>after+1)}

    def list(self, after=0, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?', (max(0,int(after)),min(200,max(1,int(limit))))).fetchall()
            return [{**json.loads(r['body']), 'server': r['server'], 'seq': r['seq']} for r in rows]

    def latest(self):
        with self.connect() as db:
            return db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]


async def forward_activity(ws, root):
    """Retry until the gateway acknowledges durable storage, across reconnects."""
    import asyncio
    store = None
    last_error = 0
    while True:
        try:
            if store is None:
                store = await offload(ActivityStore, Path(root)/'activity.sqlite3')
            events = await offload(store.pending)
            if events:
                await ws.send(json.dumps({'type':'activity_events','events':events}))
        except Exception as error:
            # A transient metadata-store failure must not stop terminal transport.
            if time.monotonic()-last_error>60:
                print('[ACTIVITY] Delivery will retry: '+type(error).__name__,flush=True)
                last_error=time.monotonic()
        await asyncio.sleep(2)


async def acknowledge_activity(root, ids):
    import asyncio
    def update():
        ActivityStore(Path(root)/'activity.sqlite3').acknowledge(ids)
    await offload(update)


async def offload(function, *args):
    """Keep legacy Python 3.8 node agents working without changing their runtime."""
    import asyncio
    import functools
    return await asyncio.get_running_loop().run_in_executor(None,functools.partial(function,*args))
