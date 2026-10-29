"""Create per-node private installers on the gateway; never export admin keys."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import urllib.request
import uuid
import zipfile

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes

import auth
from node_install import NAME_RE, PAYLOAD
import transport_pki

SOURCE=Path(__file__).resolve().parent
REQUIREMENTS='websockets==15.0.1\ncryptography==46.0.7\npsutil==7.2.2\n'
VERSION='2026.9.1'
# Published digests from Cloudflare's release API, checked when this installer was added.
ASSET_DIGESTS={
    'linux-amd64':('cloudflared-linux-amd64','03f1f25d1cc93b9ad6c60569d44060bc4f17ed97075760ed8cfca4b12dcd68cc'),
    'linux-arm64':('cloudflared-linux-arm64','3d97437c71848bd8df68041e12436b484a661d95073ea1937f01a845ce88faa3'),
    'darwin-amd64':('cloudflared-darwin-amd64.tgz','ff0d3b51d5ff70eceef89d6b32145fee985018a2174596a5dbe405e2766e2ac4'),
    'darwin-arm64':('cloudflared-darwin-arm64.tgz','c27ab8fd0aa489449e3d201eb02f957ef460a13b613662928b1b23394bf1bcfe'),
}


def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temporary=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            os.fchmod(f.fileno(),0o600);json.dump(value,f,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


@contextmanager
def registry_lock(root):
    path=root/'transport/.node-setup.lock'
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another node operation is running. Try again after it finishes.')
        yield
    finally:
        os.close(fd)


class Cloudflare:
    def __init__(self,token,client=None):
        self.client=client or httpx.Client(base_url='https://api.cloudflare.com/client/v4',timeout=30,
            headers={'Authorization':'Bearer '+token})

    def request(self,method,path,data=None,missing_ok=False):
        try:
            response=self.client.request(method,path,json=data)
        except httpx.HTTPError:
            raise ValueError('Could not reach Cloudflare; no credential values were logged.') from None
        if missing_ok and response.status_code==404:return None
        try:body=response.json()
        except ValueError:
            raise ValueError('Cloudflare returned a non-JSON response (HTTP '+str(response.status_code)+'); retry after connectivity is restored.') from None
        if response.is_error or not body.get('success'):
            raise ValueError('Cloudflare '+method+' failed with HTTP '+str(response.status_code))
        return body.get('result')

    def close(self):self.client.close()


def validate_policies(policies):
    for policy in policies:
        includes=policy.get('include',[])
        if policy.get('decision')!='non_identity' or not includes or any(set(item)!={'service_token'} for item in includes):
            raise ValueError('Machine endpoint has an unexpected Access policy. Review it before adding nodes.')


def settings(root,api):
    deployment=json.loads((root/'cloudflare-deployment.json').read_text())
    transport=json.loads((root/'transport/cloudflare-test.json').read_text())
    account='/accounts/'+deployment['account_id']
    app_path=account+'/access/apps/'+transport['application']['id']
    app=api.request('GET',app_path)
    if app['domain']!=transport['domain'] or app['aud']!=transport['application']['aud']:
        raise ValueError('Machine application does not match the deployed transport')
    policies=api.request('GET',app_path+'/policies');validate_policies(policies)
    tunnel=api.request('GET',account+'/cfd_tunnel/'+deployment['tunnel_id']+'/configurations')['config']
    routes=[r for r in tunnel['ingress'] if r.get('hostname')==transport['domain']]
    expected={'required':True,'teamName':deployment['team'],'audTag':[app['aud']]}
    if len(routes)!=1 or routes[0]['service']!='tcp://127.0.0.1:14610' or routes[0].get('originRequest',{}).get('access')!=expected:
        raise ValueError('Machine ingress is not the expected authenticated loopback relay')
    return account,app_path,transport['domain'],policies


def write_bundle(target,name,files):
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.exists():raise ValueError('Output already exists: '+str(target))
    fd,temporary=tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600)
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                directory=zipfile.ZipInfo(name+'-installer/')
                directory.create_system=3;directory.external_attr=(stat.S_IFDIR|0o700)<<16
                archive.writestr(directory,b'')
                for filename,data in files.items():
                    entry=zipfile.ZipInfo(name+'-installer/'+filename)
                    entry.create_system=3
                    entry.external_attr=(stat.S_IFREG|(0o700 if filename=='install.sh' else 0o600))<<16
                    archive.writestr(entry,data)
            output.flush();os.fsync(output.fileno())
        os.link(temporary,target)  # Exclusive publication; never overwrite an existing file.
    finally:
        os.unlink(temporary)


def cleanup(root,state,api):
    errors=[]
    for key,path in (('policy',state.get('app_path','')+'/policies/'),
                     ('service_token',state.get('account','')+'/access/service_tokens/')):
        if state.get(key) and not state.get(key+'_removed'):
            try:
                api.request('DELETE',path+state[key]['id'],missing_ok=True)
                state[key+'_removed']=True
            except Exception:
                errors.append(key)
    allowed_path=root/'transport/allowed-clients.json'
    if state.get('fingerprint'):
        allowed=json.loads(allowed_path.read_text());allowed.pop(state['fingerprint'],None)
        save_json(allowed_path,allowed)
    if state.get('registration_owned'):
        auth.revoke_server(state['name'])
    state['status']='cleanup_required' if errors else 'revoked'
    state['cleanup_errors']=errors
    if not errors and state.get('service_token'):
        state['service_token'].pop('client_secret',None)
    save_json(root/'transport/nodes'/state['name']/'state.json',state)
    if errors:raise ValueError('Could not finish revoking Cloudflare credentials; rerun revoke-node-bundle for '+state['name'])


def create_bundle(name,root=None,output=None,port=14603,api=None):
    root=Path(root or auth.CONFIG_DIR).expanduser()
    if not NAME_RE.fullmatch(name):
        raise ValueError('Use a lowercase node name beginning with a letter, with letters, digits or hyphens (max 48 characters).')
    if not 1024<=port<=65535:raise ValueError('Port must be between 1024 and 65535')
    if not auth.ca_exists() or not (root/'transport/ca/ca.key').is_file():
        raise ValueError('Run this on the initialized gateway with its private transport CA.')
    own_api=api is None
    api=api or Cloudflare((root/'cloudflare-api-token').read_text().strip())
    try:
        with registry_lock(root):
            directory=root/'transport/nodes'/name
            if directory.exists() or (auth.SERVERS_DIR/name).exists() or auth.is_revoked(name):
                raise ValueError('That node name already exists or was retired. Use a new unique name.')
            target=Path(output).expanduser().absolute() if output else directory/(name+'-installer.zip')
            if target.exists():raise ValueError('Output already exists: '+str(target))
            account,app_path,hostname,policies=settings(root,api)
            directory.mkdir(parents=True,mode=0o700)
            state={'name':name,'status':'building','account':account,'app_path':app_path,
                'bundle_id':uuid.uuid4().hex,'created_at':datetime.now(timezone.utc).isoformat()}
            state_path=directory/'state.json';save_json(state_path,state)
            try:
                # Reserve this registry entry exclusively before issuing a certificate.
                (auth.SERVERS_DIR/name).mkdir(mode=0o700)
                state['registration_owned']=True;save_json(state_path,state)
                key,cert,ca=auth.issue_server_cert(name)
                state['fingerprint']=transport_pki.issue(root/'transport/ca',name,directory/'tls',days=90)
                save_json(state_path,state)
                state['service_token']=api.request('POST',account+'/access/service_tokens',
                    {'name':'Terminal node '+name+' '+state['bundle_id'][:8],'duration':'8760h'})
                save_json(state_path,state)
                state['policy']=api.request('POST',app_path+'/policies',{
                    'name':'Only terminal node '+name,'decision':'non_identity',
                    'precedence':max([p.get('precedence',0) for p in policies]+[0])+1,
                    'include':[{'service_token':{'token_id':state['service_token']['id']}}],
                    'exclude':[],'require':[]})
                save_json(state_path,state)
                current=api.request('GET',app_path+'/policies');validate_policies(current)
                if {p['id'] for p in current}!={p['id'] for p in policies}|{state['policy']['id']}:
                    raise ValueError('Access policies changed during provisioning; the new node was not published')
                added=next(p for p in current if p['id']==state['policy']['id'])
                if added['include']!=[{'service_token':{'token_id':state['service_token']['id']}}]:
                    raise ValueError('Node service-token policy verification failed')
                allowed_path=root/'transport/allowed-clients.json'
                allowed=json.loads(allowed_path.read_text());allowed[state['fingerprint']]=name
                save_json(allowed_path,allowed)
                files={f:(SOURCE/f).read_bytes() for f in ('server.py','protocol.py','auth.py','sysinfo.py','node_runtime.py','activity.py','integrations.py')}
                files.update({'server.key':key,'server.crt':cert,'ca.crt':ca,
                    'tls.key':(directory/'tls/tls.key').read_bytes(),
                    'tls.crt':(directory/'tls/tls.crt').read_bytes(),
                    'tls-ca.crt':(directory/'tls/ca.crt').read_bytes(),
                    'requirements-agent.txt':REQUIREMENTS.encode(),
                    'access-token.json':json.dumps({k:state['service_token'][k] for k in ('client_id','client_secret')}).encode(),
                    'config.json':json.dumps({'name':name,'gateway':'https://127.0.0.1:'+str(port),
                        'tls':{'ca':'tls-ca.crt','certificate':'tls.crt','private_key':'tls.key','server_hostname':hostname}}).encode()})
                expires=x509.load_pem_x509_certificate(files['tls.crt']).not_valid_after_utc.isoformat()
                manifest={'name':name,'hostname':hostname,'port':port,'expires_at':expires,
                    'files':{f:hashlib.sha256(data).hexdigest() for f,data in files.items()},
                    'cloudflared_version':VERSION,'cloudflared':{platform:{'url':'https://github.com/cloudflare/cloudflared/releases/download/'+VERSION+'/'+asset,'sha256':digest} for platform,(asset,digest) in ASSET_DIGESTS.items()}}
                files['manifest.json']=json.dumps(manifest,indent=2).encode()
                files['install.py']=(SOURCE/'node_install.py').read_bytes()
                files['install.sh']=b'#!/bin/sh\nset -eu\ncd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\nunset PYTHONPATH\nexec python3 install.py "$@"\n'
                files['README.txt']=('PRIVATE INSTALLER FOR '+name+'\n\nThis bundle contains this node\'s credentials. Copy it securely to only the intended machine.\nRun: sh install.sh\nPreview: sh install.sh --check\nLinux uses a system service (sudo may be requested); macOS starts at login.\nPython 3.10+ is required. Internet access installs pinned dependencies.\nExisting agents and Meshnet settings are preserved.\nClient certificate expires '+expires+'. Renewal is managed from the gateway.\nAfter successful installation, delete spare copies of this bundle.\n').encode()
                write_bundle(target,name,files)
                state.update(status='ready',bundle=str(target),expires_at=expires)
                save_json(state_path,state)
                return target
            except BaseException:
                cleanup(root,state,api)
                raise
    finally:
        if own_api:api.close()


def revoke_bundle(name,root=None,api=None):
    root=Path(root or auth.CONFIG_DIR).expanduser()
    if not NAME_RE.fullmatch(name):raise ValueError('Invalid node name')
    # This command is intentionally only for unused/offline bundles. Never hide a live agent.
    try:
        with urllib.request.urlopen('http://127.0.0.1:4601/api/servers',timeout=5) as response:
            servers=json.load(response)
    except Exception:
        raise ValueError('Cannot verify that this node is offline; nothing was revoked.')
    if any(s['name']==name and s['connected'] for s in servers):
        raise ValueError('This node is connected. Stop its service before revoking its installer.')
    own_api=api is None
    api=api or Cloudflare((root/'cloudflare-api-token').read_text().strip())
    try:
        with registry_lock(root):
            state=json.loads((root/'transport/nodes'/name/'state.json').read_text())
            cleanup(root,state,api)
    finally:
        if own_api:api.close()
