"""Route parsing/stale IP fallback and real certificates, without network access."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

HERE = Path(__file__).resolve().parents[1]
from steering_control.asr import phone_tls as tls

ROUTES = """default via 192.168.50.1 dev wlan0 proto dhcp metric 600
default dev tun0 scope link metric 1
10.0.0.0/8 dev tun0 src 10.8.0.2
172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1
192.168.50.0/24 dev wlan0 proto kernel scope link src 192.168.50.26 metric 600
192.168.55.0/24 dev usb0 proto kernel scope link src 192.168.55.1
"""


class AddressTests(unittest.TestCase):
    def test_uses_default_interface_source_not_gateway_or_virtual_interface(self):
        self.assertEqual(tls.route_addresses(ROUTES), ["192.168.50.26", "192.168.55.1"])

    def test_stale_explicit_ip_falls_back_and_valid_override_is_kept(self):
        def output(command):
            return ROUTES if command[0] == "ip" else "192.168.50.26 192.168.55.1 172.17.0.1"
        with patch.object(tls, "_command_output", side_effect=output):
            self.assertEqual(tls.resolve_addresses(["192.168.226.10"])[0], "192.168.50.26")
            self.assertEqual(tls.resolve_addresses(["192.168.55.1"]), ["192.168.55.1"])

    def test_route_get_source_is_used_when_route_table_has_no_src(self):
        def output(command):
            if "get" in command:
                return "1.1.1.1 via 192.168.50.1 dev wlan0 src 192.168.50.26 uid 1000\n    cache"
            return ""
        with patch.object(tls, "_command_output", side_effect=output):
            self.assertEqual(tls.lan_addresses(), ["192.168.50.26"])

    def test_hostname_fallback_and_failed_detection_do_not_reuse_old_ip(self):
        with patch.object(tls, "_command_output", side_effect=lambda command: "192.168.50.26" if command[0] == "hostname" else ""):
            self.assertEqual(tls.lan_addresses(), ["192.168.50.26"])
        with patch.object(tls, "_command_output", return_value=""):
            self.assertEqual(tls.lan_addresses(), [])
            with self.assertRaises(ValueError):
                tls.resolve_addresses(["192.168.226.10"])

    def test_ip_change_issues_new_server_certificate_using_same_ca(self):
        root = HERE / "data/tests" / uuid4().hex / "phone_https"
        old_cert, old_key, old_ca = tls.certificate_for(["192.168.226.10"], root)
        previous = (old_cert.read_bytes(), old_key.read_bytes(), old_ca.read_bytes())
        cert, key, ca = tls.certificate_for(["192.168.50.26"], root)
        self.assertNotEqual(cert, old_cert)
        self.assertEqual(ca.read_bytes(), old_ca.read_bytes())
        check = subprocess.run([tls.OPENSSL, "verify", "-CAfile", str(old_ca), "-verify_ip", "192.168.50.26", str(cert)], capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual((old_cert.read_bytes(), old_key.read_bytes(), old_ca.read_bytes()), previous)
