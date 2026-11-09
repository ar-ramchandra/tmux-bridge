"""Opt-in loopback VNC carried on a separate, authenticated agent connection."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from websockets.legacy.client import connect
from auth import create_auth_proof
from protocol import make_rpc_response


def managed_credentials(root):
    """An explicit node-local Keychain opt-in; never expose credentials by RPC."""
    config = json.loads((Path(root) / 'desktop.json').read_text())
    managed = config.get('managed_auth')
    if managed is None:
        return None
    if not isinstance(managed, dict) or managed.get('source') != 'keychain' or sys.platform != 'darwin':
        raise ValueError('Unsupported managed desktop credential source')
    service, account = managed.get('service'), managed.get('account')
    if not all(isinstance(value, str) and 0 < len(value) <= 128 for value in (service, account)):
        raise ValueError('Invalid managed desktop credential reference')
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', service,
                             '-a', account, '-w'], capture_output=True, timeout=5)
    if result.returncode:
        raise ValueError('Managed desktop credential is unavailable')
    return account, result.stdout.decode().removesuffix('\n')


async def authenticate_ard(reader, writer, credentials):
    """Authenticate only the node's loopback desktop using Apple's ARD type 30."""
    username, password = (value.encode('utf-8') for value in credentials)
    if not all(0 < len(value) <= 63 and b'\0' not in value for value in (username, password)):
        raise ValueError('Invalid managed desktop credentials')
    writer.write(b'RFB 003.008\n')
    await writer.drain()
    count = (await reader.readexactly(1))[0]
    if not count or 30 not in await reader.readexactly(count):
        raise ValueError('Desktop does not offer Apple authentication')
    writer.write(bytes([30]))
    await writer.drain()
    generator = int.from_bytes(await reader.readexactly(2), 'big')
    size = int.from_bytes(await reader.readexactly(2), 'big')
    if not 128 <= size <= 512:
        raise ValueError('Invalid desktop key size')
    prime = int.from_bytes(await reader.readexactly(size), 'big')
    peer = int.from_bytes(await reader.readexactly(size), 'big')
    if not 1 < generator < prime - 1 or not 1 < peer < prime - 1:
        raise ValueError('Invalid desktop key parameters')
    private = secrets.randbits(size * 8) or 1
    public = pow(generator, private, prime).to_bytes(size, 'big')
    shared = pow(peer, private, prime).to_bytes(size, 'big')
    # MD5 and AES-ECB are mandated by Apple's ARD wire protocol.
    key = hashlib.md5(shared).digest()
    plain = username.ljust(64, b'\0') + password.ljust(64, b'\0')
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    writer.write(cipher.update(plain) + cipher.finalize() + public)
    await writer.drain()
    if int.from_bytes(await reader.readexactly(4), 'big') != 0:
        raise ValueError('Desktop rejected its managed credential')


async def managed_browser_handshake(ws):
    """No second password inside the already-authenticated gateway desktop link.

    Called only after upstream authentication and signed node/ticket admission.
    No unauthenticated TCP listener is created.
    """
    pending = bytearray()
    async def read(size):
        while len(pending) < size:
            frame = await ws.recv()
            if not isinstance(frame, bytes) or len(frame) > 64:
                raise ValueError('Invalid desktop handshake')
            pending.extend(frame)
        value = bytes(pending[:size])
        del pending[:size]
        return value
    await ws.send(b'RFB 003.008\n')
    if await read(12) != b'RFB 003.008\n':
        raise ValueError('Unsupported browser desktop version')
    await ws.send(bytes([1, 1]))
    if await read(1) != b'\x01':
        raise ValueError('Unsupported browser desktop authentication')
    await ws.send(bytes(4))
    return bytes(pending)


def desktop_port(root):
    try:
        data = json.loads((Path(root) / 'desktop.json').read_text())
        port = data.get('port', 5900)
        if data.get('enabled') is True and type(port) is int and 1024 <= port <= 65535:
            return port
    except (OSError, ValueError, AttributeError):
        pass
    return None


async def desktop_status(root):
    port = desktop_port(root)
    if port is None:
        return {'enabled': False, 'available': False, 'message': 'Desktop access is not configured on this device.'}
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 2)
        header = await asyncio.wait_for(reader.readexactly(12), 2)
        if not re.fullmatch(rb'RFB \d{3}\.\d{3}\n', header):
            raise ValueError('Not a VNC server')
        return {'enabled': True, 'available': True, 'protocol': 'vnc'}
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return {'enabled': True, 'available': False, 'message': 'The desktop service is unavailable on this device.'}
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()


class DesktopAgent:
    def __init__(self, root, config, tls_options):
        self.root, self.config, self.tls_options = Path(root), config, tls_options
        self.tasks = set()
        self.stream = None

    async def request(self, ws, msg):
        # Bound control work; probes must not hold up terminal input.
        if len(self.tasks) >= 4:
            await ws.send(make_rpc_response(msg['id'], 429, {'error': 'Desktop is busy; retry shortly'}))
            return
        task = asyncio.create_task(self._request(ws, msg))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _request(self, ws, msg):
        try:
            path, method = msg.get('path'), msg.get('method', 'GET')
            status, body = 404, {'error': 'Unknown desktop operation'}
            if path == '/api/desktop' and method == 'GET':
                status, body = 200, await desktop_status(self.root)
            elif path == '/api/desktop/connect' and method == 'POST':
                ticket = (msg.get('body') or {}).get('ticket')
                if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket):
                    status, body = 400, {'error': 'Invalid desktop ticket'}
                elif desktop_port(self.root) is None:
                    status, body = 409, {'error': 'Desktop access is not configured'}
                elif self.stream and not self.stream.done():
                    status, body = 409, {'error': 'Desktop is already open. Close the other viewer first.'}
                else:
                    self.stream = asyncio.create_task(self._relay(ticket))
                    status, body = 202, {'ok': True}
            await ws.send(make_rpc_response(msg['id'], status, body))
        except Exception:
            pass  # Disconnects are retried explicitly by the browser.

    async def _relay(self, ticket):
        writer = None
        pumps = []
        try:
            port = desktop_port(self.root)
            if port is None:
                return
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
            header = await asyncio.wait_for(reader.readexactly(12), 5)
            if not re.fullmatch(rb'RFB \d{3}\.\d{3}\n', header):
                return
            credentials = await asyncio.to_thread(managed_credentials, self.root)
            if credentials is not None:
                await asyncio.wait_for(authenticate_ard(reader, writer, credentials), 10)
                credentials = None
                managed = True
            else:
                managed = False
            url = self.config['gateway'].replace('https:', 'wss:').replace('http:', 'ws:') + '/server-ws'
            async with connect(url, max_size=2**20, max_queue=4, compression=None,
                               open_timeout=12, ping_interval=20, ping_timeout=30,
                               **self.tls_options(self.config)) as ws:
                proof = create_auth_proof((self.root/'server.key').read_bytes(), (self.root/'server.crt').read_bytes())
                await ws.send(json.dumps(proof))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if reply != {'type': 'auth_ok', 'name': self.config['name']}:
                    return
                await ws.send(json.dumps({'type': 'desktop', 'ticket': ticket}))
                if managed:
                    pending = await asyncio.wait_for(managed_browser_handshake(ws), 10)
                    if pending:
                        writer.write(pending)
                        await writer.drain()
                else:
                    await ws.send(header)

                async def to_gateway():
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            return
                        await ws.send(data)

                async def to_desktop():
                    async for data in ws:
                        if not isinstance(data, bytes):
                            raise ValueError('Expected desktop bytes')
                        writer.write(data)
                        await writer.drain()

                pumps = [asyncio.create_task(to_gateway()), asyncio.create_task(to_desktop())]
                try:
                    await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in pumps:
                        task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
        except Exception:
            pass  # Never log frames, tickets, or desktop credentials.
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()

    async def stop(self):
        tasks = list(self.tasks)
        if self.stream:
            tasks.append(self.stream)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
