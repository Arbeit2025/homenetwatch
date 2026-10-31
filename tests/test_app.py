"""homenetwatch のテスト。

ネットワークへの実アクセスには依存しない。
到達確認・ポート確認・名前解決は unittest.mock で差し替える。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as hn


class SubnetTest(unittest.TestCase):
    def test_guess_subnet_home(self):
        self.assertEqual(hn.guess_subnet("192.168.1.10"), "192.168.1.0/24")

    def test_guess_subnet_ten(self):
        self.assertEqual(hn.guess_subnet("10.0.0.5"), "10.0.0.0/24")

    def test_current_subnet_uses_local_ip(self):
        with patch.object(hn, "get_local_ip", return_value="192.168.0.33"):
            self.assertEqual(hn.current_subnet(), "192.168.0.0/24")


class PortCheckTest(unittest.TestCase):
    def test_check_port_open(self):
        with patch.object(hn.socket, "create_connection") as conn:
            conn.return_value.__enter__ = MagicMock(return_value=None)
            conn.return_value.__exit__ = MagicMock(return_value=False)
            self.assertTrue(hn.check_port("192.168.1.1", 80))

    def test_check_port_closed(self):
        with patch.object(
            hn.socket, "create_connection", side_effect=OSError("refused")
        ):
            self.assertFalse(hn.check_port("192.168.1.1", 80))


class PingTest(unittest.TestCase):
    def test_ping_host_success(self):
        completed = MagicMock(returncode=0)
        with patch.object(hn.subprocess, "run", return_value=completed):
            self.assertTrue(hn.ping_host("192.168.1.1"))

    def test_ping_host_failure(self):
        completed = MagicMock(returncode=1)
        with patch.object(hn.subprocess, "run", return_value=completed):
            self.assertFalse(hn.ping_host("192.168.1.99"))


class ScanTest(unittest.TestCase):
    def test_scan_host_alive_by_open_port(self):
        with patch.object(hn, "check_port", side_effect=lambda ip, p: p == 80), \
            patch.object(hn, "ping_host", return_value=False), \
            patch.object(hn, "resolve_hostname", return_value="router.local"):
            result = hn.scan_host("192.168.1.1")
        self.assertTrue(result["alive"])
        self.assertEqual(result["open_ports"], [80])
        self.assertEqual(result["hostname"], "router.local")

    def test_scan_network_returns_only_alive_sorted(self):
        fake = {
            "192.168.1.10": {"ip": "192.168.1.10", "alive": True,
                             "open_ports": [], "hostname": ""},
            "192.168.1.2": {"ip": "192.168.1.2", "alive": True,
                            "open_ports": [443], "hostname": "nas"},
        }

        def fake_scan_host(ip):
            return fake.get(ip, {"ip": ip, "alive": False,
                                 "open_ports": [], "hostname": ""})

        with patch.object(hn, "scan_host", side_effect=fake_scan_host):
            results = hn.scan_network("192.168.1.0/30")
        # /30 のホストは .1 と .2 のみ。.2 だけが alive として返る。
        self.assertEqual([r["ip"] for r in results], ["192.168.1.2"])


class WebTest(unittest.TestCase):
    def setUp(self):
        self.client = hn.app.test_client()

    def test_index_ok_and_shows_subnet(self):
        with patch.object(hn, "get_local_ip", return_value="192.168.1.10"):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("192.168.1.0/24", resp.get_data(as_text=True))

    def test_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_api_scan_shape_and_scope_fixed(self):
        fake_result = {
            "ip": "192.168.1.1", "alive": True,
            "open_ports": [80], "hostname": "router",
        }
        with patch.object(hn, "get_local_ip", return_value="192.168.1.10"), \
            patch.object(hn, "scan_network", return_value=[fake_result]) as scan:
            # 範囲を指定するつもりで余計なデータを送っても無視されること
            resp = self.client.post("/api/scan",
                                    json={"subnet": "203.0.113.0/24"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["subnet"], "192.168.1.0/24")
        self.assertEqual(data["host_count"], 1)
        self.assertEqual(data["hosts"][0]["ip"], "192.168.1.1")
        self.assertIn("scanned_at", data)
        # スキャンに渡されたのは自動推定サブネットのみ
        scan.assert_called_once_with("192.168.1.0/24")


if __name__ == "__main__":
    unittest.main()
