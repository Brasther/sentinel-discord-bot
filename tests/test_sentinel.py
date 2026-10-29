import asyncio
import json
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

import numpy as np
from scipy.signal import sosfilt

from sentinel_core import AudioFilter, ConfigStore, DEFAULT_CONFIG, SpeechDetector, Telemetry, score_audio
import main as server


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'data.json'
        self.store = ConfigStore(self.path)
        self.addCleanup(self.store.close)

    def test_invalid_updates_do_not_mutate_or_persist(self):
        for key, value in [('STATS', {}), ('WHITELIST', []), ('MUTE_TIME', -1),
                           ('UMBRAL_RMS', True), ('CASTIGO_MAX', '50'), ('AUTO_CASTIGOS', 1)]:
            with self.assertRaises(ValueError):
                self.store.update(key, value)
        self.assertEqual(self.store.data, DEFAULT_CONFIG)
        self.assertFalse(self.path.exists())

    def test_atomic_persistence_roundtrip(self):
        self.store.update('MUTE_TIME', 25)
        self.store.whitelist('1466904291313975448', True)
        reloaded = ConfigStore(self.path)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.data['MUTE_TIME'], 25)
        self.assertEqual(reloaded.snapshot()['WHITELIST'], ['1466904291313975448'])
        self.assertFalse(self.path.with_suffix('.json.tmp').exists())

    def test_failed_save_rolls_back(self):
        with patch.object(self.store.database, 'transaction', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.store.update('MUTE_TIME', 25)
            with self.assertRaises(OSError):
                self.store.whitelist('1466904291313975448', True)
        self.assertEqual(self.store.data['MUTE_TIME'], 10)
        self.assertEqual(self.store.data['WHITELIST'], [])

    def test_corrupt_database_is_preserved(self):
        self.path.write_text('bad json')
        store = ConfigStore(self.path)
        self.addCleanup(store.close)
        self.assertIsNotNone(store.warning)
        self.assertEqual(store.data, DEFAULT_CONFIG)
        self.assertEqual(self.path.read_text(), 'bad json')

    def test_legacy_data_and_string_ids_load(self):
        self.path.write_text(json.dumps({'WHITELIST': [123456789012345678], 'STATS': {'123456789012345678': 7}}))
        store = ConfigStore(self.path)
        self.addCleanup(store.close)
        self.assertEqual(store.snapshot()['WHITELIST'], ['123456789012345678'])
        self.assertEqual(store.data['STATS']['123456789012345678'], 7)
        self.assertTrue(store.data['AUTO_CASTIGOS'])


class AudioTests(unittest.TestCase):
    def test_long_speech_never_becomes_noise(self):
        points, continuous = 40, 200
        for _ in range(1000):
            points, continuous, status = score_audio(2500, True, points, continuous, DEFAULT_CONFIG)
            self.assertEqual(status, 'VOZ')
        self.assertEqual((points, continuous), (0, 0))

    def test_noise_and_silence_scoring(self):
        self.assertEqual(score_audio(2000, False, 0, 0, DEFAULT_CONFIG), (4, 1, 'RUIDO'))
        self.assertEqual(score_audio(2000, False, 40, 99, DEFAULT_CONFIG), (44, 100, 'RUIDO'))
        self.assertEqual(score_audio(2000, False, 40, 100, DEFAULT_CONFIG), (45, 101, 'RUIDO CONTINUO'))
        self.assertEqual(score_audio(2000, True, 40, 100, DEFAULT_CONFIG), (39, 0, 'VOZ'))
        self.assertEqual(score_audio(2000, True, 0, 100, DEFAULT_CONFIG), (0, 0, 'VOZ'))
        self.assertEqual(score_audio(0, False, 3, 100, DEFAULT_CONFIG), (0, 0, 'SILENCIO'))
        self.assertEqual(score_audio(2000, None, 3, 100, DEFAULT_CONFIG), (3, 100, 'ANALIZANDO'))

    def test_vad_buffers_fragments_and_keeps_users_independent(self):
        first, second = SpeechDetector(), SpeechDetector()
        self.assertIsNone(first.is_speech(b'\0' * 400))
        self.assertFalse(second.is_speech(b'\0' * 960))
        self.assertFalse(first.is_speech(b'\0' * 560))
        self.assertEqual(first.pending, b'')
        self.assertFalse(first.is_speech(b'\0' * 3840))

    def test_filter_clips_instead_of_wrapping_int16(self):
        rng = np.random.default_rng(42)
        samples = rng.choice([-32768, 32767], size=9600).astype('<i2')
        filt = AudioFilter()
        raw, _ = sosfilt(filt.sos, samples, zi=np.zeros_like(filt.zi))
        result = np.frombuffer(filt.process(samples.tobytes()), dtype='<i2')
        np.testing.assert_array_equal(result, np.clip(raw, -32768, 32767).astype('<i2'))
        self.assertTrue(np.isfinite(raw).all())

    def test_bandpass_reduces_out_of_band_energy(self):
        t = np.arange(48000) / 48000
        def amplitude(frequency):
            samples = (5000 * np.sin(2 * np.pi * frequency * t)).astype('<i2')
            filtered = np.frombuffer(AudioFilter().process(samples.tobytes()), dtype='<i2')
            return np.sqrt(np.mean(filtered[4800:].astype(float) ** 2))
        reference = amplitude(1000)
        self.assertLess(amplitude(60), reference * 0.05)
        self.assertLess(amplitude(10000), reference * 0.05)

    def test_silent_users_do_not_keep_stale_volume(self):
        telemetry = Telemetry()
        user = SimpleNamespace(id=1466904291313975448, name='Alex')
        with patch('sentinel_core.time.time', return_value=100):
            telemetry.audio(user, 4000, 'RUIDO', 20, 50)
        with patch('sentinel_core.time.time', return_value=103):
            result = telemetry.snapshot()
        self.assertEqual(result['users'][0]['id'], '1466904291313975448')
        self.assertEqual(result['users'][0]['rms'], 0)
        self.assertEqual(result['users'][0]['status'], 'SILENCIO')


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = ConfigStore(Path(directory.name) / 'sentinel.db')
        self.addCleanup(store.close)
        for name, value in [('STORE', store), ('CONFIG', store.data), ('TELEMETRY', Telemetry(store.database)),
                            ('COOLDOWN_CASTIGOS', {}), ('CASTIGOS_ACTIVOS', set()), ('PREVIEW', False)]:
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.http = server.app.test_client()
        self.socket = server.socketio.test_client(server.app)
        self.addCleanup(self.socket.disconnect)

    def test_dashboard_and_assets_load(self):
        page = self.http.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="connect-voice"', page.data)
        for path in ['/static/dashboard.css', '/static/dashboard.js', '/static/vendor/socket.io.min.js']:
            with self.http.get(path) as response:
                self.assertEqual(response.status_code, 200)
        snapshot = self.http.get('/api/status').get_json()
        self.assertFalse(snapshot['connected'])
        self.assertNotIn('TOKEN', snapshot)

    def test_socket_settings_validate_and_acknowledge(self):
        reply = self.socket.emit('update_config', {'key': 'MUTE_TIME', 'value': 25}, callback=True)
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['config']['MUTE_TIME'], 25)
        reply = self.socket.emit('update_config', {'key': 'MUTE_TIME', 'value': 0}, callback=True)
        self.assertFalse(reply['ok'])
        self.assertEqual(server.CONFIG['MUTE_TIME'], 25)
        self.assertFalse(self.socket.emit('update_config', ['bad'], callback=True)['ok'])

    def test_auto_rejoin_voice_setting_can_be_changed_from_dashboard(self):
        reply = self.socket.emit('update_config', {'key': 'AUTO_REENTRAR_VOZ', 'value': False}, callback=True)
        self.assertTrue(reply['ok'])
        self.assertFalse(reply['config']['AUTO_REENTRAR_VOZ'])
        invalid = self.socket.emit('update_config', {'key': 'AUTO_REENTRAR_VOZ', 'value': 'false'}, callback=True)
        self.assertFalse(invalid['ok'])

    def test_manual_connect_is_unavailable_in_preview(self):
        with patch.object(server, 'PREVIEW', True):
            reply = self.socket.emit('connect_voice', {}, callback=True)
        self.assertFalse(reply['ok'])
        self.assertIn('demostración', reply['error'])

    def test_whitelist_uses_exact_string_ids(self):
        reply = self.socket.emit('set_whitelist', {'id': '1466904291313975448', 'enabled': True}, callback=True)
        self.assertEqual(reply['config']['WHITELIST'], ['1466904291313975448'])
        self.assertFalse(self.socket.emit('set_whitelist', {'id': 1466904291313975448, 'enabled': True}, callback=True)['ok'])

    def test_socket_individual_sensitivity_persists_and_rejects_invalid_values(self):
        reply = self.socket.emit('set_user_sensitivity', {'id': '1466904291313975448', 'value': 75}, callback=True)
        self.assertTrue(reply['ok'])
        self.assertEqual(server.STORE.sensitivity('1466904291313975448'), 75)
        invalid = self.socket.emit('set_user_sensitivity', {'id': '1466904291313975448', 'value': 101}, callback=True)
        self.assertFalse(invalid['ok'])

    def test_calibration_without_voice_does_not_change_threshold(self):
        reply = self.socket.emit('start_calibration', {}, callback=True)
        self.assertFalse(reply['ok'])
        self.assertEqual(server.CONFIG['UMBRAL_RMS'], 1000)

    def test_chaos_requires_explicit_same_origin_action(self):
        self.assertEqual(self.http.post('/simulate_attack', json={'type': 'kick'}).status_code, 403)
        self.assertEqual(self.http.post('/simulate_attack', headers={'X-Sentinel-Action': 'chaos'}, json={'type': 'invalid'}).status_code, 400)
        self.assertEqual(self.http.post('/simulate_attack', headers={'X-Sentinel-Action': 'chaos'}, json={'type': 'kick'}).status_code, 409)

    def test_sink_cooldown_and_observation_prevent_duplicate_punishments(self):
        sink = server.VigilanteRuido(server.bot, server.CANAL_LOGS_ID)
        user = SimpleNamespace(id=1466904291313975448, name='Alex')
        samples = np.rint(2000 * np.sqrt(2) * np.sin(2 * np.pi * 1000 * np.arange(960) / 48000)).astype('<i2')
        packet = SimpleNamespace(pcm=np.repeat(samples, 2).tobytes())
        audio_settings = server.STORE.audio_settings(str(user.id))
        audio_settings['grace_seconds'] = 0
        submitted = []
        def submit(coroutine):
            submitted.append(coroutine)
            coroutine.close()
            server.CASTIGOS_ACTIVOS.discard(user.id)
        with patch.object(server, 'safe_submit', side_effect=submit), patch('sentinel_audio.SpeechDetector.is_speech', return_value=False), patch.object(server.time, 'time', return_value=100), patch.dict(server.CONFIG, {'AUTO_SENSITIVITY': False}), patch.object(server.STORE, 'audio_settings', return_value=audio_settings):
            for _ in range(100):
                sink.write(user, packet)
            self.assertEqual(len(submitted), 1)
            server.CONFIG['AUTO_CASTIGOS'] = False
            server.COOLDOWN_CASTIGOS.clear()
            for _ in range(100):
                sink.write(user, packet)
            self.assertEqual(len(submitted), 1)
        sink.cleanup()

    def test_calibration_updates_only_with_enough_samples(self):
        def fake_sleep(seconds):
            server.BUFFER_CALIBRACION.append(120)
        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()
        with patch.object(server, 'PREVIEW', True), patch.object(server, 'Thread', ImmediateThread), patch.object(server.time, 'sleep', side_effect=fake_sleep):
            reply = self.socket.emit('start_calibration', {}, callback=True)
        self.assertTrue(reply['ok'])
        self.assertEqual(server.CONFIG['UMBRAL_RMS'], 192)
        self.assertFalse(server.CALIBRANDO)

    def test_calibration_keeps_separate_backgrounds_per_user(self):
        def fake_sleep(seconds):
            server.BUFFER_CALIBRACION.append(120)
            server.CALIBRATION_USERS.setdefault(1, []).append(5)
            server.CALIBRATION_USERS.setdefault(2, []).append(120)
        class ImmediateThread:
            def __init__(self, target, daemon): self.target = target
            def start(self): self.target()
        with patch.object(server, 'PREVIEW', True), patch.object(server, 'Thread', ImmediateThread), patch.object(server.time, 'sleep', side_effect=fake_sleep), patch.object(server, 'CALIBRATION_USERS', {}), patch.object(server, 'CALIBRATION_BASELINES', {}):
            reply = self.socket.emit('start_calibration', {}, callback=True)
            self.assertTrue(reply['ok'])
            self.assertEqual(server.CALIBRATION_BASELINES, {1: 5, 2: 120})
            sink = server.VigilanteRuido(server.bot, server.CANAL_LOGS_ID)
            for identity in (1, 2):
                sink.write(SimpleNamespace(id=identity, name='Test'), SimpleNamespace(pcm=b'\0'*4))
            self.assertEqual(sink.engines[1].floor, 5)
            self.assertEqual(sink.engines[2].floor, 120)
            sink.cleanup()


class PunishmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_still_unmutes_and_releases_active_lock(self):
        member = SimpleNamespace(id=123, display_name='Test', mention='<@123>',
            voice=SimpleNamespace(mute=False, channel=SimpleNamespace(id=456)),
            guild=SimpleNamespace(id=789, voice_channels=[]), edit=AsyncMock(), move_to=AsyncMock())
        sink = server.VigilanteRuido(server.bot, server.CANAL_LOGS_ID)
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / 'sentinel.db')
            try:
                with patch.object(server, 'MUTE_JOURNAL_LOCK', asyncio.Lock()), \
                     patch.object(server, 'MUTE_TASKS', {}), \
                     patch.object(server, 'STORE', store), patch.object(server, 'CONFIG', store.data), \
                     patch.object(server, 'CASTIGOS_ACTIVOS', {123}), \
                     patch.object(store, 'increment_stat', side_effect=sqlite3.OperationalError('disk')), \
                     patch.object(server, 'log_event'):
                    await sink.castigar(member)
                    self.assertEqual(member.edit.await_args_list[0].kwargs['mute'], True)
                    self.assertEqual(member.edit.await_args_list[-1].kwargs['mute'], False)
                    self.assertNotIn(123, server.CASTIGOS_ACTIVOS)
                    self.assertEqual(store.database.pending_mutes(), [])
            finally:
                store.close()
        sink.cleanup()

    async def test_already_muted_member_is_not_unmuted_by_sentinel(self):
        member = SimpleNamespace(id=123, voice=SimpleNamespace(mute=True), edit=AsyncMock())
        sink = server.VigilanteRuido(server.bot, server.CANAL_LOGS_ID)
        with patch.object(server, 'CASTIGOS_ACTIVOS', {123}):
            await sink.castigar(member)
            member.edit.assert_not_awaited()
            self.assertNotIn(123, server.CASTIGOS_ACTIVOS)
        sink.cleanup()


if __name__ == '__main__':
    unittest.main()

