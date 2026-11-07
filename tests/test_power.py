import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
import gateway
from power import WakeController


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'wake.json'
        self.path.write_text(json.dumps({'workstation': {
            'network': '192.168.40.0/24', 'macs': ['08:bf:b8:16:66:71']}}))
        self.wake = WakeController(self.path)

    def test_packet_destination_and_cooldown(self):
        sock = Mock()
        with patch('power.make_socket') as create:
            create.return_value.__enter__.return_value = sock
            self.assertEqual(self.wake.wake('workstation')[0], 202)
            self.assertEqual(self.wake.wake('workstation')[0], 429)
        self.assertEqual(sock.sendto.call_count, 3)
        packet, destination = sock.sendto.call_args.args
        self.assertEqual(destination, ('192.168.40.255', 9))
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b'\xff' * 6)
        self.assertEqual(packet[6:], bytes.fromhex('08bfb8166671') * 16)

    def test_bad_configuration_and_unknown_nodes_cannot_send(self):
        for raw in [None, [], {}, {'workstation': {'network': '8.8.8.0/24', 'macs': ['08:bf:b8:16:66:71']}},
                    {'workstation': {'network': '192.168.40.0/24', 'macs': ['ff:ff:ff:ff:ff:ff']}}]:
            self.path.write_text(json.dumps(raw))
            with patch('power.make_socket') as create:
                self.assertEqual(self.wake.wake('workstation')[0], 404)
                create.assert_not_called()

    def test_failure_is_not_reported_as_sent_and_can_retry(self):
        with patch('power.make_socket', side_effect=OSError('no route')):
            self.assertEqual(self.wake.wake('workstation')[0], 503)
        self.assertEqual(self.wake.sent, {})

    def test_api_rejects_cross_origin_revoked_and_arbitrary_destinations(self):
        client = TestClient(gateway.app)
        with patch.object(gateway, 'wake_controller', self.wake), \
             patch.object(gateway, 'list_registered_servers', return_value=[{'name':'workstation'}]), \
             patch.object(gateway.gw_manager, 'get_server', return_value=None), \
             patch('power.make_socket') as create:
            sock = create.return_value.__enter__.return_value
            self.assertEqual(client.get('/api/workstation/wake').status_code, 405)
            self.assertEqual(client.post('/api/workstation/wake', headers={'Origin':'https://evil.example'}).status_code, 403)
            self.assertEqual(client.post('/api/unknown/wake').status_code, 404)
            create.assert_not_called()
            response = client.post('/api/workstation/wake', json={'network':'8.8.8.0/24', 'macs':['00:00:00:00:00:01']})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(sock.sendto.call_args.args[1], ('192.168.40.255', 9))
        with patch.object(gateway, 'list_registered_servers', return_value=[{'name':'workstation','revoked':True}]), \
             patch.object(gateway.wake_controller, 'wake') as send:
            self.assertEqual(client.post('/api/workstation/wake').status_code, 404)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
