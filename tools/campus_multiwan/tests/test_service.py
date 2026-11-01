import importlib.util
import ipaddress
import json
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

try:
    import fcntl
except ImportError:
    sys.modules['fcntl'] = types.SimpleNamespace()

SPEC = importlib.util.spec_from_file_location('campus_multiwan_under_test', Path(__file__).resolve().parents[1] / 'service.py')
SERVICE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVICE)
CONFIG = dict(parent='eth1', lan_interfaces=['br-lan'], clients=['10.0.0.0/24'], destinations=['0.0.0.0/0'], diagnostic_ports=False, lines=[dict(id=2, interface='cwan2', mac='02:00:00:00:00:02'), dict(id=3, interface='cwan3', mac='02:00:00:00:00:03')])


class PolicyTests(unittest.TestCase):
    def test_three_way_selection(self):
        rules = SERVICE.render_policy(CONFIG, [1, 2, 3], False)
        self.assertIn('numgen inc mod 3 map { 0 : 0x4101, 1 : 0x4102, 2 : 0x4103 }', rules)
        self.assertIn('type filter hook prerouting priority -90', rules)

    def test_excludes_down_line(self):
        rules = SERVICE.render_policy(CONFIG, [1, 3], False)
        self.assertIn('numgen inc mod 2 map { 0 : 0x4101, 1 : 0x4103 }', rules)

    def test_respects_proxy_and_foreign_marks(self):
        rules = SERVICE.render_policy(CONFIG, [1, 2, 3], False)
        self.assertLess(rules.index('meta mark != 0 return'), rules.index('meta mark set ct mark'))
        self.assertIn('ct mark != 0 return', rules)

    def test_only_new_connections_assigned(self):
        rules = SERVICE.render_policy(CONFIG, [1, 2, 3], False)
        self.assertLess(rules.index('ct state != new return'), rules.index('numgen inc'))
        self.assertIn('ct mark { 0x4101, 0x4102, 0x4103 } meta mark set ct mark return', rules)

    def test_private_destinations_excluded(self):
        rules = SERVICE.render_policy(CONFIG, [1, 2], False)
        for subnet in ['10.0.0.0/8', '100.64.0.0/10', '192.168.0.0/16', '224.0.0.0/4']:
            self.assertIn(subnet, rules)
        self.assertIn('fib daddr type local return', rules)

    def test_diagnostic_overrides_are_opt_in(self):
        self.assertNotIn('tcp sport 43000', SERVICE.render_policy(CONFIG, [1, 2, 3], False))
        config = dict(CONFIG, diagnostic_ports=True)
        rules = SERVICE.render_policy(config, [1, 2, 3], False)
        for port in ['42000-42999', '43000-43999', '44000-44999']:
            self.assertIn(port, rules)

    def test_no_active_lines_no_assignment(self):
        self.assertNotIn('numgen', SERVICE.render_policy(CONFIG, [], False))

    def test_table_replacement_atomic_input(self):
        rules = SERVICE.render_policy(CONFIG, [1, 2], True)
        self.assertTrue(rules.startswith('delete table ip campus_multiwan\ntable ip campus_multiwan'))

    def test_nat_scoped_to_secondary_interfaces(self):
        rules = SERVICE.render_policy(CONFIG, [1, 2, 3], False)
        self.assertNotIn('oifname "eth1"', rules)
        self.assertIn('oifname "cwan2" ip saddr { 10.0.0.0/24 } counter masquerade', rules)

    def test_source_and_mark_policies_separate(self):
        self.assertEqual(SERVICE.source_arguments(2, '10.184.239.190'), ['priority', '10102', 'from', '10.184.239.190/32', 'lookup', '21002'])
        self.assertEqual(SERVICE.policy_arguments(CONFIG, 3), ['priority', '10003', 'fwmark', '0x4103', 'lookup', '21003'])


