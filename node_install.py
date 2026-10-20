"""Private bundle installer. Uses only the standard library before venv setup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import urllib.request

NAME_RE=re.compile(r'[a-z][a-z0-9-]{0,47}\Z')
PAYLOAD=('server.py','protocol.py','auth.py','sysinfo.py','node_runtime.py',
    'server.key','server.crt','ca.crt','tls.key','tls.crt','tls-ca.crt','access-token.json',
    'config.json','requirements-agent.txt','manifest.json')


def private_write(path,data):
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:
        f.write(data if isinstance(data,bytes) else data.encode())


def check_bundle(bundle):
    manifest=json.loads((bundle/'manifest.json').read_text())
    if not NAME_RE.fullmatch(manifest['name']):
        raise ValueError('Invalid node name')
    if datetime.fromisoformat(manifest['expires_at'])<=datetime.now(timezone.utc):
        raise ValueError('This bundle has expired. Create a new one on the gateway.')
    if not 1024<=manifest['port']<=65535:
        raise ValueError('Invalid loopback port')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]+',manifest['hostname']):
        raise ValueError('Invalid gateway hostname')
    for name in PAYLOAD:
        p=bundle/name
        if p.is_symlink() or not p.is_file():
            raise ValueError('Bundle file missing or unsafe: '+name)
        if name!='manifest.json' and hashlib.sha256(p.read_bytes()).hexdigest()!=manifest['files'][name]:
            raise ValueError('Bundle file checksum failed: '+name)
    config=json.loads((bundle/'config.json').read_text())
    expected={'name':manifest['name'],'gateway':'https://127.0.0.1:'+str(manifest['port']),
        'tls':{'ca':'tls-ca.crt','certificate':'tls.crt','private_key':'tls.key','server_hostname':manifest['hostname']}}
    if config!=expected:
        raise ValueError('Bundle must use verified, loopback-carried mTLS')
    return manifest


def platform_asset(system=None,machine=None):
    system=system or sys.platform
    machine=(machine or platform.machine()).lower()
    architecture={'x86_64':'amd64','amd64':'amd64','arm64':'arm64','aarch64':'arm64'}.get(machine)
    if system not in ('linux','darwin') or not architecture:
        raise ValueError('Supported nodes: Linux or macOS on x86-64 or ARM64')
    return system+'-'+architecture


def check_target(home,name,port,foreground=False):
    root=home/'.tmux-bridge-nodes'/name
    if root.exists():
        raise ValueError('This node is already installed: '+str(root))
    for old in (home/'.tmux-bridge/config.json',home/'.tmux-bridge-cf-production/config.json'):
        if old.exists():
            raise ValueError('This account already runs a bridge agent. Existing agents will not be replaced.')
    parent=root.parent
    if parent.exists() and any((p/'config.json').exists() for p in parent.iterdir() if p.is_dir()):
        raise ValueError('Another node identity is already installed for this account.')
    if not foreground:
        target=service_path(home,name)
        if target.exists():
            raise ValueError('A service with this node name already exists.')
        if sys.platform=='linux' and not Path('/run/systemd/system').is_dir():
            raise ValueError('systemd is unavailable. Use --foreground instead.')
        if sys.platform=='darwin':
            subprocess.run(['launchctl','print','gui/'+str(os.getuid())],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',port))
    return root


def ensure_tmux():
    found=shutil.which('tmux')
    if found:
        return str(Path(found).resolve())
    if sys.platform=='darwin' and shutil.which('brew'):
        command=['brew','install','tmux']
    elif sys.platform=='linux' and shutil.which('apt-get'):
        command=([] if os.getuid()==0 else ['sudo'])+['apt-get','install','-y','tmux','python3-venv']
    elif sys.platform=='linux' and shutil.which('dnf'):
        command=([] if os.getuid()==0 else ['sudo'])+['dnf','install','-y','tmux']
    else:
        raise ValueError('Install tmux with your package manager, then rerun this installer.')
    subprocess.run(command,check=True)
    found=shutil.which('tmux')
    if not found:
        raise ValueError('tmux is still missing from PATH')
    return str(Path(found).resolve())


def download_cloudflared(root,asset):
    # The bundle pins both the release URL and its published SHA-256 digest.
    if not asset['url'].startswith('https://github.com/cloudflare/cloudflared/releases/download/'):
        raise ValueError('Untrusted cloudflared download URL')
    archive=root/'cloudflared.download'
    digest=hashlib.sha256()
    with urllib.request.urlopen(asset['url'],timeout=60) as response,archive.open('xb') as f:
        archive.chmod(0o600)
        total=0
        while True:
            chunk=response.read(256*1024)
            if not chunk:
                break
            total+=len(chunk)
            if total>200*1024*1024:
                raise ValueError('Unexpected cloudflared download size')
            digest.update(chunk);f.write(chunk)
    if digest.hexdigest()!=asset['sha256']:
        raise ValueError('cloudflared checksum mismatch; nothing will be executed')
    binary=root/'bin/cloudflared'
    binary.parent.mkdir(mode=0o700)
    if asset['url'].endswith('.tgz'):
        with tarfile.open(archive,'r:gz') as tar:
            members=[m for m in tar.getmembers() if Path(m.name).name=='cloudflared' and m.isfile()]
            if len(members)!=1:
                raise ValueError('Unexpected cloudflared archive')
            with tar.extractfile(members[0]) as src,binary.open('xb') as dest:
                shutil.copyfileobj(src,dest)
    else:
        shutil.copyfile(archive,binary)
    binary.chmod(0o700)
    archive.unlink()


def service_path(home,name,system=None):
    if (system or sys.platform)=='darwin':
        return home/'Library/LaunchAgents'/('com.user.tmux-bridge-node-'+name+'.plist')
    return Path('/etc/systemd/system')/('tmux-bridge-node-'+name+'.service')


def run_probe(python,root,environment):
    command=[python,str(root/'node_runtime.py'),'--probe']
    child=subprocess.Popen(command,env=environment)
    try:
        result=child.wait(timeout=90)
        if result:
            raise subprocess.CalledProcessError(result,command)
    except BaseException:
        # Let the supervisor stop its carrier even if installation is interrupted.
        if child.poll() is None:
            child.terminate()
            try:child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill();child.wait()
        raise


def service_bytes(root,name,system=None,uid=None):
    system=system or sys.platform
    uid=os.getuid() if uid is None else uid
    command=[str(root/'.venv/bin/python'),str(root/'node_runtime.py')]
    if system=='darwin':
        return plistlib.dumps({'Label':'com.user.tmux-bridge-node-'+name,
            'ProgramArguments':command,'WorkingDirectory':str(root),'RunAtLoad':True,
            'KeepAlive':True,'AbandonProcessGroup':True,'ThrottleInterval':5,
            'EnvironmentVariables':{'PYTHONUNBUFFERED':'1','PYTHONPATH':''},
            'StandardOutPath':str(root/'node.log'),'StandardErrorPath':str(root/'node.log')})
    def quote(value,command=False):
        text=str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')
        if command:text=text.replace('$','$$')
        return '"'+text+'"'
    text='[Unit]\nDescription=Terminal node '+name+'\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=simple\nUser='+str(uid)+'\n'
    # Runtime resolves every file from __file__ and sets child working directories.
    # Older systemd versions treat WorkingDirectory quotes as literal path bytes.
    text+='ExecStart='+' '.join(quote(a,True) for a in command)+'\n'
    text+='Environment=PYTHONUNBUFFERED=1\nEnvironment=PYTHONPATH=\nRestart=always\nRestartSec=5\nKillMode=process\nTimeoutStopSec=20\n[Install]\nWantedBy=multi-user.target\n'
    return text.encode()


def install_service(root,home,name):
    target=service_path(home,name)
    source=root/target.name
    private_write(source,service_bytes(root,name))
    if target.exists():
        raise ValueError('Service appeared during installation; refusing to replace it')
    if sys.platform=='darwin':
        target.parent.mkdir(parents=True,exist_ok=True)
        private_write(root/'node.log',b'')
        private_write(target,source.read_bytes())
        try:
            subprocess.run(['launchctl','bootstrap','gui/'+str(os.getuid()),str(target)],check=True)
        except BaseException:
            subprocess.run(['launchctl','bootout','gui/'+str(os.getuid()),str(target)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            target.unlink();raise
    else:
        sudo=[] if os.getuid()==0 else ['sudo']
        subprocess.run(sudo+['systemd-analyze','verify','--man=no',str(source)],check=True)
        # An exclusive hard link avoids replacing an existing unit on a race.
        staged=target.with_name('.'+target.name+'.installing')
        if staged.exists():
            raise ValueError('An interrupted service installation needs review: '+str(staged))
        linked=False
        try:
            subprocess.run(sudo+['install','-m','0644',str(source),str(staged)],check=True)
            subprocess.run(sudo+['ln',str(staged),str(target)],check=True);linked=True
            subprocess.run(sudo+['systemctl','daemon-reload'],check=True)
            subprocess.run(sudo+['systemctl','enable','--now',target.name],check=True)
        except BaseException:
            if linked:
                subprocess.run(sudo+['systemctl','disable','--now',target.name],check=True)
                subprocess.run(sudo+['rm','--',str(target)],check=True)
                subprocess.run(sudo+['systemctl','daemon-reload'],check=False)
            raise
        finally:
            subprocess.run(sudo+['rm','-f','--',str(staged)],check=False)
    return target


def main(argv=None):
    parser=argparse.ArgumentParser(description='Install this private terminal-node bundle.')
    parser.add_argument('--check',action='store_true',help='Check the bundle and target without installing')
    parser.add_argument('--foreground',action='store_true',help='Run in this terminal instead of installing a service')
    args=parser.parse_args(argv)
    root=None
    try:
        if sys.version_info<(3,10) or not ssl.HAS_TLSv1_3:
            raise ValueError('Python 3.10+ with TLS 1.3 is required; install Python 3.12+ if needed.')
        bundle=Path(__file__).resolve().parent
        manifest=check_bundle(bundle);asset=platform_asset()
        home=Path.home();root=check_target(home,manifest['name'],manifest['port'],args.foreground)
        if args.check:
            print('Bundle and target checks passed. Node: '+manifest['name'])
            print('tmux: '+('installed' if shutil.which('tmux') else 'will be installed with your package manager'))
            print('Certificates expire: '+manifest['expires_at'])
            return 0
        tmux=ensure_tmux()
        root.parent.mkdir(mode=0o700,exist_ok=True)
        root.mkdir(mode=0o700)
        try:
            for name in PAYLOAD:
                private_write(root/name,(bundle/name).read_bytes())
            environment=dict(os.environ);environment.pop('PYTHONPATH',None)
            subprocess.run([sys.executable,'-m','venv',str(root/'.venv')],check=True,env=environment)
            python=str(root/'.venv/bin/python')
            subprocess.run([python,'-m','pip','install','--disable-pip-version-check','-r',str(root/'requirements-agent.txt')],check=True,env=environment)
            download_cloudflared(root,manifest['cloudflared'][asset])
            private_write(root/'installation.json',json.dumps({'name':manifest['name'],'port':manifest['port'],'tmux':tmux,'service':not args.foreground}))
            run_probe(python,root,environment)
            if not args.foreground:
                target=install_service(root,home,manifest['name'])
                print('Installed '+manifest['name']+'. Service: '+str(target))
                print('Startup: '+('at login' if sys.platform=='darwin' else 'at boot'))
        except BaseException:
            if service_path(home,manifest['name']).exists() and not args.foreground:
                print('Service cleanup needs review; installation files retained at '+str(root),file=sys.stderr)
            else:
                shutil.rmtree(root)
            raise
        print('Keep this bundle private. Certificates expire '+manifest['expires_at']+'. Mesh settings were not changed.')
        if args.foreground:
            return subprocess.call([python,str(root/'node_runtime.py')],env=environment)
        return 0
    except (ValueError,OSError,subprocess.SubprocessError,KeyError) as error:
        print('Install failed: '+str(error),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
