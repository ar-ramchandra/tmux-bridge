import asyncio
import contextlib
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from websockets.legacy.client import connect

from preview_registry import PreviewRegistry
from preview_agent import PreviewAgent, clean_headers, MAX_BODY
from preview_gateway import PreviewGateway, PreviewOriginMiddleware


@contextlib.asynccontextmanager
async def running(app):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='off', ws_max_size=4*1024*1024, ws_max_queue=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(.01)
    try:
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


class RegistryTests(unittest.TestCase):
    def test_expiry_stop_session_recreation_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'previews.json').write_text('{"enabled":true,"host_suffix":"-preview.test"}')
            created = [123]
            reg = PreviewRegistry(root, lambda s: created[0])
            row = reg.create('work', 3000)
            self.assertEqual(reg.create('work', 3000)['id'], row['id'])
            self.assertEqual((root/'previews.sqlite3').stat().st_mode & 0o777, 0o600)
            reg.stop(row['id'])
            with self.assertRaises(ValueError): reg.get(row['id'])
            newer = reg.create('work', 3000)
            self.assertNotEqual(row['id'], newer['id'])
            created[0] += 1
            self.assertEqual(reg.listing(), [])
            reg.create('work', 3000, ttl=60)
            with patch('preview_registry.time.time', return_value=time.time()+61):
                self.assertEqual(reg.listing(), [])
            for port in (True, 22, 5900, 4601, '3000', 65536):
                with self.assertRaises(ValueError): reg.create('work', port)

    def test_sensitive_headers_and_cookie_scope(self):
        result = dict(clean_headers([('Cookie','CF_Authorization=secret; app=ok; __cf_bm=secret'),
            ('Cf-Access-Jwt-Assertion','secret'),('Cf-Access-Authenticated-User-Email','private'),
            ('Connection','X-Secret'),('X-Secret','secret'),('Host','control'),
            ('Set-Cookie','app=ok; Domain=.example.com; Path=/'),('Set-Cookie','CF_Authorization=evil')], True))
        self.assertEqual(result, {'Cookie':'app=ok','Set-Cookie':'app=ok; Path=/'})


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = contextlib.AsyncExitStack()
        await self.stack.__aenter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'previews.json').write_text(json.dumps({'enabled':True,'host_suffix':'-preview.test','gateway_hosts':['gateway.test']}))
        self.reg = PreviewRegistry(self.root, lambda _:123)
        self.upstream = FastAPI()
        @self.upstream.get('/html')
        async def html():
            return Response('<h1>Private app</h1><script src="/asset.js"></script>',media_type='text/html')
        @self.upstream.get('/asset.js')
        async def asset(): return Response('window.loaded=true',media_type='application/javascript')
        @self.upstream.api_route('/echo', methods=['GET','POST'])
        async def echo(req: Request):
            return {'body': (await req.body()).decode(), 'headers':dict(req.headers)}
        @self.upstream.get('/redirect')
        async def redirect():
            return Response(status_code=302,headers={'Location':f'http://127.0.0.1:{self.app_port}/html'})
        @self.upstream.get('/big')
        async def big(): return Response(b'x'*(3*1024*1024))
        @self.upstream.get('/events')
        async def events():
            async def stream():
                while True:
                    yield b'data: update\n\n'
                    await asyncio.sleep(.05)
            return StreamingResponse(stream(),media_type='text/event-stream')
        @self.upstream.websocket('/socket')
        async def socket_echo(ws: WebSocket):
            await ws.accept(subprotocol='test' if 'test' in ws.scope['subprotocols'] else None)
            try:
                while True:
                    msg=await ws.receive()
                    if msg.get('text') is not None: await ws.send_text(msg['text'])
                    elif msg.get('bytes') is not None: await ws.send_bytes(msg['bytes'])
                    else: return
            except Exception: pass
        self.app_port=await self.stack.enter_async_context(running(self.upstream))
        self.row=self.reg.create('work', self.app_port)
        self.hostname=self.row['id']+'-preview.test'
        self.agent=PreviewAgent(self.root,{},lambda _: {})
        self.conn=type('Connection',(),{'is_active':True,'sessions':[{'name':'work','created':123}]})()
        self.gw=PreviewGateway(lambda name:self.conn if name=='node' else None,self.root)
        self.gw.update('node',[self.row])
        self.tasks=set()
        async def rpc(method,path,body=None):
            if path=='/api/previews/connect':
                async def node_stream():
                    async with connect(f'ws://127.0.0.1:{self.gw_port}/test-agent', max_size=MAX_BODY*2,max_queue=2) as ws:
                        await ws.send(json.dumps({'ticket':body['ticket']}))
                        request=json.loads(await ws.recv())
                        await self.agent.serve(ws,self.reg.get(body['id']),request)
                task=asyncio.create_task(node_stream());self.tasks.add(task)
                return {'status':200,'body':{}}
            if path=='/api/previews/stop':
                self.reg.stop(body['id']);self.gw.update('node',self.reg.listing())
                return {'status':200,'body':{}}
            raise ValueError(path)
        self.conn.rpc=rpc
        app=FastAPI()
        app.include_router(self.gw.router)
        @app.get('/api/control-secret')
        async def secret(): return {'secret':'must not leak'}
        @app.websocket('/test-agent')
        async def agent_socket(ws:WebSocket):
            await ws.accept();data=await ws.receive_json()
            await self.gw.accept_agent(ws,'node',data['ticket'])
        app.add_middleware(PreviewOriginMiddleware,previews=self.gw)
        self.gw_port=await self.stack.enter_async_context(running(app))
        self.client=await self.stack.enter_async_context(httpx.AsyncClient(base_url=f'http://127.0.0.1:{self.gw_port}',headers={'host':self.hostname},trust_env=False))
        self.worker=asyncio.create_task(self.gw.worker())

    async def asyncTearDown(self):
        self.worker.cancel()
        for task in self.tasks: task.cancel()
        await asyncio.gather(self.worker,*self.tasks,return_exceptions=True)
        for link in list(self.gw.links.values()):
            if link.task: link.task.cancel()
        await self.stack.aclose()
        self.tmp.cleanup()

    async def test_http_assets_redirect_stream_and_auth_stripping(self):
        r=await self.client.get('/html');self.assertEqual(r.status_code,200);self.assertIn('Private app',r.text)
        self.assertEqual((await self.client.get('/asset.js')).text,'window.loaded=true')
        r=await self.client.post('/echo',content='payload',headers={'origin':'https://'+self.hostname,'Cf-Access-Jwt-Assertion':'secret','Cookie':'CF_Authorization=secret; app=yes'})
        body=r.json();self.assertEqual(body['body'],'payload')
        self.assertEqual(body['headers']['cookie'],'app=yes');self.assertNotIn('cf-access-jwt-assertion',body['headers'])
        self.assertEqual(body['headers']['host'],'127.0.0.1:'+str(self.app_port))
        self.assertEqual((await self.client.get('/redirect')).headers['location'],'https://'+self.hostname+'/html')
        self.assertEqual(len((await self.client.get('/big')).content),3*1024*1024)
        async with self.client.stream('GET','/events') as r:
            async for data in r.aiter_bytes():
                self.assertIn(b'data: update',data);break

    async def test_origin_separation_unknown_hosts_and_stopped_links(self):
        r=await self.client.get('/api/control-secret');self.assertEqual(r.status_code,404);self.assertNotIn('must not leak',r.text)
        self.assertEqual((await self.client.get('/html',headers={'origin':'https://another-preview.test'})).status_code,403)
        self.assertEqual((await self.client.get('/html',headers={'host':'unknown.test'})).status_code,410)
        self.gw.update('node',[])
        self.assertEqual((await self.client.get('/html')).status_code,410)
        self.assertEqual((await self.client.get('/api/control-secret',headers={'host':'gateway.test'})).json()['secret'],'must not leak')

    async def test_websocket_subprotocol_and_revocation(self):
        async with connect(f'ws://{self.hostname}:{self.gw_port}/socket',host='127.0.0.1',port=self.gw_port,origin='https://'+self.hostname,subprotocols=['test']) as ws:
            self.assertEqual(ws.subprotocol,'test')
            await ws.send('live reload');self.assertEqual(await ws.recv(),'live reload')
            await ws.send(b'bytes');self.assertEqual(await ws.recv(),b'bytes')
            self.gw.update('node',[])
            with self.assertRaises(Exception): await asyncio.wait_for(ws.recv(),2)
        self.assertEqual(len(self.gw.links),0)

    async def test_wrong_node_ticket_cannot_attach(self):
        from preview_gateway import Link
        class Fake:
            closed=False
            async def close(self,**kwargs): self.closed=True
        link=Link('node',self.row['id'],time.monotonic()+10,asyncio.get_running_loop().create_future())
        self.gw.links['ticket']=link
        ws=Fake();await self.gw.accept_agent(ws,'other-node','ticket')
        self.assertTrue(ws.closed);self.assertFalse(link.ready.done())
        self.gw.links.clear()

    async def test_expiry_disconnect_and_body_limit(self):
        r=await self.client.post('/echo',content=b'x'*(MAX_BODY+1))
        self.assertNotEqual(r.status_code,200)
        self.gw.items[self.row['id']]['expires']=time.time()-1
        self.assertEqual((await self.client.get('/html')).status_code,410)
        await self.gw.close_node('node')
        self.assertEqual(self.gw.items,{})


if __name__=='__main__': unittest.main()
