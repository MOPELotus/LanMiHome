import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location('integration', Path(__file__).parents[1] / 'integration.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def status(active=None):
    return dict(active=[1, 2, 3] if active is None else active,
                lines={str(identity): dict(interface=interface)
                       for identity, interface in [(1, 'eth1'), (2, 'cwan2'), (3, 'cwan3')]})


class IntegrationTests(unittest.TestCase):
    def test_marks_only_network_outbounds(self):
        config = {'outbounds': [{'type': 'direct'}, {'type': 'anytls'}, {'type': 'urltest'}],
                  'dns': {'servers': [{'type': 'udp'}, {'type': 'https', 'detour': 'proxy'}, {'type': 'local'}]}}
        patched = MODULE.patch_config(config)
        self.assertEqual(patched['outbounds'][0]['routing_mark'], 0x4200)
        self.assertEqual(patched['outbounds'][1]['routing_mark'], 0x4200)
        self.assertNotIn('routing_mark', patched['outbounds'][2])
        self.assertEqual(patched['dns']['servers'][0]['routing_mark'], 0x4200)
        self.assertNotIn('routing_mark', patched['dns']['servers'][1])
        self.assertNotIn('routing_mark', patched['dns']['servers'][2])

    def test_binding_is_not_silently_overridden(self):
        with self.assertRaises(ValueError):
            MODULE.patch_config({'outbounds': [{'type': 'direct', 'bind_interface': 'wan'}]})

    def test_domains_normalize_and_deduplicate(self):
        self.assertEqual(MODULE.direct_domains('# comment\nSteamContent.com\n*.example.org.\nsteamcontent.com\n'),
                         ['example.org', 'steamcontent.com'])

    def test_invalid_domain_cannot_inject_dnsmasq(self):
        for value in ['steam', 'example.org/1.1.1.1', 'a..com', '-bad.com', 'a.com;server=evil']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.direct_domains(value)

    def test_dnsmasq_tracks_both_families(self):
        self.assertIn('/example.org/4#inet#fw4#lotus_direct4,6#inet#fw4#lotus_direct6', MODULE.dnsmasq_text(['example.org']))

    def test_template_preserves_original_and_is_idempotent(self):
        original = 'chain homeproxy_mangle_tun {\n original rule\n}\n'
        patched = MODULE.patch_template(original)
        self.assertIn(' original rule', patched)
        self.assertLess(patched.index('meta mark 0x4200'), patched.index(' original rule'))
        self.assertEqual(MODULE.patch_template(patched), patched)

    def test_policy_pins_connections_and_scopes_marks(self):
        source = MODULE.render_policy(status())
        self.assertIn('type route hook output priority -140', source)
        self.assertIn('meta mark != 0x4200 return', source)
        self.assertIn('ct mark 0x4302 meta mark set 0x4102 counter return', source)
        self.assertLess(source.index('ct mark 0x4302'), source.index('numgen'))
        self.assertIn('ct state != new return', source)
        self.assertIn('numgen inc mod 3 map { 0 : 0x4301, 1 : 0x4302, 2 : 0x4303 }', source)

    def test_secondary_source_nat_is_not_lan_only(self):
        source = MODULE.render_policy(status())
        self.assertIn('oifname "cwan2" ct mark 0x4302 counter masquerade', source)
        self.assertIn('priority 98', source)
        self.assertNotIn('ip saddr', source)

    def test_down_lines_excluded_and_no_active_has_no_assignment(self):
        self.assertIn('mod 2 map { 0 : 0x4301, 1 : 0x4303 }', MODULE.render_policy(status([1, 3])))
        self.assertNotIn('numgen', MODULE.render_policy(status([])))

    def test_private_and_control_destinations_excluded(self):
        source = MODULE.render_policy(status())
        self.assertIn('10.0.0.0/8', source)
        self.assertIn('fib daddr type local return', source)

    def test_atomic_table_and_input_validation(self):
        self.assertTrue(MODULE.render_policy(status(), True).startswith('delete table ip lotus_proxy_multiwan\n'))
        invalid = status()
        invalid['lines']['2']['interface'] = 'cwan2; reboot'
        with self.assertRaises(ValueError):
            MODULE.render_policy(invalid)


if __name__ == '__main__':
    unittest.main()
