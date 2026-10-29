from collections import OrderedDict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from discord.opus import OpusError
from discord.ext.voice_recv import opus
import main
from sentinel_runtime import acquire_instance
from voice_compat import DavePacketDecoder, DroppedAudio


class RuntimeTests(unittest.TestCase):
    def test_only_one_instance_and_release_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'instance.lock'
            first = acquire_instance(path)
            try:
                with self.assertRaises(RuntimeError):
                    acquire_instance(path)
            finally:
                first.close()
            second = acquire_instance(path)
            second.close()

    def test_http_socket_not_inherited_during_nuclear_restart(self):
        server = Mock()
        with patch.dict('os.environ', {'SENTINEL_HOST': '127.0.0.1', 'SENTINEL_PORT': '0'}), \
             patch.object(main, 'make_server', return_value=server) as make_server:
            self.assertIs(main.create_http_server(), server)
        make_server.assert_called_once_with('127.0.0.1', 0, main.app, threaded=True)
        server.socket.set_inheritable.assert_called_once_with(False)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def run_one_check(self, connected=True, closed=False, heartbeat_age=35):
        ws = SimpleNamespace(is_closed=closed, latency=.05,
                             _keep_alive=SimpleNamespace(_last_ack=99))
        vc = SimpleNamespace(ws=ws, is_connected=Mock(return_value=connected),
                             channel=SimpleNamespace(members=[object(), object()]))
        bot = SimpleNamespace(voice_clients=[vc], wait_until_ready=AsyncMock(),
                              is_closed=Mock(side_effect=[False, True]))
        with patch.object(main, 'bot', bot), \
             patch.object(main, 'ULTIMO_LATIDO', 100 - heartbeat_age), \
             patch.object(main, 'time', SimpleNamespace(time=lambda: 100, perf_counter=lambda: 100)), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock) as sleep, \
             patch.object(main.os, 'execv') as reboot, patch.object(main, 'log_event'):
            await main.watchdog()
            bot.wait_until_ready.assert_awaited_once_with()
            sleep.assert_awaited_once_with(10)
            return main.ULTIMO_LATIDO, reboot

    async def test_dtx_open_websocket_renews_heartbeat_without_restart(self):
        heartbeat, reboot = await self.run_one_check(heartbeat_age=35)
        self.assertEqual(heartbeat, 100)
        reboot.assert_not_called()

    async def test_closed_or_disconnected_signalling_restarts_after_thirty_seconds(self):
        for connected, closed in [(True, True), (False, False)]:
            with self.subTest(connected=connected, closed=closed):
                heartbeat, reboot = await self.run_one_check(connected=connected, closed=closed)
                self.assertEqual(heartbeat, 65)
                reboot.assert_called_once_with(main.sys.executable, ['python'] + main.sys.argv)

    async def test_twenty_to_thirty_seconds_do_not_trigger_watchdog(self):
        for age in (20.1, 30):
            with self.subTest(age=age):
                heartbeat, reboot = await self.run_one_check(closed=True, heartbeat_age=age)
                self.assertEqual(heartbeat, 100 - age)
                reboot.assert_not_called()


class VoiceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(ready=True, decrypt=Mock(return_value=b'plain-opus'))
        connection = SimpleNamespace(dave_protocol_version=1, dave_session=self.session)
        self.voice = SimpleNamespace(_connection=connection, _get_id_from_ssrc=lambda ssrc: 123)
        self.decoder = DavePacketDecoder.__new__(DavePacketDecoder)
        self.decoder.router = SimpleNamespace(sink=SimpleNamespace(voice_client=self.voice))
        self.decoder.ssrc = 456
        self.decoder._cached_id = None
        self.decoder._plain_cache = OrderedDict()
        self.decoder._warning_time = -float('inf')
        self.decoder._decoder = Mock()
        self.packet = SimpleNamespace(decrypted_data=b'encrypted', is_silence=lambda: False, sequence=1, timestamp=100)

    def test_dave_decrypts_before_opus_and_fec_cache_avoids_double_decryption(self):
        self.decoder._decode_packet(self.packet)
        self.decoder._decode_packet(self.packet)
        self.session.decrypt.assert_called_once()
        self.assertEqual(self.session.decrypt.call_args.args[0], 123)
        self.assertEqual(self.decoder._decoder.decode.call_args.args[0], b'plain-opus')
        self.assertEqual(self.packet.decrypted_data, b'encrypted')

    def test_pending_dave_never_passes_ciphertext_to_opus(self):
        self.session.ready = False
        with self.assertRaises(DroppedAudio):
            self.decoder._decode_packet(self.packet)
        self.decoder._decoder.decode.assert_not_called()

    def test_failed_decryption_drops_frame_without_unencrypted_fallback(self):
        self.session.decrypt.side_effect = ValueError('decryption failed')
        with self.assertRaises(DroppedAudio):
            self.decoder._decode_packet(self.packet)
        self.decoder._decoder.decode.assert_not_called()

    def test_plain_voice_without_dave_keeps_original_audio(self):
        self.voice._connection.dave_protocol_version = 0
        self.assertEqual(self.decoder._payload(self.packet), b'encrypted')
        self.session.decrypt.assert_not_called()

    def test_corrupt_packet_does_not_kill_receiver_or_emit_fake_pcm(self):
        expected = opus.VoiceData(self.packet, SimpleNamespace(id=123), pcm=b'pcm')
        error = OpusError.__new__(OpusError)
        Exception.__init__(error, 'corrupted stream')
        with patch.object(opus.PacketDecoder, '_process_packet', side_effect=[error, expected]):
            discarded = self.decoder._process_packet(self.packet)
            good = self.decoder._process_packet(self.packet)
        self.assertIsNone(discarded.source)
        self.assertEqual(discarded.pcm, b'')
        self.assertIs(good, expected)
