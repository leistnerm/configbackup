import importlib.util
import unittest
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parents[1] / "collectors" / "system" / "collect_system.py"
spec = importlib.util.spec_from_file_location("configbackup_system_collector", COLLECTOR)
syscol = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(syscol)


class SystemCollectorTests(unittest.TestCase):
    def test_windows_firewall_summary_describes_effective_match(self):
        rule = {
            "Direction": "Inbound",
            "Action": "Block",
            "Protocol": "TCP",
            "LocalPort": ["445"],
            "RemotePort": ["Any"],
            "LocalAddress": ["Any"],
            "RemoteAddress": ["192.0.2.10"],
            "Program": "Any",
            "Service": "Any",
        }
        summary = syscol.windows_firewall_rule_summary(rule)
        self.assertEqual(summary, "Inbound Block TCP local-port=445 remote-address=192.0.2.10")

    def test_windows_firewall_collector_uses_associated_filters(self):
        source = COLLECTOR.read_text(encoding="utf-8")
        self.assertIn("Get-NetFirewallRule -PolicyStore ActiveStore", source)
        self.assertIn("Get-NetFirewallAddressFilter", source)
        self.assertIn("Get-NetFirewallPortFilter", source)
        self.assertIn("Get-NetFirewallApplicationFilter", source)
        self.assertIn("Get-NetFirewallServiceFilter", source)
        self.assertIn("Get-NetFirewallInterfaceFilter", source)
        self.assertIn("Get-NetFirewallInterfaceTypeFilter", source)
        self.assertIn("Get-NetFirewallSecurityFilter", source)
        self.assertIn('firewall-rules.csv', source)


if __name__ == "__main__":
    unittest.main()
