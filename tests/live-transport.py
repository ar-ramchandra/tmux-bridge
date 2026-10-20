"""Opt-in production transport test using only a disposable terminal session."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import statistics
import time

import httpx
from websockets.legacy.client import connect

BASE = os.environ.get('BRIDGE_URL', 'http://127.0.0.1:4601')
SERVER = os.environ.get('BRIDGE_SERVER', 'test-node')
NAME = 'bridge-transport-check-'+str(time.time_ns())
PROGRAM = '''import os,tty,hashlib
tty.setraw(0)
os.write(1,b'READY\\r\\n')
pending=b''
while True:
 data=os.read(0,4096)
 if not data:break
 pending+=data
 while b'\\n' in pending:
  line,pending=pending.split(b'\\n',1)
  if line.startswith(b'BULK:'):
   reply=b'HASH:'+hashlib.sha256(line[5:]).hexdigest().encode()
  else:reply=line
  os.write(1,b'\\r\\n'+reply+b'\\r\\n')
'''


async def receive_marker(ws, marker, timeout=10):
    received=b''
    deadline=time.monotonic()+timeout
    while marker not in received:
        item=await asyncio.wait_for(ws.recv(),max(.01,deadline-time.monotonic()))
        if isinstance(item,bytes):
            received+=item


async def measure(ws, count, label):
    samples=[]
    for i in range(count):
        marker=('%s-%03d'%(label,i)).encode()
        started=time.perf_counter()
        await ws.send(marker+b'\n')
        await receive_marker(ws,marker)
        samples.append((time.perf_counter()-started)*1000)
        await asyncio.sleep(.1)
    return {'samples':count,'median_ms':round(statistics.median(samples),1),
        'p95_ms':round(sorted(samples)[int((count-1)*.95)],1),'max_ms':round(max(samples),1)}


async def main():
    report={}
    async with httpx.AsyncClient(base_url=BASE,timeout=15) as client:
        result=await client.post('/api/'+SERVER+'/sessions/'+NAME,
            json={'command':'python3 -u -c '+shlex.quote(PROGRAM)})
        result.raise_for_status()
        assert result.json()['created']
        try:
            url=BASE.replace('http:', 'ws:').replace('https:', 'wss:')+'/ws/'+SERVER+'/'+NAME+'?cols=120&rows=40&device=Transport-test&client_id=production-transport-test'
            async with connect(url,origin=BASE) as old:
                await receive_marker(old,b'READY')
                report['echo']=await measure(old,40,'CLOUDFLARE')
                print('echo',json.dumps(report['echo']),flush=True)
                async with connect(url,origin=BASE) as new:
                    await asyncio.sleep(.5)
                    report['reconnect_echo']=await measure(new,10,'RECONNECTED')
                    await asyncio.wait_for(old.wait_closed(),3)
                    assert old.close_code==1000
                    report['same_browser_reconnect']=True
                    payload=b'abcdefghij'*6500
                    # Terminal output includes rendering escapes: validate large
                    # input at the receiving app via a digest, not its screen bytes.
                    await new.send(b'BULK:'+payload+b'\n')
                    await receive_marker(new,b'HASH:'+hashlib.sha256(payload).hexdigest().encode())
                    report['bulk_input_bytes_verified']=len(payload)
                    report['after_bulk_echo']=await measure(new,5,'AFTER-BULK')
                    print('validation',json.dumps(report),flush=True)
        finally:
            result=await client.delete('/api/'+SERVER+'/sessions/'+NAME)
            result.raise_for_status()
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    asyncio.run(main())
