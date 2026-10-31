import copy
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest


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
        self.assertEqual(patched['route'], routes)
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


if __name__ == '__main__':
    unittest.main()
