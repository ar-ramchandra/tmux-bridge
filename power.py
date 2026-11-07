"""Operator-configured LAN wake packets; never infer that offline means asleep."""
import ipaddress
import json
import re
import socket
from socket import socket as make_socket
import time
from pathlib import Path


class WakeController:
    def __init__(self, config: Path):
        self.config = config
        self.sent = {}

    def targets(self):
        try:
            raw = json.loads(self.config.read_text())
            result = {}
            for name, entry in raw.items():
                if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', name):
                    raise ValueError('Invalid node name')
                network = ipaddress.IPv4Network(entry['network'])
                if not network.is_private or not 16 <= network.prefixlen <= 30:
                    raise ValueError('Wake requires a private local subnet')
                macs = entry['macs']
                if not isinstance(macs, list) or not 1 <= len(macs) <= 4:
                    raise ValueError('Expected one to four MAC addresses')
                for mac in macs:
                    if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
                        raise ValueError('Invalid MAC address')
                    if int(mac[:2], 16) & 1 or int(mac.replace(':', ''), 16) == 0:
                        raise ValueError('Expected a unicast MAC address')
                result[name] = (str(network.broadcast_address), macs)
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}

    def wake(self, name):
        target = self.targets().get(name)
        if target is None:
            return 404, {'error': 'Wake is not configured for this device'}
        now = time.monotonic()
        if now - self.sent.get(name, -float('inf')) < 15:
            return 429, {'error': 'Wake already requested; wait a few seconds'}
        address, macs = target
        try:
            with make_socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(1)
                for mac in macs:
                    packet = b'\xff' * 6 + bytes.fromhex(mac.replace(':', '')) * 16
                    for _ in range(3):
                        sock.sendto(packet, (address, 9))
        except OSError:
            return 503, {'error': 'Could not send wake packet from the gateway'}
        self.sent[name] = now
        return 202, {'status': 'wake_sent', 'message': 'Wake request sent. Waiting for the device to reconnect.'}
