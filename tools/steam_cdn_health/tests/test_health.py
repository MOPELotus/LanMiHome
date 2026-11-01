import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


try:
    import fcntl
except ImportError:
    sys.modules['fcntl'] = types.SimpleNamespace()

SPEC = importlib.util.spec_from_file_location('cdn_health', Path(__file__).parents[1] / 'health.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HealthTests(unittest.TestCase):
    def config(self):
        return dict(domain='cdn.example.com', seed_ips=['1.1.1.1'],
                    discovery_url='https://dns.example.com/resolve', interval=60)

    def test_validation_rejects_injection_and_private_candidates(self):
        for domain in ['bad.example/new', 'bad;example.com', '*.example.com', 'a..com']:
            with self.assertRaises(ValueError):
                MODULE.validate_config(dict(self.config(), domain=domain))
        for address in ['127.0.0.1', '10.0.0.1', '::1', 'invalid']:
            with self.assertRaises(ValueError):
                MODULE.validate_config(dict(self.config(), seed_ips=[address]))
        with self.assertRaises(ValueError):
            MODULE.validate_config(dict(self.config(), discovery_url='http://dns.example.com/'))

    def test_valid_config_and_exact_hostname_records(self):
        MODULE.validate_config(self.config())
        self.assertEqual(MODULE.dns_text('cdn.example.com', ['1.1.1.1', '1.1.1.1']),
                         '1.1.1.1 cdn.example.com\n')
        self.assertNotIn('server=', MODULE.dns_text('cdn.example.com', ['1.1.1.1']))

    def test_recovery_requires_two_successes_and_failure_removes_immediately(self):
        results = {'1.1.1.1': dict(ok=True, status=403)}
        first = MODULE.update_health({}, results)
        self.assertFalse(first['1.1.1.1']['healthy'])
        second = MODULE.update_health(first, results)
        self.assertTrue(second['1.1.1.1']['healthy'])
        failed = MODULE.update_health(second, {'1.1.1.1': dict(ok=False)})
        self.assertFalse(failed['1.1.1.1']['healthy'])

    def test_probe_keeps_hostname_without_fetching_signed_content(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 403
        with patch.object(MODULE.http.client, 'HTTPConnection', return_value=connection):
            result = MODULE.probe('cdn.example.com', '1.1.1.1')
        self.assertTrue(result['ok'])
        connection.request.assert_called_once_with('HEAD', '/', headers={
            'Host': 'cdn.example.com', 'Connection': 'close'})
        connection.close.assert_called_once()

    def test_probe_timeout_and_server_failure_are_unhealthy(self):
        connection = MagicMock()
        connection.request.side_effect = TimeoutError()
        with patch.object(MODULE.http.client, 'HTTPConnection', return_value=connection):
            self.assertFalse(MODULE.probe('cdn.example.com', '1.1.1.1')['ok'])
        connection.request.side_effect = None
        connection.getresponse.return_value.status = 503
        with patch.object(MODULE.http.client, 'HTTPConnection', return_value=connection):
            self.assertFalse(MODULE.probe('cdn.example.com', '1.1.1.1')['ok'])

    def test_discovery_excludes_private_and_unrelated_answers(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'Answer': [
            {'type': 1, 'data': '1.1.1.1'}, {'type': 1, 'data': '127.0.0.1'},
            {'type': 5, 'data': 'alias.example.com'}, {'type': 28, 'data': '::1'}]}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(MODULE.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(MODULE.discover(self.config()), ['1.1.1.1'])

    def test_only_our_fragment_is_changed_and_stable_pool_does_not_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / 'other.conf'
            unrelated.write_text('unchanged')
            with patch.object(MODULE, 'RUNTIME', root / 'runtime'), patch.object(MODULE.subprocess, 'run') as runner:
                self.assertTrue(MODULE.publish('cdn.example.com', ['1.1.1.1'], [root]))
                self.assertEqual(runner.call_count, 2)
                self.assertEqual(runner.call_args.args[0][1], 'restart')
                runner.reset_mock()
                self.assertFalse(MODULE.publish('cdn.example.com', ['1.1.1.1'], [root]))
                runner.assert_not_called()
                self.assertTrue(MODULE.publish('cdn.example.com', [], [root]))
                self.assertEqual(runner.call_args.args[0][1], 'reload')
                self.assertEqual((root / 'runtime' / 'hosts').read_text(), '')
                self.assertTrue(MODULE.publish('', [], [root]))
            self.assertFalse((root / MODULE.FRAGMENT).exists())
            self.assertEqual(unrelated.read_text(), 'unchanged')

    def test_invalid_dnsmasq_candidate_does_not_replace_previous_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / MODULE.FRAGMENT
            path.write_text('original')
            with patch.object(MODULE, 'RUNTIME', root / 'runtime'), patch.object(MODULE.subprocess, 'run', side_effect=RuntimeError('test failed')):
                with self.assertRaises(RuntimeError):
                    MODULE.publish('cdn.example.com', ['1.1.1.1'], [root])
            self.assertEqual(path.read_text(), 'original')
            self.assertFalse(path.with_name(path.name + '.new').exists())


if __name__ == '__main__':
    unittest.main()
