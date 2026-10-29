import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from activity import ActivityStore, new_event, forward_activity
from activity_gateway import ActivityGateway, validate_subscription
from activity_mcp import ActivityReader
from integrations import normalize, record, install_hooks


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=ActivityStore(self.root/'activity.sqlite3')

    def event(self,kind='turn_finished',**kwargs):
        return new_event('codex','thread',kind,session='work',turn_id='turn',**kwargs)

    def test_replay_deduplicates_and_cursor_does_not_skip_later_events(self):
        event=self.event();first,inserted=self.store.receive('node',event)
        self.assertTrue(inserted)
        second,inserted=ActivityStore(self.store.path).receive('node',event)
        self.assertFalse(inserted);self.assertEqual(first['seq'],second['seq'])
        page=self.store.page(0)
        next_event,_=self.store.receive('node',self.event('running'))
        self.assertEqual(self.store.page(page['next_cursor'])['events'],[next_event])

    def test_old_delivery_does_not_overwrite_newer_prompt_status(self):
        older=self.event('running');newer=self.event()
        self.store.receive('node',newer);self.store.receive('node',older)
        self.assertEqual(self.store.snapshot()[0]['kind'],'turn_finished')

    def test_scope_filters_before_exposing_results_and_cursor_advances(self):
        self.store.receive('private',self.event())
        reader=ActivityReader(self.store.path,[('allowed','work')])
        page=reader.events();self.assertEqual(page['events'],[]);self.assertGreater(page['next_cursor'],0)
        self.store.receive('allowed',self.event())
        self.assertEqual(len(reader.events(page['next_cursor'])['events']),1)
        self.assertEqual([e['server'] for e in reader.sessions()],['allowed'])

    def test_no_subagent_completion_or_raw_prompt_storage(self):
        self.assertIsNone(normalize('claude',{'hook_event_name':'SubagentStop','session_id':'x'}))
        with patch('integrations.detect_session',return_value='work'):
            start=record('claude',{'hook_event_name':'UserPromptSubmit','session_id':'x','prompt':'PRIVATE PROMPT'},self.root)
            finish=record('claude',{'hook_event_name':'Stop','session_id':'x','last_assistant_message':'PRIVATE ANSWER'},self.root)
            again=record('claude',{'hook_event_name':'UserPromptSubmit','session_id':'x'},self.root)
        self.assertEqual(start['turn_id'],finish['turn_id'])
        self.assertNotEqual(start['turn_id'],again['turn_id'])
        self.assertNotIn('PRIVATE',json.dumps(self.store.pending()))
        self.assertEqual(finish['session'],'work')

    def test_hook_install_preserves_existing_settings_and_is_idempotent(self):
        settings=self.root/'.claude/settings.json';settings.parent.mkdir()
        settings.write_text(json.dumps({'theme':'dark','hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing'}]}]}}))
        install_hooks('claude',self.root,home=self.root)
        first=json.loads(settings.read_text());install_hooks('claude',self.root,home=self.root)
        self.assertEqual(first,json.loads(settings.read_text()));self.assertEqual(first['theme'],'dark')
        self.assertEqual(len(first['hooks']['Stop']),2)

    def test_gateway_invalid_event_does_not_drop_good_events(self):
        gateway=ActivityGateway(self.root/'gateway')
        ids=asyncio.run(gateway.receive('authenticated-node',[{'id':'bad','version':9},self.event()]))
        self.assertEqual(len(ids),2)
        self.assertEqual(gateway.store.snapshot()[0]['server'],'authenticated-node')

    def test_history_gap_is_explicit(self):
        for _ in range(4):self.store.receive('node',self.event())
        with self.store.connect() as db:db.execute('DELETE FROM events WHERE seq<4')
        self.assertTrue(self.store.page(1)['history_gap'])

    def subscription(self,host='web.push.apple.com'):
        enc=lambda n:base64.urlsafe_b64encode(bytes(n)).decode().rstrip('=')
        return {'endpoint':'https://'+host+'/test','keys':{'p256dh':enc(65),'auth':enc(16)}}

    def test_push_rejects_private_and_credential_urls(self):
        for host in ['127.0.0.1','localhost','web.push.apple.com.evil.test','user@web.push.apple.com']:
            with self.assertRaises(ValueError):validate_subscription(self.subscription(host))

    def test_per_device_filters_restart_dedup_and_key_rotation(self):
        gateway=ActivityGateway(self.root/'gateway')
        identity=gateway.subscribe(self.subscription(),{'kinds':['waiting'],'servers':['node']})
        gateway.store.receive('node',self.event())
        gateway.store.receive('other',self.event('waiting'))
        gateway.store.receive('node',self.event('waiting'))
        with patch('activity_gateway.FOREGROUND_GRACE_SECONDS',0),patch('pywebpush.webpush') as send:
            gateway.dispatch_push();self.assertEqual(send.call_count,1)
            payload=json.loads(send.call_args.kwargs['data'])
            self.assertNotIn('thread',json.dumps(payload));self.assertNotIn('work',json.dumps(payload))
            ActivityGateway(self.root/'gateway').dispatch_push();self.assertEqual(send.call_count,1)
        self.assertEqual(identity,gateway.subscribe(self.subscription(),{'kinds':[]}))
        gateway.store.receive('node',self.event('waiting'))
        with patch('activity_gateway.FOREGROUND_GRACE_SECONDS',0),patch('pywebpush.webpush') as send:gateway.dispatch_push();send.assert_not_called()

    def test_api_snapshot_script_and_validation(self):
        gateway=ActivityGateway(self.root/'gateway');app=FastAPI();app.include_router(gateway.router())
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/activity/snapshot').json()['sessions'],[])
            self.assertIn('bridgeActivity',client.get('/activity.js').text)
            self.assertEqual(client.post('/api/activity/push',json=[]).status_code,400)

    def test_foreground_receipt_suppresses_only_that_device_and_event(self):
        gateway=ActivityGateway(self.root/'gateway')
        phone=gateway.subscribe(self.subscription())
        other=self.subscription();other['endpoint']+='-other'
        laptop=gateway.subscribe(other)
        first,_=gateway.store.receive('node',self.event())
        second,_=gateway.store.receive('node',self.event())
        app=FastAPI();app.include_router(gateway.router())
        with TestClient(app) as client:
            url='/api/activity/push/'+phone+'/received'
            self.assertEqual(client.post(url,json={'sequences':[first['seq']]}).status_code,200)
            for invalid in [None,{},[True],[-1],[2**64],[1]*201]:
                self.assertEqual(client.post(url,json={'sequences':invalid}).status_code,400)
        with patch('activity_gateway.time.time',return_value=time.time()+10),patch('pywebpush.webpush') as send:
            gateway.dispatch_push();self.assertEqual(send.call_count,3)
            gateway.dispatch_push();self.assertEqual(send.call_count,3)
        with gateway.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM push_jobs WHERE seq=? AND subscription_id=?',
                                       (first['seq'],phone)).fetchone()[0],'foreground')

    def test_background_grace_and_receipt_after_enqueue_survive_restart(self):
        gateway=ActivityGateway(self.root/'gateway')
        phone=gateway.subscribe(self.subscription())
        first,_=gateway.store.receive('node',self.event())
        second,_=gateway.store.receive('node',self.event())
        with patch('pywebpush.webpush') as send:
            gateway.dispatch_push();send.assert_not_called()
            gateway.acknowledge_foreground(phone,[first['seq']])
            with patch('activity_gateway.time.time',return_value=time.time()+10):
                ActivityGateway(gateway.root).dispatch_push()
            self.assertEqual(send.call_count,1)
            self.assertIn(str(second['seq']),json.loads(send.call_args.kwargs['data'])['tag'])

    def test_interrupted_send_recovers_and_receipt_cannot_relabel_sent_push(self):
        gateway=ActivityGateway(self.root/'gateway')
        phone=gateway.subscribe(self.subscription())
        event,_=gateway.store.receive('node',self.event())
        gateway.dispatch_push()
        with gateway.store.connect() as db:
            db.execute("UPDATE push_jobs SET status='sending',next_attempt=0")
        with patch('pywebpush.webpush') as send:
            ActivityGateway(gateway.root).dispatch_push();self.assertEqual(send.call_count,1)
        gateway.acknowledge_foreground(phone,[event['seq']])
        with gateway.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM push_jobs').fetchone()[0],'sent')

    def test_real_push_signing_and_encryption_for_test_and_background_delivery(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        import jwt
        import requests
        subscriber=ec.generate_private_key(ec.SECP256R1())
        public=subscriber.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        subscription=self.subscription()
        subscription['keys']['p256dh']=base64.urlsafe_b64encode(public).decode().rstrip('=')
        gateway=ActivityGateway(self.root/'gateway')
        identity=gateway.subscribe(subscription)
        app=FastAPI();app.include_router(gateway.router())
        response=requests.Response();response.status_code=201;response._content=b''
        # Only replace HTTP transport. Exercise real VAPID validation, signing,
        # and payload encryption; mocking webpush itself hid the original bug.
        with patch('activity_gateway.FOREGROUND_GRACE_SECONDS',0),patch.dict(os.environ,{},clear=True),patch('requests.post',return_value=response) as post:
            with TestClient(app) as client:
                result=client.post('/api/activity/push/'+identity+'/test')
                self.assertEqual(result.status_code,200,result.text)
            gateway.store.receive('node',self.event())
            gateway.dispatch_push()
            self.assertEqual(post.call_count,2)
            private=serialization.load_pem_private_key((gateway.root/'push-private.pem').read_bytes(),password=None)
            for call in post.call_args_list:
                token=call.kwargs['headers']['Authorization'].split('t=',1)[1].split(',',1)[0]
                claims=jwt.decode(token,private.public_key(),algorithms=['ES256'],audience='https://web.push.apple.com')
                self.assertEqual(claims['sub'],'https://github.com')
                self.assertNotIn(b'Notifications are enabled',call.kwargs['data'])


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_loss_replays_after_reconnect_without_duplicate_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);node=ActivityStore(root/'activity.sqlite3');gateway=ActivityStore(root/'gateway.sqlite3')
            event=new_event('codex','thread','turn_finished',session='work');node.enqueue(event)
            class Socket:
                async def send(self,payload):
                    for item in json.loads(payload)['events']:gateway.receive('node',item)
            async def once():
                async def stop(_):raise asyncio.CancelledError
                with patch('asyncio.sleep',side_effect=stop):
                    with self.assertRaises(asyncio.CancelledError):await forward_activity(Socket(),root)
            await once();await once()
            self.assertEqual(len(gateway.list()),1)
            self.assertEqual(len(node.pending()),1)
            node.acknowledge([event['id']]);self.assertEqual(node.pending(),[])
