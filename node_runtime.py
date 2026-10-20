"""Supervise only this node's carrier and agent; leave tmux servers running."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading


async def authenticate(root):
    from websockets.legacy.client import connect
    from server import websocket_tls_options
    from auth import create_auth_proof
    config=json.loads((root/'config.json').read_text())
    for key in ('ca','certificate','private_key'):
        config['tls'][key]=str(root/config['tls'][key])
    async with connect(config['gateway'].replace('https:', 'wss:')+'/server-ws',
                       open_timeout=12, **websocket_tls_options(config)) as ws:
        proof=create_auth_proof((root/'server.key').read_bytes(),(root/'server.crt').read_bytes())
        await ws.send(json.dumps(proof))
        reply=json.loads(await asyncio.wait_for(ws.recv(),10))
        if reply != {'type':'auth_ok','name':config['name']}:
            raise RuntimeError('Gateway rejected this node identity')


def stop_child(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--probe',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    os.environ['TMUX_BRIDGE_AGENT_DIR']=str(root)
    os.environ.pop('PYTHONPATH',None)
    installed=json.loads((root/'installation.json').read_text())
    config=json.loads((root/'config.json').read_text())
    credential=json.loads((root/'access-token.json').read_text())
    port=installed['port']
    stopped=threading.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,lambda *_:stopped.set())
    environment=dict(os.environ, PYTHONUNBUFFERED='1')
    environment['PATH']=str(Path(installed['tmux']).parent)+os.pathsep+environment.get('PATH','/usr/bin:/bin')
    carrier_environment=dict(environment,TUNNEL_SERVICE_TOKEN_ID=credential['client_id'],
        TUNNEL_SERVICE_TOKEN_SECRET=credential['client_secret'])
    while not stopped.is_set():
        carrier=agent=None
        try:
            # Refuse to attach to somebody else's listener.
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',port))
            carrier=subprocess.Popen([str(root/'bin/cloudflared'),'access','tcp',
                '--hostname',config['tls']['server_hostname'],'--url','127.0.0.1:'+str(port)],
                cwd=root,env=carrier_environment)
            for _ in range(100):
                if stopped.is_set() or carrier.poll() is not None:
                    raise RuntimeError('Carrier stopped before becoming ready')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):
                        break
                except OSError:
                    stopped.wait(.1)
            asyncio.run(authenticate(root))
            if args.probe:
                print('Verified private TLS and signed node identity.',flush=True)
                return 0
            if stopped.is_set():
                break
            agent=subprocess.Popen([sys.executable,str(root/'server.py')],cwd=root,env=environment)
            print('Node agent started with authenticated transport.',flush=True)
            while not stopped.wait(.5):
                if carrier.poll() is not None or agent.poll() is not None:
                    break
        except Exception as error:
            # No credential values or subprocess environment in diagnostics.
            print('Node connection failed: '+type(error).__name__,flush=True)
            if args.probe:
                return 1
        finally:
            stop_child(agent)
            stop_child(carrier)
        if args.probe or stopped.wait(5):
            break
    return 0


if __name__=='__main__':
    raise SystemExit(main())
