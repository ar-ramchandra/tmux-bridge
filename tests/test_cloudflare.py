"""Public exposure must fail closed and leave unrelated tunnels alone."""
import json
import unittest
from unittest.mock import patch, Mock

import httpx
import cloudflare


class CloudflareTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_email_policy_has_no_bypass(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'success': True, 'result': {'id': 'app', 'aud': 'aud'}})
        client = self.client(handler)
        with patch.object(cloudflare.httpx, 'AsyncClient', return_value=client):
            result = await cloudflare.setup_access('secret', 'account', 'terminal.example.com', 'me@example.com')
        self.assertEqual(result['aud'], 'aud')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1], {'name': 'Admin', 'decision': 'allow', 'include': [{'email': {'email': 'me@example.com'}}]})

    async def test_policy_failure_is_not_reported_as_success(self):
        def handler(request):
            if request.url.path.endswith('/policies'):
                return httpx.Response(403, json={'success': False})
            return httpx.Response(200, json={'success': True, 'result': {'id': 'app', 'aud': 'aud'}})
        client = self.client(handler)
        with patch.object(cloudflare.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'unpublished'):
                await cloudflare.setup_access('secret', 'account', 'terminal.example.com', 'me@example.com')

    async def test_existing_app_is_not_assumed_safe(self):
        client = self.client(lambda r: httpx.Response(409, json={'success': False, 'errors': [{'message': 'Already exists'}]}))
        with patch.object(cloudflare.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'review'):
                await cloudflare.setup_access('secret', 'account', 'terminal.example.com', 'me@example.com')

    async def test_ingress_requires_access_at_the_connector(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'success': True})
        client = self.client(handler)
        with patch.object(cloudflare.httpx, 'AsyncClient', return_value=client):
            await cloudflare.configure_ingress('secret', 'account', 'tunnel', 'terminal.example.com', team_name='team', audience='aud')
        rules = requests[0]['config']['ingress']
        self.assertEqual(rules[0]['service'], 'http://127.0.0.1:4601')
        self.assertEqual(rules[0]['originRequest']['access'], {'required': True, 'teamName': 'team', 'audTag': ['aud']})
        self.assertEqual(rules[-1], {'service': 'http_status:404'})
        with self.assertRaises(ValueError):
            await cloudflare.configure_ingress('secret', 'account', 'tunnel', 'terminal.example.com', team_name='', audience='aud')

    async def test_unprotected_tunnel_never_starts_or_stops_another_tunnel(self):
        with patch.object(cloudflare, 'kill_cloudflared') as stop, patch.object(cloudflare.subprocess, 'Popen') as start:
            for config in ({}, {'quick_tunnel': True}, {'cf_tunnel_token': 'secret', 'domain': 'terminal.example.com'}):
                with self.assertRaises(ValueError):
                    await cloudflare.start_cloudflared(config)
            stop.assert_not_called()
            start.assert_not_called()

    def test_shutdown_only_stops_its_owned_process(self):
        own = Mock()
        with patch.object(cloudflare, '_cloudflared_proc', own), patch.object(cloudflare.subprocess, 'run') as run:
            cloudflare.kill_cloudflared()
            own.terminate.assert_called_once()
            run.assert_not_called()
