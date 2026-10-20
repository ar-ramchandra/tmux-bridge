"""Portable setup and foreground process commands for tmux-bridge."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

DEFAULT_CONFIG = Path.home() / '.config' / 'tmux-bridge'
DEFAULT_AGENT = Path.home() / '.tmux-bridge'


def default_config_dir():
    if os.environ.get('TMUX_BRIDGE_CONFIG_DIR'):
        return os.environ['TMUX_BRIDGE_CONFIG_DIR']
    return str(DEFAULT_CONFIG)


def gateway_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ('', '/')):
        raise ValueError('Use a gateway origin such as https://my-machine.example.ts.net (no path or credentials).')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Remote pairing requires HTTPS. HTTP is allowed only on localhost.')
    return value.rstrip('/')


def private_write(path, content):
    # Never replace existing identity files, even if setup is run twice.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)


def write_agent(directory, credentials, gateway):
    from auth import validate_server_name
    validate_server_name(credentials['name'])
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    # Reject malformed responses before writing anything.
    key = serialization.load_pem_private_key(credentials['key'].encode(), password=None)
    cert = x509.load_pem_x509_certificate(credentials['cert'].encode())
    x509.load_pem_x509_certificate(credentials['ca'].encode())
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if key.public_key().public_bytes(*fmt) != cert.public_key().public_bytes(*fmt):
        raise ValueError('Pairing certificate does not match its private key.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = {'server.key': credentials['key'], 'server.crt': credentials['cert'],
             'ca.crt': credentials['ca'], 'config.json': json.dumps({'name': credentials['name'], 'gateway': gateway}, indent=2)}
    if any((directory / name).exists() for name in files):
        raise ValueError('This agent is already configured. Use --agent-dir for a separate installation.')
    written = []
    try:
        for name, content in files.items():
            path = directory / name
            private_write(path, content)
            written.append(path)
    except Exception:
        for path in written:
            path.unlink()
        raise


def doctor(args):
    failed = False
    def check(ok, label):
        nonlocal failed
        print(('OK   ' if ok else 'FAIL ') + label)
        failed |= not ok
    check(sys.version_info >= (3, 10), 'Python 3.10+ (3.12+ recommended for new installations)')
    check(sys.platform in ('darwin', 'linux'), 'macOS or Linux')
    check(bool(shutil.which('tmux')), 'tmux on PATH — install with brew install tmux / apt install tmux')
    for module in ('fastapi', 'uvicorn', 'websockets', 'cryptography', 'httpx', 'psutil'):
        check(importlib.util.find_spec(module) is not None, module + ' installed')
    config = Path(args.config_dir).expanduser()
    agent = Path(args.agent_dir).expanduser()
    print('Gateway: ' + ('initialized' if (config / 'ca.key').exists() else 'not initialized; run tmux-bridge init'))
    print('Agent:   ' + ('paired' if (agent / 'config.json').exists() else 'not paired; run tmux-bridge pair'))
    print('Mesh:    ' + ('Tailscale CLI detected (connection not checked)' if shutil.which('tailscale') else 'optional; Tailscale CLI not detected'))
    if args.gateway:
        import httpx
        url = gateway_url(args.gateway)
        try:
            response = httpx.get(url + '/health', timeout=10, follow_redirects=False)
            check(response.status_code == 200 and response.json().get('status') == 'ok', 'Gateway reachable and healthy')
        except (httpx.HTTPError, ValueError):
            check(False, 'Gateway unreachable or returned a login page; check HTTPS, network access and proxy rules')
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Set up a private terminal gateway and pair your machines.')
    parser.add_argument('--config-dir', default=default_config_dir())
    parser.add_argument('--agent-dir', default=os.environ.get('TMUX_BRIDGE_AGENT_DIR', str(DEFAULT_AGENT)))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create gateway identity without replacing existing keys')
    serve = commands.add_parser('gateway', help='Run a loopback gateway; publish privately using your reverse proxy')
    serve.add_argument('--port', type=int, default=4601)
    add = commands.add_parser('add-server', help='Create a one-time pairing code (expires in 10 minutes)')
    add.add_argument('name')
    node = commands.add_parser('add-node', help='Create a private installer bundle for a new Cloudflare/mTLS node')
    node.add_argument('name')
    node.add_argument('--transport-dir', help='Transport administration directory (defaults to --config-dir)')
    node.add_argument('--output', help='Destination ZIP path (default: private gateway node directory)')
    node.add_argument('--port', type=int, default=14603, help='Node-local carrier port')
    revoke = commands.add_parser('revoke-node-bundle', help='Revoke an unused/offline private node installer')
    revoke.add_argument('name')
    revoke.add_argument('--transport-dir', help='Transport administration directory (defaults to --config-dir)')
    pair = commands.add_parser('pair', help='Pair this machine without executing downloaded code')
    pair.add_argument('--gateway', required=True)
    commands.add_parser('agent', help='Run this machine’s paired agent')
    diagnose = commands.add_parser('doctor', help='Check dependencies, setup, and optional gateway reachability')
    diagnose.add_argument('--gateway')
    args = parser.parse_args(argv)
    os.environ['TMUX_BRIDGE_CONFIG_DIR'] = str(Path(args.config_dir).expanduser().resolve())
    os.environ['TMUX_BRIDGE_AGENT_DIR'] = str(Path(args.agent_dir).expanduser().resolve())
    try:
        if args.command == 'doctor':
            return doctor(args)
        if args.command == 'add-node':
            from node_setup import create_bundle
            target = create_bundle(args.name, root=args.transport_dir or args.config_dir, output=args.output, port=args.port)
            print('Private installer created: '+str(target))
            print('Copy it securely to the new node, unzip it, and run: sh install.sh')
            print('The bundle contains only this node\'s credentials. Keep it private.')
            return 0
        if args.command == 'revoke-node-bundle':
            from node_setup import revoke_bundle
            revoke_bundle(args.name, root=args.transport_dir or args.config_dir)
            print('Node installer revoked and its registry entry removed: '+args.name)
            return 0
        if args.command == 'init':
            from auth import ca_exists, init_ca, CA_KEY_FILE, CA_CERT_FILE
            if ca_exists():
                print('Gateway already initialized; existing identity preserved.')
            elif CA_KEY_FILE.exists() or CA_CERT_FILE.exists():
                raise ValueError('Incomplete gateway identity. Restore the missing file before continuing.')
            else:
                init_ca()
                print('Gateway initialized. Run tmux-bridge gateway, then tmux-bridge add-server NAME.')
        elif args.command == 'gateway':
            from auth import ca_exists
            if not ca_exists():
                raise ValueError('Run tmux-bridge init first.')
            if not 1 <= args.port <= 65535:
                raise ValueError('Port must be between 1 and 65535.')
            # New installations never launch an implicit public tunnel.
            os.environ['TMUX_BRIDGE_AUTO_TUNNEL'] = '0'
            import uvicorn
            print(f'Local gateway: http://127.0.0.1:{args.port}\nRemote access: configure a private HTTPS reverse proxy; see docs/setup.md.', flush=True)
            uvicorn.run('gateway:app', host='127.0.0.1', port=args.port)
        elif args.command == 'add-server':
            from auth import ca_exists, create_enrollment_token, SERVERS_DIR, validate_server_name
            validate_server_name(args.name)
            if not ca_exists():
                raise ValueError('Run tmux-bridge init first.')
            if (SERVERS_DIR / args.name).exists():
                raise ValueError('That server name is already enrolled. Choose a unique name.')
            token = create_enrollment_token(args.name, ttl=600)
            print('On the other machine, install this app, then run:\n  tmux-bridge pair --gateway https://YOUR-GATEWAY\n\nPaste this one-time code at its prompt (expires in 10 minutes):\n' + token)
        elif args.command == 'pair':
            url = gateway_url(args.gateway)
            directory = Path(args.agent_dir).expanduser()
            if any((directory / f).exists() for f in ('config.json', 'server.key', 'server.crt', 'ca.crt')):
                raise ValueError('Agent identity already exists. Use --agent-dir for a separate installation.')
            token = getpass.getpass('One-time pairing code: ').strip()
            import httpx
            try:
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    response = client.post(url + '/api/enroll', json={'token': token})
            except httpx.HTTPError:
                raise ValueError('Cannot reach the gateway. Check the URL, HTTPS certificate and network connection.')
            if response.status_code != 200:
                raise ValueError(f'Pairing failed (HTTP {response.status_code}). Check the code, gateway version and proxy access.')
            try:
                credentials = response.json()
            except ValueError:
                raise ValueError('Gateway returned a login page instead of pairing data. Check the proxy’s machine endpoint rules.')
            write_agent(directory, credentials, url)
            print('Paired as ' + credentials['name'] + '. Run tmux-bridge agent to connect.')
        elif args.command == 'agent':
            if not shutil.which('tmux'):
                raise ValueError('Install tmux first: brew install tmux (macOS), or apt install tmux (Debian/Ubuntu).')
            import server
            if not server.CONFIG_FILE.exists():
                raise ValueError('Run tmux-bridge pair --gateway URL first.')
            asyncio.run(server.main())
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 1
    except ImportError as exc:
        print('Missing dependency: ' + str(exc) + '. Run ./setup.sh.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
