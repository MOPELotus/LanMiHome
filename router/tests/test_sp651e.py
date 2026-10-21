import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sp651e import SP651ETransport, control_frames, decode_status, frame

STATUS = bytes.fromhex(
    "530200010053000111000756333032302e3037010303003c0001023d00010078060101"
    "ffffff0000ff000a1e011000ff0000ffff010706ff00000600ff00060000ff000000"
    "0000000000000000000000000003040104020403"
)


class CodecTests(unittest.TestCase):
    def test_real_unbound_controller_status(self):
        state = decode_status(STATUS)
        self.assertEqual(state['firmware'], 'V3020.07')
        self.assertTrue(state['power'])
        self.assertEqual(state['brightness'], 100)
        self.assertEqual(state['mode'], 6)

    def test_query_and_power_frames(self):
        self.assertEqual(frame(2, bytes([1])).hex(), '53020001000101')
        self.assertEqual(control_frames({'power': False}, decode_status(STATUS))[0].hex(), '53500001000100')

    def test_music_mode_requires_static_white_before_dimming(self):
        commands = control_frames({'brightness': 50}, decode_status(STATUS))
        self.assertEqual(commands, [frame(0x53, bytes([2, 1])), frame(0x51, bytes([1, 128]))])
        self.assertFalse(any(command[1] == 0x50 for command in commands))

    def test_static_dimming_does_not_resend_mode(self):
        state = dict(decode_status(STATUS), mode=2)
        self.assertEqual(control_frames({'brightness': 100}, state), [frame(0x51, bytes([1, 255]))])

    def test_all_percentages_roundtrip(self):
        for percent in range(1, 101):
            raw = bytearray(STATUS)
            raw[36] = round(percent * 255 / 100)
            self.assertEqual(decode_status(bytes(raw))['brightness'], percent)

    def test_bad_or_unverified_status_rejected(self):
        bad_frames = [b'', STATUS[:-1], bytes([0]) + STATUS[1:]]
        for offset, value in [(2, 1), (3, 2), (5, 0), (11, 88), (19, 2), (29, 2), (36, 0)]:
            raw = bytearray(STATUS)
            raw[offset] = value
            bad_frames.append(bytes(raw))
        for raw in bad_frames:
            with self.subTest(raw=raw.hex()), self.assertRaises(ValueError):
                decode_status(raw)


class RuntimeConfigurationTests(unittest.TestCase):
    def test_configured_strip_uses_existing_coordinator(self):
        import lanmihome_coordinated as coordinator
        app = coordinator.core.LanMiHomeApp({'light_strip': {
            'enabled': True, 'address': 'AA:BB:CC:DD:EE:FF', 'protocol': 'banlanx-sp651e',
        }})
        self.assertTrue(app.ble.needed)
        self.assertIsInstance(app.light_strip._transport, SP651ETransport)
        self.assertIsNone(app.ble.thread)

    def test_absent_strip_does_not_start_bluetooth(self):
        import lanmihome_coordinated as coordinator
        app = coordinator.core.LanMiHomeApp({})
        self.assertFalse(app.ble.needed)
        self.assertFalse(app.light_strip.capabilities()['protocol_ready'])

    def test_unknown_protocol_is_not_attached(self):
        import lanmihome_coordinated as coordinator
        app = coordinator.core.LanMiHomeApp({'light_strip': {
            'enabled': True, 'address': 'AA:BB:CC:DD:EE:FF', 'protocol': 'unknown',
        }})
        self.assertFalse(app.ble.needed)
        self.assertEqual(app.light_strip.state()['status'], 'protocol_pending')


