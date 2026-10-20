"""Agent-only WebSocket relay with end-to-end, privately issued mutual TLS.

Cloudflare carries opaque TCP bytes to this loopback listener. TLS ends here.
Only an allowlisted client certificate and matching bridge identity may reach
/server-ws; the existing gateway still verifies the signed authentication proof.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import ssl
import time
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from websockets.legacy.client import connect
from websockets.legacy.server import serve


def server_context(config):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(config['certificate'], config['private_key'])
    context.load_verify_locations(cafile=config['client_ca'])
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class AgentTLSProxy:
    def __init__(self, config):
        self.config = config

    async def process_request(self, path, headers):
        if path != '/server-ws':
            return HTTPStatus.NOT_FOUND, [('Content-Type', 'text/plain')], b'Not found\n'
        return None

    def identity(self, ws):
        ssl_object = ws.transport.get_extra_info('ssl_object')
        if ssl_object is None:
            raise ValueError('TLS is required')
        certificate = ssl_object.getpeercert(binary_form=True)
        fingerprint = hashlib.sha256(certificate).hexdigest()
        # Read at connection time: removing a certificate takes effect on the
        # next handshake without changing the CA or restarting other clients.
        allowed = json.loads(Path(self.config['allowed_clients']).read_text())
        name = allowed.get(fingerprint)
        if not isinstance(name, str) or not name:
            raise ValueError('Client certificate is not authorized')
        return name

    async def handle(self, ws, path):
        started=time.monotonic()
        try:
            name = self.identity(ws)
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            if not isinstance(first, str) or len(first) > 16384:
                raise ValueError('Expected bridge authentication')
            proof = json.loads(first)
            if proof.get('type') != 'auth':
                raise ValueError('Expected bridge authentication')
            cert = x509.load_pem_x509_certificate(proof['cert'].encode())
            common_name = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            if common_name != name:
                raise ValueError('TLS and bridge identities must match')
        except Exception:
            await ws.close(code=1008, reason='Agent authentication required')
            return

        counts={'agent_frames':0,'gateway_frames':0,'agent_bytes':0,'gateway_bytes':0}
        def report(event,**details):
            print(json.dumps({'time':datetime.now(timezone.utc).isoformat(),'event':event,
                'name':name,'duration_s':round(time.monotonic()-started,3),**details}),flush=True)

        async def copy(source, target, direction):
            async for frame in source:
                counts[direction+'_frames']+=1
                counts[direction+'_bytes']+=len(frame)
                await target.send(frame)

        async def observe(upstream):
            previous=time.monotonic();last_report=previous;lag=0.
            while True:
                await asyncio.sleep(.5)
                now=time.monotonic();lag=max(lag,now-previous-.5);previous=now
                if now-last_report<60:
                    continue
                report('transport_sample',agent_rtt_s=round(ws.latency,4),
                    gateway_rtt_s=round(upstream.latency,4),loop_lag_max_s=round(lag,3),
                    agent_receive_queue=len(ws.messages),gateway_receive_queue=len(upstream.messages),**counts)
                lag=0.;last_report=now

        try:
            async with connect(self.config['upstream'], max_size=2**22,
                               max_queue=16, ping_interval=20, ping_timeout=20) as upstream:
                await upstream.send(first)
                report('transport_open')
                monitor=asyncio.create_task(observe(upstream))
                tasks = [asyncio.create_task(copy(ws, upstream,'agent')),
                         asyncio.create_task(copy(upstream, ws,'gateway'))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks+[monitor]:
                        task.cancel()
                    results=await asyncio.gather(*tasks,monitor, return_exceptions=True)
                    report('transport_close',agent_code=ws.close_code,gateway_code=upstream.close_code,
                        agent_close_sent=ws.close_sent.code if ws.close_sent else None,
                        gateway_close_sent=upstream.close_sent.code if upstream.close_sent else None,
                        errors=[type(result).__name__ for result in results if isinstance(result,Exception)],**counts)
        except Exception as exc:
            print(f'[MTLS] Connection ended: {type(exc).__name__}', flush=True)
            await ws.close(code=1011, reason='Gateway unavailable')

    async def start(self):
        # The only ingress is cloudflared on this host, never a public listener.
        return await serve(self.handle, '127.0.0.1', self.config['port'],
                           ssl=server_context(self.config), ssl_handshake_timeout=5,
                           process_request=self.process_request, max_size=2**22,
                           max_queue=16, ping_interval=20, ping_timeout=20,
                           compression=None)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if not config['upstream'].startswith('ws://127.0.0.1:'):
        raise ValueError('Upstream must be the local gateway')
    listener = await AgentTLSProxy(config).start()
    print(f"[MTLS] Agent-only listener on 127.0.0.1:{config['port']}", flush=True)
    async with listener:
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
