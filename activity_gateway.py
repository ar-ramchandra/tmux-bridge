"""Activity API and optional Web Push. Shares existing gateway admission."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from activity import ActivityStore, ALERT_KINDS, LABELS

# Give the foreground poll time to acknowledge delivery before sending a push.
FOREGROUND_GRACE_SECONDS = 6


def notification_preferences(value):
    if not isinstance(value,dict):raise ValueError('Invalid preferences')
    kinds=value.get('kinds',['turn_finished','waiting','failed'])
    servers=value.get('servers',[])
    if not isinstance(kinds,list) or not all(k in ALERT_KINDS for k in kinds):raise ValueError('Invalid notification kinds')
    if not isinstance(servers,list) or len(servers)>100 or not all(isinstance(s,str) and 0<len(s)<=128 for s in servers):raise ValueError('Invalid server filter')
    return {'kinds':list(dict.fromkeys(kinds)),'servers':list(dict.fromkeys(servers))}


def validate_subscription(value):
    endpoint = value.get('endpoint','') if isinstance(value,dict) else ''
    parsed = urlsplit(endpoint)
    # Browser-provided URLs must never become an arbitrary server-side fetch.
    if (len(endpoint)>4096 or parsed.scheme!='https' or parsed.hostname not in
        {'fcm.googleapis.com','updates.push.services.mozilla.com','web.push.apple.com'} or
        parsed.port not in (None,443) or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('Unsupported push service endpoint')
    keys = value.get('keys',{})
    for name, size in [('p256dh',65),('auth',16)]:
        raw = keys.get(name,'')
        if not isinstance(raw,str) or len(raw)>128:
            raise ValueError('Invalid push key')
        try:
            decoded=base64.b64decode(raw+'='*(-len(raw)%4),altchars=b'-_',validate=True)
        except Exception:
            raise ValueError('Invalid push key') from None
        if len(decoded)!=size:
            raise ValueError('Invalid push key size')
    return {'endpoint':endpoint,'keys':{'p256dh':keys['p256dh'],'auth':keys['auth']}}


class ActivityGateway:
    def __init__(self, root):
        self.root=Path(root)
        self.store=ActivityStore(self.root/'activity.sqlite3')
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id TEXT PRIMARY KEY, endpoint TEXT UNIQUE, body TEXT, start_seq INTEGER);
                CREATE TABLE IF NOT EXISTS push_jobs (
                    seq INTEGER, subscription_id TEXT, attempts INTEGER DEFAULT 0,
                    next_attempt REAL DEFAULT 0, status TEXT DEFAULT 'pending',
                    PRIMARY KEY(seq,subscription_id));
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(push_subscriptions)')}
            if 'preferences' not in columns:
                db.execute("ALTER TABLE push_subscriptions ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")
            db.execute('CREATE TABLE IF NOT EXISTS push_cursor (id INTEGER PRIMARY KEY, seq INTEGER)')
            db.execute('INSERT OR IGNORE INTO push_cursor VALUES (1,0)')

    def push_available(self):
        return importlib.util.find_spec('pywebpush') is not None

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        path=self.root/'push-private.pem'
        if not path.exists():
            key=ec.generate_private_key(ec.SECP256R1())
            data=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
            try:
                fd=os.open(str(path),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd,'wb') as f:f.write(data)
        key=serialization.load_pem_private_key(path.read_bytes(),password=None)
        data=key.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(data).decode().rstrip('=')

    def subscribe(self, subscription, preferences=None):
        subscription=validate_subscription(subscription)
        prefs=json.dumps(notification_preferences(preferences or {}))
        with self.store.connect() as db:
            row=db.execute('SELECT id FROM push_subscriptions WHERE endpoint=?',(subscription['endpoint'],)).fetchone()
            if row:
                db.execute('UPDATE push_subscriptions SET body=?,preferences=? WHERE id=?',(json.dumps(subscription),prefs,row[0]))
                return row[0]
            if db.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0]>=100:
                raise ValueError('Push subscription limit reached')
            identity=str(uuid.uuid4())
            latest=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            db.execute('INSERT INTO push_subscriptions VALUES (?,?,?,?,?)',
                       (identity,subscription['endpoint'],json.dumps(subscription),latest,prefs))
            return identity

    def unsubscribe(self, identity):
        with self.store.connect() as db:
            db.execute('DELETE FROM push_subscriptions WHERE id=?',(identity,))
            db.execute('DELETE FROM push_jobs WHERE subscription_id=?',(identity,))

    def acknowledge_foreground(self, identity, sequences):
        if (not isinstance(sequences,list) or len(sequences)>200 or
            any(type(seq) is not int or seq<=0 or seq>2**63-1 for seq in sequences)):
            raise ValueError('Invalid event receipts')
        with self.store.connect() as db:
            sub=db.execute('SELECT start_seq FROM push_subscriptions WHERE id=?',(identity,)).fetchone()
            if sub is None:raise ValueError('Unknown subscription')
            for seq in sequences:
                # Only this device's explicitly received events are suppressed.
                # A locked, disconnected, or suspended app cannot acknowledge.
                db.execute('''INSERT INTO push_jobs(seq,subscription_id,status)
                    SELECT seq,?,'foreground' FROM events WHERE seq=? AND seq>?
                    ON CONFLICT(seq,subscription_id) DO UPDATE SET status='foreground'
                    WHERE push_jobs.status='pending' ''',(identity,seq,sub['start_seq']))

    def send_push(self, subscription, payload, ttl):
        from pywebpush import webpush
        self.public_key()
        # py-vapid's strict subject validator accepts HTTPS origins, not URL
        # paths. Keep the default compatible; deployments can set a contact URI.
        return webpush(subscription_info=subscription,data=json.dumps(payload),
                       vapid_private_key=str(self.root/'push-private.pem'),
                       vapid_claims={'sub':os.environ.get('TMUX_BRIDGE_PUSH_SUBJECT','https://github.com')},
                       ttl=ttl,timeout=8)

    @staticmethod
    def log_push_error(error):
        # Exceptions can contain subscription URLs and JWTs: never log their text.
        code=getattr(getattr(error,'response',None),'status_code',None)
        print('[ACTIVITY] Push delivery failed: '+type(error).__name__+
              (' HTTP '+str(code) if code is not None else ''),flush=True)

    def dispatch_push(self):
        if not self.push_available():return
        from pywebpush import WebPushException
        # Recover after a process restart, but don't notify about old history.
        with self.store.connect() as db:
            cursor=db.execute('SELECT seq FROM push_cursor WHERE id=1').fetchone()[0]
            rows=db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 500',(cursor,)).fetchall()
            subscriptions=db.execute('SELECT * FROM push_subscriptions').fetchall()
            for row in rows:
                event=json.loads(row['body'])
                for sub in subscriptions:
                    prefs=notification_preferences(json.loads(sub['preferences']))
                    if (row['seq']>sub['start_seq'] and row['received']>time.time()-3600 and event['kind'] in prefs['kinds']
                        and (not prefs['servers'] or row['server'] in prefs['servers'])):
                        db.execute('INSERT OR IGNORE INTO push_jobs(seq,subscription_id,next_attempt) VALUES (?,?,?)',
                                   (row['seq'],sub['id'],row['received']+FOREGROUND_GRACE_SECONDS))
            if rows:db.execute('UPDATE push_cursor SET seq=? WHERE id=1',(rows[-1]['seq'],))
            jobs=db.execute('''SELECT j.*,s.body AS subscription,s.preferences,e.body AS event,e.server FROM push_jobs j
                JOIN push_subscriptions s ON j.subscription_id=s.id JOIN events e ON e.seq=j.seq
                WHERE j.status IN ('pending','sending') AND j.next_attempt<=? ORDER BY j.seq LIMIT 10''',(time.time(),)).fetchall()
            db.execute('DELETE FROM push_jobs WHERE seq NOT IN (SELECT seq FROM events)')
        for job in jobs:
            # Atomically arbitrate against a foreground receipt. Recover this
            # claim after a crash; never strand a notification in 'sending'.
            with self.store.connect() as db:
                claimed=db.execute("""UPDATE push_jobs SET status='sending',next_attempt=?
                    WHERE seq=? AND subscription_id=? AND status IN ('pending','sending') AND next_attempt<=?""",
                    (time.time()+60,job['seq'],job['subscription_id'],time.time())).rowcount
            if not claimed:continue
            event=json.loads(job['event'])
            prefs=notification_preferences(json.loads(job['preferences']))
            if event['kind'] not in prefs['kinds'] or (prefs['servers'] and job['server'] not in prefs['servers']):
                with self.store.connect() as db:db.execute("UPDATE push_jobs SET status='skipped' WHERE seq=? AND subscription_id=?",(job['seq'],job['subscription_id']))
                continue
            # No prompts, summaries, paths, or terminal text on the lock screen.
            payload={'title':'Tmux Bridge','body':event['provider']+': '+LABELS[event['kind']],
                     'tag':'bridge-activity-'+str(job['seq']),'url':'/?activity='+str(job['seq'])}
            status='sent'
            try:
                self.send_push(json.loads(job['subscription']),payload,ttl=3600)
            except WebPushException as exc:
                self.log_push_error(exc)
                code=getattr(exc.response,'status_code',None)
                if code in (404,410):
                    self.unsubscribe(job['subscription_id']);continue
                status='failed' if job['attempts']>=4 else 'pending'
            except Exception as exc:
                self.log_push_error(exc)
                status='failed' if job['attempts']>=4 else 'pending'
            with self.store.connect() as db:
                db.execute('UPDATE push_jobs SET status=?,attempts=attempts+1,next_attempt=? WHERE seq=? AND subscription_id=?',
                           (status,time.time()+min(3600,30*2**job['attempts']),job['seq'],job['subscription_id']))

    async def worker(self):
        while True:
            try:await asyncio.to_thread(self.dispatch_push)
            except Exception as error:print('[ACTIVITY] Push worker: '+type(error).__name__,flush=True)
            await asyncio.sleep(5)

    async def receive(self, server, events):
        if not isinstance(events,list) or len(events)>50:raise ValueError('Invalid activity batch')
        ids=[]
        for event in events:
            try:
                saved,_=await asyncio.to_thread(self.store.receive,server,event)
                ids.append(saved['id'])
            except (ValueError,TypeError):
                # Reject malformed metadata without dropping terminal transport.
                if isinstance(event,dict) and isinstance(event.get('id'),str):ids.append(event['id'])
        return ids

    def router(self):
        router=APIRouter()
        @router.get('/api/activity')
        async def activity(after:int=0):
            def read():
                return {**self.store.page(after),'labels':LABELS}
            return await asyncio.to_thread(read)
        @router.get('/api/activity/snapshot')
        async def snapshot():
            # Cursor first: concurrent events can be replayed, never skipped.
            def read():return {'cursor':self.store.latest(),'sessions':self.store.snapshot(),'labels':LABELS}
            return await asyncio.to_thread(read)
        @router.get('/activity.js')
        async def script():
            return Response((Path(__file__).parent/'tmux_bridge_assets/activity.js').read_text(),media_type='application/javascript')
        @router.get('/api/activity/push')
        async def push_info():
            if not self.push_available():return {'available':False}
            return {'available':True,'public_key':await asyncio.to_thread(self.public_key)}
        @router.post('/api/activity/push')
        async def subscribe(request:Request):
            if not self.push_available():return JSONResponse({'error':'Push dependency not installed'},status_code=503)
            raw=await request.body()
            if len(raw)>16384:return JSONResponse({'error':'Subscription too large'},status_code=413)
            try:
                value=json.loads(raw)
                identity=await asyncio.to_thread(self.subscribe,value['subscription'],value.get('preferences',{}))
                return {'id':identity}
            except (ValueError,TypeError,KeyError,AttributeError):return JSONResponse({'error':'Invalid push subscription'},status_code=400)
        @router.post('/api/activity/push/{identity}/test')
        async def test_push(identity:str):
            def send():
                with self.store.connect() as db:
                    row=db.execute('SELECT body FROM push_subscriptions WHERE id=?',(identity,)).fetchone()
                if not row:raise ValueError('No subscription')
                self.send_push(json.loads(row[0]),{'title':'Tmux Bridge','body':'Notifications are enabled on this device.','tag':'bridge-test','url':'/'},ttl=60)
            try:await asyncio.to_thread(send);return {'ok':True}
            except Exception as error:
                self.log_push_error(error)
                return JSONResponse({'error':'Push delivery failed on the gateway. Your device is still registered; please retry the test.'},status_code=502)
        @router.post('/api/activity/push/{identity}/received')
        async def foreground_receipt(identity:str,request:Request):
            raw=await request.body()
            if len(raw)>8192:return JSONResponse({'error':'Too many event receipts'},status_code=413)
            try:
                value=json.loads(raw)
                await asyncio.to_thread(self.acknowledge_foreground,identity,value['sequences'])
                return {'ok':True}
            except (ValueError,TypeError,KeyError):
                return JSONResponse({'error':'Invalid event receipts'},status_code=400)
        @router.delete('/api/activity/push/{identity}')
        async def unsubscribe(identity:str):
            await asyncio.to_thread(self.unsubscribe,identity);return {'ok':True}
        @router.get('/activity-sw.js')
        async def service_worker():
            return Response(SERVICE_WORKER,media_type='application/javascript',headers={'Service-Worker-Allowed':'/'})
        return router


SERVICE_WORKER=r'''
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('push',event=>{
 let data={};try{data=event.data.json();}catch{}
 event.waitUntil(self.registration.showNotification(data.title||'Tmux Bridge',{
  body:data.body||'A session has an update',tag:data.tag||'bridge-activity',
  icon:'/icon.svg',data:{url:/^\/\?activity=\d+$/.test(data.url)?data.url:'/'},
 }));
});
self.addEventListener('notificationclick',event=>{
 event.notification.close();
 event.waitUntil((async()=>{
  const url=new URL(event.notification.data?.url||'/',self.location.origin).href;
  const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
  for(const client of windows){if(new URL(client.url).origin===self.location.origin){await client.navigate(url);return client.focus();}}
  return self.clients.openWindow(url);
 })());
});
'''