class FakeClient:
    instances = []
    fail_connect = False
    ignore_write = False
    disconnect_error = False
    initial = STATUS

    def __init__(self, device, timeout):
        self.device = device
        self.status = bytearray(self.initial)
        self.commands = []
        self.disconnected = False
        self.is_connected = False
        self.instances.append(self)

    async def connect(self, **kwargs):
        if self.fail_connect:
            raise ConnectionError('test connection failure')
        self.is_connected = True

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def write_gatt_char(self, uuid, data, response):
        self.commands.append(data)
        command = data[1]
        if command == 2:
            self.callback(0, bytes(self.status))
        elif not self.ignore_write:
            if command == 0x50:
                self.status[29] = data[6]
            elif command == 0x53:
                self.status[32:34] = data[6:8]
            elif command == 0x51 and self.status[32] == 2:
                self.status[36] = data[7]

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False
        if self.disconnect_error:
            raise OSError('test disconnect failure')


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.instances = []
        FakeClient.fail_connect = False
        FakeClient.ignore_write = False
        FakeClient.disconnect_error = False
        FakeClient.initial = STATUS
        self.gate = []
        self.device = SimpleNamespace(name='SP651E')

        async def resolve(address):
            return self.device

        async def acquire():
            self.gate.append('acquired')
            return {'token': 'test-lease'}

        async def release(token):
            self.assertEqual(token, 'test-lease')
            self.gate.append('released')

        runtime = SimpleNamespace(loop=asyncio.get_running_loop(), _resolve_device=resolve)
        self.transport = SP651ETransport(runtime, 'AA:BB:CC:DD:EE:FF', acquire, release)
        self.mock = patch('sp651e.BleakClient', FakeClient)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    async def asyncTearDown(self):
        await self.transport._shutdown()

    async def test_read_never_sends_control_or_pairing(self):
        state = await self.transport._operate(None)
        self.assertTrue(state['power'])
        self.assertEqual([command[1] for command in FakeClient.instances[0].commands], [2])
        self.assertFalse(FakeClient.instances[0].disconnected)
        self.assertEqual(self.gate, ['acquired', 'released'])

    async def test_dimming_is_confirmed_in_static_mode(self):
        state = await self.transport._operate({'brightness': 50})
        self.assertEqual(state['brightness'], 50)
        self.assertEqual(state['mode'], 2)
        self.assertTrue(state['power'])

    async def test_dimming_preserves_power_off(self):
        raw = bytearray(STATUS)
        raw[29] = 0
        FakeClient.initial = bytes(raw)
        state = await self.transport._operate({'brightness': 25})
        self.assertFalse(state['power'])
        self.assertEqual(state['brightness'], 25)

    async def test_power_write_is_confirmed(self):
        state = await self.transport._operate({'power': False})
        self.assertFalse(state['power'])

    async def test_ignored_command_is_rejected(self):
        FakeClient.ignore_write = True
        with self.assertRaisesRegex(ConnectionError, 'not confirmed'):
            await self.transport._operate({'power': False})
        self.assertEqual(self.gate[-1], 'released')

    async def test_connect_failure_releases_gate(self):
        FakeClient.fail_connect = True
        with self.assertRaises(ConnectionError):
            await self.transport._operate(None)
        self.assertEqual(self.gate, ['acquired', 'released'])

    async def test_disconnect_failure_still_releases_gate(self):
        FakeClient.disconnect_error = True
        FakeClient.ignore_write = True
        with self.assertRaises(OSError):
            await self.transport._operate({'power': False})
        self.assertEqual(self.gate[-1], 'released')

    async def test_wrong_device_never_acquires_or_connects(self):
        self.device.name = 'other-light'
        with self.assertRaises(ConnectionError):
            await self.transport._operate(None)
        self.assertEqual(self.gate, [])
        self.assertEqual(FakeClient.instances, [])

    async def test_unverified_firmware_never_receives_control(self):
        raw = bytearray(STATUS)
        raw[11] = 88
        FakeClient.initial = bytes(raw)
        with self.assertRaises(ValueError):
            await self.transport._operate({'power': False})
        self.assertEqual([command[1] for command in FakeClient.instances[0].commands], [2])

    async def test_sync_read_uses_shared_loop_and_cache(self):
        await self.transport._poll_once()
        first = await asyncio.to_thread(self.transport.read_state)
        second = await asyncio.to_thread(self.transport.read_state)
        self.assertEqual(first, second)
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(self.transport.firmware, 'V3020.07')

    async def test_sync_write_invalidates_old_cache(self):
        await self.transport._poll_once()
        await asyncio.to_thread(self.transport.set_state, {'power': False})
        self.assertFalse((await asyncio.to_thread(self.transport.read_state))['power'])
        self.assertEqual(len(FakeClient.instances), 1)

    async def test_get_before_background_report_does_not_connect(self):
        with self.assertRaises(ConnectionError):
            self.transport.read_state()
        self.assertEqual(FakeClient.instances, [])

    async def test_repeated_polls_reuse_connection_and_release_setup_lock(self):
        await self.transport._poll_once()
        await self.transport._poll_once()
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(self.gate, ['acquired', 'released'])

    async def test_remote_notification_updates_cached_state(self):
        await self.transport._poll_once()
        changed = bytearray(STATUS)
        changed[29] = 0
        FakeClient.instances[0].callback(0, bytes(changed))
        self.assertFalse(self.transport.read_state()['power'])

    async def test_shutdown_disconnects_persistent_session(self):
        await self.transport._poll_once()
        await self.transport._shutdown()
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_background_poll_start_is_idempotent(self):
        self.transport.start()
        await asyncio.sleep(0.01)
        worker = self.transport._worker
        self.transport.start()
        await asyncio.sleep(0.01)
        self.assertIs(worker, self.transport._worker)
        self.assertTrue(self.transport.read_state()['power'])

    async def test_dropped_session_reconnects_on_next_poll(self):
        await self.transport._poll_once()
        FakeClient.instances[0].is_connected = False
        await self.transport._poll_once()
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertEqual(self.gate, ['acquired', 'released', 'acquired', 'released'])


if __name__ == '__main__':
    unittest.main()