class ConfigTests(unittest.TestCase):
    def check(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            with patch.object(SERVICE, 'CONFIG', path):
                return SERVICE.load_config()

    def test_valid_config(self):
        self.assertEqual(self.check(CONFIG), CONFIG)

    def test_duplicate_identity_rejected(self):
        config = json.loads(json.dumps(CONFIG))
        config['lines'][1]['mac'] = config['lines'][0]['mac']
        with self.assertRaises(ValueError):
            self.check(config)

    def test_interface_injection_rejected(self):
        with self.assertRaises(ValueError):
            self.check(dict(CONFIG, parent='eth1; reboot'))

    def test_invalid_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.check(dict(CONFIG, clients=['all']))

    def test_only_two_extra_devices(self):
        with self.assertRaises(ValueError):
            self.check(dict(CONFIG, lines=CONFIG['lines'][:1]))


class BindingTests(unittest.TestCase):
    def test_connector_binds_device_and_address(self):
        stream = MagicMock()
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 80))]
        with patch.object(SERVICE.socket, 'getaddrinfo', return_value=answers), patch.object(SERVICE.socket, 'socket', return_value=stream), patch.object(SERVICE.socket, 'SO_BINDTODEVICE', 25, create=True):
            result = SERVICE.interface_connector('cwan2', '10.184.239.190')(('example.test', 80), 5)
        self.assertIs(result, stream)
        stream.bind.assert_called_once_with(('10.184.239.190', 0))
        stream.setsockopt.assert_called_once_with(socket.SOL_SOCKET, 25, b'cwan2\0')
        stream.connect.assert_called_once_with(('192.0.2.1', 80))

    def test_connect_failure_closes_socket(self):
        stream = MagicMock()
        stream.connect.side_effect = OSError('unreachable')
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 80))]
        with patch.object(SERVICE.socket, 'getaddrinfo', return_value=answers), patch.object(SERVICE.socket, 'socket', return_value=stream), patch.object(SERVICE.socket, 'SO_BINDTODEVICE', 25, create=True):
            with self.assertRaises(OSError):
                SERVICE.interface_connector('cwan2', '10.184.239.190')(('example.test', 80), 5)
        stream.close.assert_called_once()

class FixedRoutingTests(unittest.TestCase):
    def config(self):
        config = json.loads(json.dumps(CONFIG))
        config['clients'] = ['192.0.2.0/24', '198.51.100.0/24']
        config['fixed_routes'] = {'192.0.2.0/24': 1, '198.51.100.0/24': 2}
        config['proxy_line'] = 1
        config['lines'][1]['enabled'] = False
        return config

    def test_fixed_routes_replace_balancing(self):
        rules = SERVICE.render_policy(self.config(), [1, 2], False)
        self.assertNotIn('numgen', rules)
        self.assertIn('ip saddr 192.0.2.0/24 ct mark set 0x4101', rules)
        self.assertIn('ip saddr 198.51.100.0/24 ct mark set 0x4102', rules)

    def test_shared_does_not_fall_back_when_its_line_is_offline(self):
        rules = SERVICE.render_policy(self.config(), [1], False)
        self.assertIn('ip saddr 198.51.100.0/24 ct mark set 0x4102', rules)
        self.assertNotIn('numgen', rules)

    def test_fixed_config_validates(self):
        self.assertEqual(ConfigTests().check(self.config())['proxy_line'], 1)

    def test_disabled_destination_is_rejected(self):
        config = self.config()
        config['fixed_routes']['198.51.100.0/24'] = 3
        with self.assertRaises(ValueError):
            ConfigTests().check(config)

    def test_overlapping_or_missing_client_routes_are_rejected(self):
        config = self.config()
        config['fixed_routes'].pop('198.51.100.0/24')
        with self.assertRaises(ValueError):
            ConfigTests().check(config)
        config = self.config()
        config['clients'] = ['192.0.2.0/24', '192.0.2.0/25']
        config['fixed_routes'] = dict.fromkeys(config['clients'], 1)
        with self.assertRaises(ValueError):
            ConfigTests().check(config)

    def test_invalid_disable_flag_and_proxy_target_are_rejected(self):
        config = self.config()
        config['lines'][1]['enabled'] = 'false'
        with self.assertRaises(ValueError):
            ConfigTests().check(config)
        config = self.config()
        config['proxy_line'] = 3
        with self.assertRaises(ValueError):
            ConfigTests().check(config)

    def test_single_line_cleanup_does_not_remove_another_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(SERVICE, 'RUNTIME', Path(directory)), patch.object(SERVICE, 'run') as runner:
                SERVICE.cleanup_line(self.config(), self.config()['lines'][1])
            commands = [call.args[0] for call in runner.call_args_list]
            self.assertIn(['ip', 'link', 'del', 'cwan3'], commands)
            self.assertNotIn(['ip', 'link', 'del', 'cwan2'], commands)

if __name__ == '__main__':
    unittest.main()

