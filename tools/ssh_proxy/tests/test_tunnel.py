import importlib.util
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location('tunnel', Path(__file__).parents[1] / 'tunnel.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TunnelTests(unittest.TestCase):
    def test_command_is_loopback_only_and_keeps_host_verification(self):
        command = MODULE.ssh_arguments(dict(alias='test-server', local_port=27653, remote_port=7890))
        self.assertEqual(command[0], '/usr/bin/ssh')
        self.assertIn('127.0.0.1:27653:127.0.0.1:7890', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('BatchMode=yes', command)
        self.assertIn('IdentitiesOnly=yes', command)
        self.assertIn('ExitOnForwardFailure=yes', command)
        self.assertIn('ServerAliveInterval=20', command)
        self.assertIn('ServerAliveCountMax=3', command)
        self.assertTrue(any(value.startswith('ProxyCommand=/usr/bin/python3 ') for value in command))
        self.assertNotIn('-g', command)
        self.assertEqual(command[-1], 'test-server')

    def test_invalid_alias_and_ports_are_rejected(self):
        settings = dict(alias='test-server', local_port=27653, remote_port=7890)
        for alias in ['', '-Fconfig', 'two aliases', 'a;command', 'a\ncommand', None]:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                MODULE.ssh_arguments(dict(settings, alias=alias))
        for key, value in [('local_port', 80), ('local_port', 65536), ('local_port', True),
                           ('remote_port', 0), ('remote_port', '7890'), ('remote_port', False)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                MODULE.ssh_arguments(dict(settings, **{key: value}))

    def test_socket_mark_precedes_connect_and_failed_address_is_closed(self):
        failed = Mock()
        failed.connect.side_effect = OSError('unavailable')
        connected = Mock()
        timeline = []
        connected.setsockopt.side_effect = lambda *args: timeline.append(('mark', args[-1]))
        connected.connect.side_effect = lambda address: timeline.append(('connect', address))
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, '', ('192.0.2.1', 22)),
                     (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('192.0.2.2', 22))]
        with patch.object(MODULE.socket, 'getaddrinfo', return_value=addresses), \
                patch.object(MODULE.socket, 'socket', side_effect=[failed, connected]):
            self.assertIs(MODULE.connect_marked('test.invalid', 22), connected)
        failed.close.assert_called_once()
        self.assertEqual(timeline, [('mark', 0x4200), ('connect', ('192.0.2.2', 22))])
        connected.settimeout.assert_any_call(None)

    def test_relay_transfers_both_directions_and_propagates_input_eof(self):
        client, server = socket.socketpair()
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        received = []
        errors = []

        def peer():
            try:
                server.settimeout(3)
                while True:
                    chunk = server.recv(65536)
                    if not chunk:
                        break
                    received.append(chunk)
                server.sendall(b'response')
                server.shutdown(socket.SHUT_WR)
            except OSError as error:
                errors.append(error)

        worker = threading.Thread(target=peer)
        worker.start()
        try:
            os.write(input_write, b'request')
            os.close(input_write)
            input_write = None
            client.settimeout(3)
            MODULE.relay_stream(client, input_read, output_write)
            self.assertEqual(os.read(output_read, 8), b'response')
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(b''.join(received), b'request')
            self.assertEqual(errors, [])
        finally:
            client.close()
            server.close()
            for descriptor in [input_read, input_write, output_read, output_write]:
                if descriptor is not None:
                    os.close(descriptor)

    def test_start_stop_priorities_fit_openwrt_disable_patterns(self):
        source = (Path(__file__).parents[1] / 'ssh-proxy.init').read_text()
        self.assertIn('START=98\n', source)
        self.assertIn('STOP=08\n', source)


if __name__ == '__main__':
    unittest.main()
