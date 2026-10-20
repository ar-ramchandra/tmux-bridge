"""Provision private nodes against isolated CAs and a fake Cloudflare account."""
import copy
import asyncio
import io
import json
from pathlib import Path
import plistlib
import ssl
import stat
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography import x509

import auth
import node_install
import node_setup
import node_runtime
import mtls_proxy
import transport_pki


class FakeCloudflare:
    def __init__(self):
        self.policies=[{'id':'production-policy','decision':'non_identity','precedence':1,
            'include':[{'service_token':{'token_id':'production-token'}}]}]
        self.tokens={};self.calls=[];self.fail_policy=False

    def request(self,method,path,data=None,missing_ok=False):
        self.calls.append((method,path,copy.deepcopy(data)))
        if method=='GET' and path.endswith('/access/apps/app'):
            return {'domain':'bridge.example.test','aud':'aud'}
        if method=='GET' and path.endswith('/policies'):
            return copy.deepcopy(self.policies)
        if method=='GET' and path.endswith('/configurations'):
            return {'config':{'ingress':[{'hostname':'bridge.example.test','service':'tcp://127.0.0.1:14610',
                'originRequest':{'access':{'required':True,'teamName':'team','audTag':['aud']}}}]}}
        if method=='POST' and path.endswith('/service_tokens'):
            token={'id':'token-'+str(len(self.tokens)+1),'client_id':'client-'+str(len(self.tokens)+1),
                'client_secret':'private-service-secret-'+str(len(self.tokens)+1)}
            self.tokens[token['id']]=token;return copy.deepcopy(token)
        if method=='POST' and path.endswith('/policies'):
            if self.fail_policy:raise ValueError('Cloudflare policy creation denied')
            policy=dict(data,id='policy-'+str(len(self.policies)))
            self.policies.append(policy);return copy.deepcopy(policy)
        if method=='DELETE' and '/policies/' in path:
            self.policies=[p for p in self.policies if p['id']!=path.split('/')[-1]];return {}
        if method=='DELETE' and '/service_tokens/' in path:
            self.tokens.pop(path.split('/')[-1],None);return {}
        raise AssertionError((method,path))


class NodeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'private';self.gateway=Path(self.temp.name)/'gateway'
        self.patch=patch.multiple(auth,CONFIG_DIR=self.gateway,CA_KEY_FILE=self.gateway/'ca.key',
            CA_CERT_FILE=self.gateway/'ca.crt',SERVERS_DIR=self.gateway/'servers',
            TOKENS_DIR=self.gateway/'tokens',REVOKED_FILE=self.gateway/'revoked.json')
        self.patch.start();self.addCleanup(self.patch.stop)
        auth.ensure_config_dir();auth.init_ca()
        transport_pki.init_ca(self.root/'transport/ca')
        node_setup.save_json(self.root/'cloudflare-deployment.json',{'account_id':'account','tunnel_id':'tunnel','team':'team'})
        node_setup.save_json(self.root/'transport/cloudflare-test.json',{'domain':'bridge.example.test','application':{'id':'app','aud':'aud'}})
        node_setup.save_json(self.root/'transport/allowed-clients.json',{'existing-fingerprint':'hpc'})
        (self.root/'cloudflare-api-token').write_text('ADMIN-TOKEN-NEVER-EXPORT')
        self.api=FakeCloudflare()

    def create(self,name='new-node',**kwargs):
        return node_setup.create_bundle(name,root=self.root,api=self.api,**kwargs)

    def unpack(self,target):
        folder=Path(self.temp.name)/('unpacked-'+target.stem)
        with zipfile.ZipFile(target) as archive:archive.extractall(folder)
        return next(folder.iterdir())

    def test_private_bundle_has_unique_leaf_credentials_and_no_admin_or_ca_key(self):
        first=self.create();second=self.create('second-node')
        self.assertEqual(first.stat().st_mode&0o777,0o600)
        with zipfile.ZipFile(first) as archive:
            content=b'\n'.join(archive.read(n) for n in archive.namelist())
            self.assertNotIn(b'ADMIN-TOKEN-NEVER-EXPORT',content)
            self.assertNotIn(auth.CA_KEY_FILE.read_bytes(),content)
            self.assertNotIn((self.root/'transport/ca/ca.key').read_bytes(),content)
            for entry in archive.infolist():
                self.assertEqual((entry.external_attr>>16)&0o077,0)
        a,b=self.unpack(first),self.unpack(second)
        self.assertNotEqual((a/'tls.key').read_bytes(),(b/'tls.key').read_bytes())
        self.assertNotEqual((a/'access-token.json').read_bytes(),(b/'access-token.json').read_bytes())
        manifest=node_install.check_bundle(a)
        self.assertEqual(manifest['name'],'new-node')
        cert=x509.load_pem_x509_certificate((a/'tls.crt').read_bytes())
        self.assertIn(cert.fingerprint(node_setup.hashes.SHA256()).hex(),json.loads((self.root/'transport/allowed-clients.json').read_text()))
        self.assertEqual(self.api.policies[0]['id'],'production-policy')
        self.assertEqual(len(self.api.policies),3)
        self.assertTrue(all(p['decision']=='non_identity' for p in self.api.policies))

    def test_duplicate_or_invalid_names_cannot_overwrite_identity_or_make_api_changes(self):
        bundle=self.create();original=bundle.read_bytes();calls=len(self.api.calls)
        for name in ('new-node','../escape','bad/name','-option','Name With Spaces'):
            with self.assertRaises(ValueError):self.create(name)
        self.assertEqual(len(self.api.calls),calls)
        self.assertEqual(bundle.read_bytes(),original)

    def test_policy_failure_revokes_new_token_and_removes_only_new_registration(self):
        self.api.fail_policy=True
        with self.assertRaisesRegex(ValueError,'policy creation denied'):self.create()
        self.assertEqual(self.api.tokens,{})
        self.assertFalse((auth.SERVERS_DIR/'new-node').exists())
        self.assertEqual(json.loads((self.root/'transport/allowed-clients.json').read_text()),{'existing-fingerprint':'hpc'})
        self.assertEqual(len(self.api.policies),1)

    def test_existing_bypass_is_rejected_before_issuing_credentials(self):
        self.api.policies.append({'id':'unsafe','decision':'bypass','include':[{'everyone':{}}]})
        with self.assertRaisesRegex(ValueError,'unexpected Access policy'):self.create()
        self.assertEqual(self.api.tokens,{})
        self.assertFalse((auth.SERVERS_DIR/'new-node').exists())

    def test_tampered_payload_and_plaintext_transport_are_rejected(self):
        bundle=self.unpack(self.create())
        (bundle/'server.py').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'checksum'):node_install.check_bundle(bundle)
        # Even if checksums are recalculated, the installer refuses plaintext.
        manifest=json.loads((bundle/'manifest.json').read_text())
        config=json.loads((bundle/'config.json').read_text());config['gateway']='http://127.0.0.1:14603'
        (bundle/'config.json').write_text(json.dumps(config))
        for name in manifest['files']:manifest['files'][name]=node_setup.hashlib.sha256((bundle/name).read_bytes()).hexdigest()
        (bundle/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'mTLS'):node_install.check_bundle(bundle)

    def test_existing_machine_and_service_are_preserved(self):
        home=Path(self.temp.name)/'home';(home/'.tmux-bridge').mkdir(parents=True)
        p=home/'.tmux-bridge/config.json';p.write_text('original')
        with self.assertRaisesRegex(ValueError,'already runs a bridge'):node_install.check_target(home,'new-node',14603,True)
        self.assertEqual(p.read_text(),'original')

    def test_revoking_live_node_is_refused_and_offline_bundle_is_cleanly_revoked(self):
        self.create()
        with patch.object(node_setup.urllib.request,'urlopen',return_value=io.BytesIO(b'[{"name":"new-node","connected":true}]')):
            with self.assertRaisesRegex(ValueError,'connected'):node_setup.revoke_bundle('new-node',root=self.root,api=self.api)
        self.assertEqual(len(self.api.policies),2)
        with patch.object(node_setup.urllib.request,'urlopen',return_value=io.BytesIO(b'[{"name":"new-node","connected":false}]')):
            node_setup.revoke_bundle('new-node',root=self.root,api=self.api)
        self.assertEqual(len(self.api.policies),1)
        self.assertTrue(auth.is_revoked('new-node'))
        self.assertEqual(self.api.tokens,{})

    def test_service_definitions_preserve_tmux_and_contain_no_credentials(self):
        root=Path('/Users/some user/100%node')
        linux=node_install.service_bytes(root,'new-node','linux',1000).decode()
        self.assertIn('KillMode=process',linux)
        self.assertIn('100%%node',linux)
        self.assertNotIn('client_secret',linux)
        mac=plistlib.loads(node_install.service_bytes(root,'new-node','darwin',501))
        self.assertTrue(mac['AbandonProcessGroup'])
        self.assertEqual(mac['ProgramArguments'][0],str(root/'.venv/bin/python'))
        self.assertNotIn('TUNNEL_SERVICE_TOKEN_SECRET',mac['EnvironmentVariables'])

    def test_binary_checksum_failure_never_installs_or_executes_download(self):
        root=Path(self.temp.name)/'download';root.mkdir()
        asset={'url':'https://github.com/cloudflare/cloudflared/releases/download/2026.9.1/test','sha256':'0'*64}
        with patch.object(node_install.urllib.request,'urlopen',return_value=io.BytesIO(b'wrong content')):
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):node_install.download_cloudflared(root,asset)
        self.assertFalse((root/'bin/cloudflared').exists())

    def test_failed_probe_removes_new_install_without_starting_service(self):
        bundle=self.unpack(self.create());home=Path(self.temp.name)/'clean-home';home.mkdir()
        def run(args,**kwargs):
            if '--probe' in args:raise subprocess.CalledProcessError(1,args)
            return subprocess.CompletedProcess(args,0)
        with patch.object(node_install,'__file__',str(bundle/'install.py')),patch.object(Path,'home',return_value=home),\
             patch.object(node_install,'check_target',return_value=home/'.tmux-bridge-nodes/new-node'),\
             patch.object(node_install,'ensure_tmux',return_value='/usr/bin/tmux'),\
             patch.object(node_install.subprocess,'run',side_effect=run),\
             patch.object(node_install,'run_probe',side_effect=subprocess.CalledProcessError(1,['probe'])),\
             patch.object(node_install,'download_cloudflared'),patch.object(node_install,'install_service') as service:
            self.assertEqual(node_install.main([]),1)
        service.assert_not_called()
        self.assertFalse((home/'.tmux-bridge-nodes/new-node').exists())

    def test_interrupted_probe_terminates_supervisor_before_install_cleanup(self):
        from unittest.mock import Mock
        child=Mock()
        child.wait.side_effect=[KeyboardInterrupt(),0]
        child.poll.return_value=None
        with patch.object(node_install.subprocess,'Popen',return_value=child):
            with self.assertRaises(KeyboardInterrupt):node_install.run_probe('python',Path('/node'),{})
        child.terminate.assert_called_once()
        self.assertEqual(child.wait.call_count,2)

    def test_runtime_authenticates_registers_and_preserves_tmux_on_stop(self):
        """Real bundle/agent/TLS, with a local TCP carrier replacing Cloudflare."""
        from websockets.legacy.server import serve
        bundle=self.unpack(self.create())
        tmux=shutil.which('tmux')
        if not tmux:self.skipTest('tmux unavailable')
        socket_name='node-test-'+node_setup.uuid.uuid4().hex
        subprocess.run([tmux,'-L',socket_name,'new-session','-d','-s','preserved'],check=True)
        self.addCleanup(lambda:subprocess.run([tmux,'-L',socket_name,'kill-server'],capture_output=True))
        binaries=bundle/'bin';binaries.mkdir()
        # The real agent sees only this test's tmux socket.
        import shlex
        (binaries/'tmux').write_text('#!/bin/sh\nexec '+shlex.quote(tmux)+' -L '+socket_name+' "$@"\n')
        (binaries/'tmux').chmod(0o700)
        transport_pki.issue(self.root/'transport/ca','bridge.example.test',self.root/'relay',server=True)

        async def exercise():
            registered=asyncio.Event();received=[]
            async def gateway(ws,path):
                proof=json.loads(await ws.recv())
                name=auth.verify_auth_proof(proof['cert'].encode(),proof['signature'],proof['timestamp'])
                await ws.send(json.dumps({'type':'auth_ok','name':name}))
                async for raw in ws:
                    if isinstance(raw,str):
                        message=json.loads(raw);received.append(message)
                        if message.get('type')=='register':registered.set()
            upstream=await serve(gateway,'127.0.0.1',0)
            relay=await mtls_proxy.AgentTLSProxy({'port':0,
                'upstream':'ws://127.0.0.1:'+str(upstream.sockets[0].getsockname()[1])+'/server-ws',
                'certificate':str(self.root/'relay/tls.crt'),'private_key':str(self.root/'relay/tls.key'),
                'client_ca':str(self.root/'transport/ca/ca.crt'),
                'allowed_clients':str(self.root/'transport/allowed-clients.json')}).start()
            child=None
            try:
                relay_port=relay.sockets[0].getsockname()[1]
                config=json.loads((bundle/'config.json').read_text())
                config['gateway']='https://127.0.0.1:'+str(relay_port)
                (bundle/'config.json').write_text(json.dumps(config))
                await node_runtime.authenticate(bundle)
                self.assertFalse(registered.is_set(), 'Probe must not register or displace an agent')
                config['tls']['server_hostname']='wrong.example.test'
                (bundle/'config.json').write_text(json.dumps(config))
                with self.assertRaises(ssl.SSLCertVerificationError):await node_runtime.authenticate(bundle)
                config['tls']['server_hostname']='bridge.example.test'
                with node_install.socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                config['gateway']='https://127.0.0.1:'+str(port)
                (bundle/'config.json').write_text(json.dumps(config))
                (bundle/'installation.json').write_text(json.dumps({'port':port,'tmux':str(binaries/'tmux')}))
                (binaries/'cloudflared').write_text('#!'+sys.executable+'\n'+'''
import asyncio
async def handle(reader,writer):
    remote_reader,remote_writer=await asyncio.open_connection('127.0.0.1',RELAY)
    async def copy(source,target):
        try:
            while data:=await source.read(65536):
                target.write(data);await target.drain()
        finally:target.close()
    await asyncio.gather(copy(reader,remote_writer),copy(remote_reader,writer))
async def main():
    listener=await asyncio.start_server(handle,'127.0.0.1',PORT)
    async with listener:await listener.serve_forever()
asyncio.run(main())
'''.replace('RELAY',str(relay_port)).replace('PORT',str(port)))
                (binaries/'cloudflared').chmod(0o700)
                child=await asyncio.create_subprocess_exec(sys.executable,str(bundle/'node_runtime.py'),
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
                await asyncio.wait_for(registered.wait(),15)
                registration=next(m for m in received if m.get('type')=='register')
                self.assertEqual(registration['name'],'new-node')
                self.assertEqual([s['name'] for s in registration['sessions']],['preserved'])
                child.terminate();output,_=await asyncio.wait_for(child.communicate(),10)
                self.assertEqual(child.returncode,0,output.decode())
                self.assertNotIn(b'private-service-secret',output)
                subprocess.run([tmux,'-L',socket_name,'has-session','-t','preserved'],check=True)
                with node_install.socket.socket() as sock:sock.bind(('127.0.0.1',port))
            finally:
                if child is not None and child.returncode is None:
                    child.terminate();await asyncio.wait_for(child.communicate(),10)
                for listener in (relay,upstream):
                    listener.close();await listener.wait_closed()
        asyncio.run(exercise())


if __name__=='__main__':unittest.main()
