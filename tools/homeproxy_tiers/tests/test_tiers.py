import copy
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest
import json
import tempfile
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('tiered_proxy', Path(__file__).parents[1] / 'tiered_proxy.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def node(tag, cost):
    return dict(tag=tag, cost=cost, label=tag)


class TierTests(unittest.TestCase):
    def test_region_exclusions(self):
        for label in ['🇭🇰测试', 'HK GO ×0.1', 'TaiWan GO', '台湾', '澳門', 'Macau', 'Hong Kong']:
            with self.subTest(label=label):
                self.assertTrue(MODULE.EXCLUDED.search(label))
        for label in ['🇸🇬SG GO ×0.1', '🇲🇾MY', '日本', 'Australia']:
            self.assertFalse(MODULE.EXCLUDED.search(label))

    def test_rates(self):
        for label, expected in [('SG ×0.05', '0.05'), ('SG ×0.86', '0.86'), ('test', '1'),
                                ('日本+3', '3'), ('GO ×1.22(NEW)', '1.22')]:
            self.assertEqual(MODULE.multiplier(label), Decimal(expected))

    def test_filter_sort_metadata(self):
        sections = {
            'one': dict(section_type='node', label='SG ×0.5', type='anytls', port='443'),
            'two': dict(section_type='node', label='JP ×0.1', type='vless', port='443'),
            'three': dict(section_type='node', label='HK ×0.01', type='vless', port='443'),
            'meta': dict(section_type='node', label='剩余流量 ×0.01', type='anytls', port='9999'),
        }
        self.assertEqual([item['identity'] for item in MODULE.eligible_nodes(sections)], ['two', 'one'])

    def test_price_precedes_latency(self):
        nodes = [node('cheap', '0.1'), node('fast', '1')]
        self.assertEqual(MODULE.select_node(nodes, {'cheap': 900, 'fast': 20}, {})['tag'], 'cheap')

    def test_failover_and_no_direct_fallback(self):
        nodes = [node('cheap', '0.1'), node('backup', '1')]
        self.assertEqual(MODULE.select_node(nodes, {'backup': 100}, {'selected': 'cheap'})['tag'], 'backup')
        self.assertIsNone(MODULE.select_node(nodes, {}, {'selected': 'cheap'}))

    def test_recovery_requires_two_successes(self):
        nodes = [node('cheap', '0.1'), node('backup', '1')]
        previous = dict(selected='backup', streaks={'cheap': 1})
        self.assertEqual(MODULE.select_node(nodes, {'cheap': 900, 'backup': 10}, previous)['tag'], 'backup')
        previous['streaks']['cheap'] = 2
        self.assertEqual(MODULE.select_node(nodes, {'cheap': 900, 'backup': 10}, previous)['tag'], 'cheap')

    def test_same_price_tolerance(self):
        nodes = [node('current', '1'), node('other', '1')]
        previous = dict(selected='current', streaks={'other': 2})
        self.assertEqual(MODULE.select_node(nodes, {'current': 100, 'other': 70}, previous)['tag'], 'current')
        self.assertEqual(MODULE.select_node(nodes, {'current': 200, 'other': 70}, previous)['tag'], 'other')

    def test_generated_configuration_preserves_routes(self):
        config = dict(outbounds=[dict(tag='direct-out', type='direct'),
                                 dict(tag='main-out', type='urltest'), dict(tag='cheap', type='anytls')],
                      route={'final': 'main-out'}, experimental={'cache_file': {'enabled': True}})
        routes = copy.deepcopy(config['route'])
        patched = MODULE.patch_config(config, [node('cheap', '0.05')], 'test-only-secret', 19099)
        self.assertEqual(patched['route']['final'], routes['final'])
        self.assertEqual(patched['route']['rules'][0]['outbound'], 'cheap')
        self.assertEqual(patched['experimental']['clash_api']['external_controller'], '127.0.0.1:19099')
        main = next(item for item in patched['outbounds'] if item['tag'] == 'main-out')
        self.assertFalse(main['interrupt_exist_connections'])
        self.assertNotIn('direct-out', main['outbounds'])

    def test_stale_pool_and_existing_controller_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.patch_config({'outbounds': [{'tag': 'main-out'}]}, [node('missing', '1')], '', 1)
        with self.assertRaises(ValueError):
            MODULE.patch_config({'outbounds': [{'tag': 'main-out'}, {'tag': 'present'}],
                                 'experimental': {'clash_api': {'external_controller': 'existing'}}},
                                [node('present', '1')], '', 1)

    def test_prepare_handles_missing_list_and_avoids_flash_writes(self):
        nodes = [dict(identity='first')]
        commands = MODULE.prepare_commands({}, nodes)
        self.assertNotIn('delete homeproxy.config.main_urltest_nodes', commands)
        self.assertIn('add_list homeproxy.config.main_urltest_nodes=first', commands)
        self.assertEqual(MODULE.prepare_commands(dict(main_node='urltest', main_udp_node='same',
                                                     main_urltest_nodes='first'), nodes), [])

    def test_bootstrap_and_multiwan_settings_survive_tier_patch(self):
        nodes = [node('backup', '1'), node('cheap', '0.1')]
        config = dict(outbounds=[dict(tag='main-out', type='urltest'),
                                 dict(tag='cheap', type='anytls', routing_mark=0x4200),
                                 dict(tag='backup', type='vless', routing_mark=0x4200)],
                      route=dict(auto_detect_interface=False))
        patched = MODULE.patch_config(config, nodes, 'test-only-secret', 19099, 'backup')
        main = next(item for item in patched['outbounds'] if item['tag'] == 'main-out')
        self.assertEqual(main['outbounds'], ['lotus-rate-0_1', 'lotus-rate-1'])
        self.assertEqual(main['default'], 'lotus-rate-1')
        self.assertFalse(patched['route']['auto_detect_interface'])
        self.assertEqual(next(item for item in patched['outbounds'] if item['tag'] == 'cheap')['routing_mark'], 0x4200)

    def test_reconcile_selects_real_groups_after_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = base / 'state.json'
            (base / 'private.json').write_text(json.dumps(dict(port=19099, secret='test-only', probe_url='https://example.org')))
            (base / 'pool.json').write_text(json.dumps(dict(nodes=[node('cheap', '0.1'), node('backup', '1')])))
            state.write_text(json.dumps(dict(selected='backup', streaks={'cheap': 1})))
            changes = []

            def api(settings, path, method='GET', payload=None):
                if method == 'PUT':
                    changes.append((path, payload['name']))
                    return {}
                if path == '/proxies':
                    return dict(proxies={'main-out': dict(type='Selector', now='lotus-rate-1'),
                                         'lotus-rate-1': dict(now='backup')})
                return dict(delay=500 if '/cheap/' in path else 10)

            sections = {'config': dict(lotus_tiers_enabled='1', main_node='urltest')}
            with patch.object(MODULE, 'BASE', base), patch.object(MODULE, 'STATE', state), \
                    patch.object(MODULE, 'read_uci', return_value=sections), patch.object(MODULE, 'api', side_effect=api), \
                    patch.object(MODULE, 'probe_node', side_effect=lambda item, settings, context:
                                 (item['tag'], dict(delay=500 if item['tag'] == 'cheap' else 10, reason=None))):
                MODULE.reconcile()
            self.assertEqual(changes, [('/proxies/lotus-rate-0_1', 'cheap'),
                                       ('/proxies/main-out', 'lotus-rate-0_1')])
            self.assertEqual(json.loads(state.read_text())['selected'], 'cheap')

    def test_chatgpt_criterion_rejects_errors_and_challenges(self):
        headers = {'content-type': 'text/html'}
        valid = b'<html><title>ChatGPT</title><meta content="https://chatgpt.com"></html>'
        self.assertIsNone(MODULE.chatgpt_response_reason(200, headers, valid))
        self.assertEqual(MODULE.chatgpt_response_reason(403, headers, valid), 'HTTP_403')
        self.assertEqual(MODULE.chatgpt_response_reason(200, headers,
                         b'<html><title>Just a moment...</title>chatgpt.com</html>'), 'CHALLENGE')
        self.assertEqual(MODULE.chatgpt_response_reason(200, headers,
                         b'<html>chatgpt.com unsupported_country_region_territory</html>'), 'REGION_OR_ACCESS_DENIED')
        self.assertEqual(MODULE.chatgpt_response_reason(200, headers, b'<html>some other site</html>'), 'NOT_CHATGPT_PAGE')

    def test_probes_are_loopback_only_and_force_each_leaf(self):
        config = dict(outbounds=[dict(tag='main-out', type='urltest'), dict(tag='cheap', type='anytls')])
        patched = MODULE.patch_config(config, [node('cheap', '.1')], 'test-only', 19099)
        self.assertEqual(patched['inbounds'][0]['listen'], '127.0.0.1')
        self.assertEqual(patched['route']['rules'][0]['inbound'], ['lotus-probe-0'])
        with self.assertRaises(ValueError):
            MODULE.patch_config(config, [node('cheap', '.1')], 'test-only', 19099, probe_base_port=65536)


if __name__ == '__main__':
    unittest.main()
