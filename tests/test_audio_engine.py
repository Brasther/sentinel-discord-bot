from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

import main
from sentinel_audio import AudioEngine, calibrated_threshold, rms_pcm, stereo_to_mono
from sentinel_core import DEFAULT_CONFIG, SpeechDetector, validate_setting


def stereo(samples, inverse=False):
    samples = np.asarray(samples, dtype='<i2')
    return np.column_stack((samples, -samples if inverse else samples)).astype('<i2').tobytes()


def square(level):
    return np.tile(np.array([-level, level], dtype='<i2'), 480)


def tone(level, frequency=1000, frames=1):
    time = np.arange(960 * frames) / 48000
    return np.rint(level * np.sqrt(2) * np.sin(2 * np.pi * frequency * time)).astype('<i2')


class AudioEngineTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(DEFAULT_CONFIG, AUTO_SENSITIVITY=False, UMBRAL_RMS=100)
        self.pcm = stereo(tone(200))

    def engine(self, speech=False):
        engine = AudioEngine()
        engine.vad.is_speech = Mock(return_value=speech)
        return engine

    def test_mono_conversion_preserves_opposite_phase_and_single_channel(self):
        pcm = stereo(square(1000), inverse=True)
        self.assertEqual(rms_pcm(stereo_to_mono(pcm)), 1000)
        pcm = np.column_stack((np.full(960, 10000), square(800))).astype('<i2').tobytes()
        self.assertEqual(rms_pcm(stereo_to_mono(pcm)), 800)

    def test_rms_handles_full_int16_range_without_overflow(self):
        self.assertEqual(rms_pcm(np.full(960, -32768, dtype='<i2').tobytes()), 32768)
        self.assertEqual(rms_pcm(b''), 0)

    def test_incomplete_pcm_does_not_change_engine_state(self):
        engine = self.engine()
        with self.assertRaises(ValueError):
            engine.feed(b'123', self.config)
        self.assertEqual(engine.pending, b'')
        self.assertIsNone(engine.last_input)

    def test_packet_fragmentation_and_batching_produce_identical_scores(self):
        whole, split = self.engine(), self.engine()
        pcm = self.pcm * 80
        expected = whole.feed(pcm, self.config, now=0)
        actual = []
        # 5 ms fragments, not aligned with the 20 ms scoring windows.
        for index in range(0, len(pcm), 960):
            actual.extend(split.feed(pcm[index:index+960], self.config, now=0))
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 80)
        self.assertEqual(split.pending, b'')

    def test_quiet_noise_above_adaptive_floor_can_accumulate(self):
        engine = self.engine()
        config = dict(self.config, AUTO_SENSITIVITY=True, UMBRAL_RMS=1000)
        engine.feed(stereo(np.zeros(960, dtype='<i2')) * 51, config, now=0)
        result = engine.feed(stereo(tone(26)) * 20, config, now=.1)[-1]
        self.assertLess(result['threshold'], 26)
        self.assertEqual(result['rms'], 26)
        self.assertGreater(result['violation'], 50)

    def test_startup_does_not_learn_audible_noise_as_background(self):
        engine = self.engine()
        config = dict(self.config, AUTO_SENSITIVITY=True)
        results = engine.feed(self.pcm * 60, config, now=0)
        self.assertGreater(results[-1]['violation'], 50)
        self.assertLessEqual(results[-1]['threshold'], 10)

    def test_speech_is_protected_until_sustained_audio_limit(self):
        engine = self.engine(speech=True)
        results = engine.feed(self.pcm * 1000, self.config, now=0)
        self.assertEqual(results[100]['status'], 'VOZ')
        self.assertEqual(results[100]['violation'], 0)
        self.assertEqual(results[-1]['status'], 'RUIDO CONTINUO')
        self.assertGreater(results[-1]['violation'], 0)

    def test_brief_vad_dropout_does_not_punish_speech(self):
        engine = self.engine(speech=True)
        engine.feed(self.pcm, self.config, now=0)
        engine.vad.is_speech.return_value = False
        results = engine.feed(self.pcm * 9, self.config, now=.1)
        self.assertTrue(all(item['violation'] == 0 for item in results))

    def test_single_impulse_is_not_an_offense(self):
        engine = self.engine()
        first = engine.feed(self.pcm, self.config, now=0)[0]
        final = engine.feed(stereo(np.zeros(960, dtype='<i2')) * 20, self.config, now=.1)[-1]
        self.assertEqual(first['violation'], 0)
        self.assertEqual(final['violation'], 0)

    def test_repeated_high_crest_impulses_are_confirmed_but_one_is_not(self):
        engine = self.engine()
        config = dict(self.config, UMBRAL_RMS=10, USER_GRACE_SECONDS=0)
        impulse = np.zeros(960, dtype='<i2')
        impulse[120] = 5000
        silence = np.zeros(960, dtype='<i2')
        engine.filter.process = Mock(side_effect=[
            impulse.tobytes(), silence.tobytes(), silence.tobytes(),
            impulse.tobytes(), silence.tobytes(), silence.tobytes(),
            impulse.tobytes(),
        ])
        results = []
        for index in range(7):
            results.extend(engine.feed(self.pcm, config, now=0))
        self.assertEqual(results[0]['status'], 'ANALIZANDO')
        self.assertEqual(results[0]['violation'], 0)
        self.assertEqual(results[-1]['status'], 'RUIDO')
        self.assertIn('repetidos', results[-1]['reason'])
        self.assertGreater(results[-1]['violation'], 0)

    def test_continuous_mode_can_limit_speech_only_after_configured_duration(self):
        engine = self.engine(speech=True)
        config = dict(self.config, AUDIO_MODE='continuous', CONTINUOUS_SECONDS=5)
        results = engine.feed(self.pcm * 265, config, now=0)
        self.assertTrue(all(item['violation'] == 0 for item in results[:249]))
        self.assertEqual(results[-1]['status'], 'AUDIO CONTINUO')
        self.assertGreater(results[-1]['violation'], 50)

    def test_half_second_silence_breaks_continuous_audio(self):
        engine = self.engine(speech=True)
        config = dict(self.config, AUDIO_MODE='continuous', CONTINUOUS_SECONDS=5)
        engine.feed(self.pcm * 200, config, now=0)
        engine.feed(stereo(np.zeros(960, dtype='<i2')) * 25, config, now=.1)
        result = engine.feed(self.pcm * 200, config, now=.2)[-1]
        self.assertEqual(result['violation'], 0)

    def test_dtx_resets_fragment_and_old_points(self):
        engine = self.engine()
        engine.feed(self.pcm * 30 + self.pcm[:960], self.config, now=0)
        self.assertTrue(engine.pending)
        engine.points = 120
        engine.voice_hold = 10
        evidence = list(engine.noise_evidence)
        filt, vad = engine.filter, engine.vad
        # Aislar el drenaje del puntaje que añadiría el nuevo bloque de audio.
        with patch.object(engine, 'analyse', side_effect=lambda frame, config: {'violation': engine.points}):
            result = engine.feed(self.pcm, self.config, now=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['violation'], 110)
        self.assertEqual(engine.points, 110)
        self.assertEqual(engine.pending, b'')
        self.assertEqual(engine.voice_hold, 0)
        self.assertEqual(list(engine.noise_evidence), evidence)
        self.assertIs(engine.filter, filt)
        self.assertIs(engine.vad, vad)

    def test_long_dtx_clears_transient_evidence_and_drains_without_forced_reset(self):
        for initial, expected in [(20, 0), (120, 95)]:
            with self.subTest(initial_points=initial):
                engine = self.engine()
                engine.feed(self.pcm * 30 + self.pcm[:960], self.config, now=0)
                engine.points = initial
                engine.voice_hold = 10
                self.assertGreater(engine.noise_frames, 0)
                self.assertGreater(engine.audible_ms, 0)
                filt, vad = engine.filter, engine.vad
                with patch.object(engine, 'analyse', side_effect=lambda frame, config: {'violation': engine.points}):
                    result = engine.feed(self.pcm, self.config, now=2.5)
                self.assertEqual(result[0]['violation'], expected)
                self.assertEqual(engine.points, expected)
                self.assertEqual(engine.pending, b'')
                self.assertEqual(engine.voice_hold, 0)
                self.assertEqual(list(engine.noise_evidence), [])
                self.assertEqual(engine.noise_frames, 0)
                self.assertEqual(engine.audible_ms, 0)
                self.assertIsNot(engine.filter, filt)
                self.assertIsNot(engine.vad, vad)

    def test_dtx_decay_and_transient_reset_boundaries(self):
        for elapsed, expected, reset in [(.5, 120, False), (.5001, 115, False),
                                        (2.0, 100, False), (2.0001, 100, True), (20, 0, True)]:
            with self.subTest(elapsed=elapsed):
                engine = self.engine()
                engine.feed(self.pcm * 10, self.config, now=0)
                engine.points = 120
                evidence = list(engine.noise_evidence)
                with patch.object(engine, 'analyse', side_effect=lambda frame, config: {'violation': engine.points}):
                    engine.feed(self.pcm, self.config, now=elapsed)
                self.assertEqual(engine.points, expected)
                self.assertEqual(list(engine.noise_evidence), [] if reset else evidence)
                self.assertEqual(engine.pending, b'')

    def test_intermittent_noise_accumulates_across_dtx_gaps(self):
        engine = self.engine()
        scores = []
        for burst in range(5):
            engine.feed(self.pcm * 10, self.config, now=burst * .6)
            scores.append(engine.points)
        self.assertTrue(all(new > old for old, new in zip(scores, scores[1:])))
        self.assertGreater(scores[-1], self.config['CASTIGO_MAX'])

    def test_mode_change_cannot_inherit_an_offense(self):
        engine = self.engine()
        engine.feed(self.pcm * 30, self.config, now=0)
        self.assertGreater(engine.points, 0)
        result = engine.feed(self.pcm, dict(self.config, AUDIO_MODE='continuous'), now=.1)[0]
        self.assertLess(result['violation'], 10)

    def test_clipping_is_reported_without_wrapping(self):
        samples = np.tile(np.array([-32768, 32767], dtype='<i2'), 480)
        result = self.engine().feed(stereo(samples), self.config, now=0)[0]
        self.assertEqual(result['peak'], 32768)
        self.assertEqual(result['clipping'], 1)
        self.assertAlmostEqual(result['dbfs'], 0)

    def test_dc_offset_is_not_counted_as_audible_noise(self):
        result = self.engine().feed(stereo(np.full(960, 20000)) * 40, self.config, now=0)[-1]
        self.assertEqual(result['rms'], 0)
        self.assertEqual(result['violation'], 0)

    def test_calibration_handles_low_levels_and_rejects_invalid_samples(self):
        self.assertEqual(calibrated_threshold([0] * 50), 5)
        self.assertEqual(calibrated_threshold([5] * 50), 10)
        self.assertEqual(calibrated_threshold([120] * 50), 192)
        self.assertEqual(calibrated_threshold([100000] * 50), 10000)
        for values in ([0] * 24, [float('nan')] * 50, [-1] * 50):
            with self.assertRaises(ValueError):
                calibrated_threshold(values)

    def test_calibration_resets_scores_and_adaptive_floor(self):
        engine = self.engine()
        engine.feed(self.pcm * 30, self.config, now=0)
        engine.calibrate(4)
        self.assertEqual(engine.points, 0)
        self.assertEqual(engine.floor, 4)

    def test_configuration_bounds_and_modes(self):
        self.assertEqual(validate_setting('UMBRAL_RMS', 5), 5)
        for key, value in [('CONTINUOUS_SECONDS', 4), ('AUDIO_MODE', []), ('AUTO_SENSITIVITY', 1)]:
            with self.assertRaises(ValueError):
                validate_setting(key, value)

    def test_concealed_audio_does_not_accumulate_or_enter_calibration(self):
        sink = main.VigilanteRuido(main.bot, main.CANAL_LOGS_ID)
        user = SimpleNamespace(id=123, name='Test')
        data = SimpleNamespace(pcm=self.pcm, packet=False)
        with patch.object(main, 'TELEMETRY') as telemetry:
            sink.write(user, data)
            telemetry.audio.assert_not_called()
        self.assertNotIn(user.id, sink.engines)

    def test_real_vad_can_detect_quiet_broadband_noise_above_silence(self):
        engine = AudioEngine()
        config = dict(DEFAULT_CONFIG)
        engine.feed(b'\0' * 3840 * 60, config, now=0)
        rng = np.random.default_rng(5)
        for index in range(100):
            pcm = stereo(rng.normal(0, 80, 960).astype('<i2'))
            result = engine.feed(pcm, config, now=.1)
        self.assertGreater(result[-1]['violation'], 50)
        self.assertLess(result[-1]['threshold'], result[-1]['band_rms'])

    def test_low_frequency_energy_does_not_score_in_manual_or_adaptive_mode(self):
        for automatic in (False, True):
            for frequency in (20, 60, 100):
                with self.subTest(automatic=automatic, frequency=frequency):
                    engine = self.engine()
                    config = dict(self.config, AUTO_SENSITIVITY=automatic, UMBRAL_RMS=15)
                    results = engine.feed(stereo(tone(3000, frequency, frames=80)), config, now=0)
                    self.assertGreater(results[-1]['rms'], 1000)
                    self.assertLess(results[-1]['band_rms'], 15)
                    self.assertTrue(all(result['violation'] == 0 for result in results))
                    self.assertEqual(results[-1]['status'], 'SILENCIO')
                    self.assertLess(engine.floor, 15)

    def test_background_learning_uses_filtered_energy(self):
        engine = self.engine()
        config = dict(self.config, AUTO_SENSITIVITY=True)
        engine.filter.process = Mock(return_value=tone(5).tobytes())
        results = engine.feed(self.pcm * 60, config, now=0)
        self.assertEqual(results[-1]['rms'], 200)
        self.assertAlmostEqual(engine.floor, 5, delta=1)
        self.assertGreaterEqual(results[-1]['threshold'], 9)
        self.assertLess(results[-1]['threshold'], 11)
        self.assertTrue(all(value < 6 for value in engine.floor_samples))

    def test_effective_threshold_is_clamped_in_both_modes(self):
        for automatic, threshold, floor, expected in [(False, 5, 0, 5),
                (False, 20000, 0, 10000), (True, 5, 0, 5), (True, 5, 20000, 10000)]:
            with self.subTest(automatic=automatic, threshold=threshold, floor=floor):
                engine = self.engine()
                engine.calibrate(floor)
                result = engine.feed(self.pcm, dict(self.config, AUTO_SENSITIVITY=automatic,
                                                   UMBRAL_RMS=threshold), now=0)[0]
                self.assertEqual(result['threshold'], expected)

    def test_percussive_noise_overrides_residual_voice_and_uncertain_vad(self):
        noise = np.random.default_rng(5).normal(0, 500, 960).astype('<i2')
        for speech in (False, None):
            with self.subTest(speech=speech):
                engine = self.engine(speech=True)
                engine.feed(self.pcm, self.config, now=0)
                engine.vad.is_speech.return_value = speech
                result = engine.feed(stereo(noise), self.config, now=.1)[0]
                self.assertGreater(result['zcr'], .28)
                self.assertGreaterEqual(result['band_rms'], result['threshold'])
                self.assertEqual(result['status'], 'RUIDO')
                self.assertEqual(result['violation'], 4)
                self.assertFalse(result['voice_protected'])
                self.assertEqual(engine.voice_hold, 0)

    def test_high_zcr_does_not_override_confirmed_voice(self):
        noise = np.random.default_rng(5).normal(0, 500, 960).astype('<i2')
        result = self.engine(speech=True).feed(stereo(noise), self.config, now=0)[0]
        self.assertGreater(result['zcr'], .28)
        self.assertEqual(result['status'], 'VOZ')
        self.assertEqual(result['violation'], 0)

    def test_percussive_scoring_preserves_continuous_noise_acceleration(self):
        engine = self.engine()
        engine.feed(self.pcm, self.config, now=0)
        engine.points = 40
        engine.noise_frames = self.config['TIEMPO_TOLERANCIA']
        noise = np.random.default_rng(5).normal(0, 500, 960).astype('<i2')
        result = engine.feed(stereo(noise), self.config, now=.1)[0]
        self.assertEqual(result['status'], 'RUIDO')
        self.assertEqual(result['violation'], 49)

    def test_high_zcr_below_band_threshold_cannot_add_points(self):
        engine = self.engine()
        results = engine.feed(stereo(square(200)) * 80, self.config, now=0)
        self.assertEqual(results[-1]['zcr'], 1.0)
        self.assertLess(results[-1]['band_rms'], results[-1]['threshold'])
        self.assertTrue(all(result['violation'] == 0 for result in results))

    def test_low_level_static_can_now_reach_mute_limit_without_vad_gain(self):
        engine = AudioEngine()
        config = dict(DEFAULT_CONFIG)
        engine.feed(b'\0' * 3840 * 60, config, now=0)
        noise = np.random.default_rng(5).normal(0, 26, 960 * 100).astype('<i2')
        results = engine.feed(stereo(noise), config, now=.1)
        self.assertGreater(results[-1]['violation'], config['CASTIGO_MAX'])
        self.assertFalse(results[-1]['speech'])

    def test_quiet_noise_below_previous_minimum_reaches_mute_limit(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                engine = self.engine()
                config = dict(self.config, AUTO_SENSITIVITY=automatic, UMBRAL_RMS=5)
                engine.feed(b'\0' * 3840 * 60, config, now=0)
                results = engine.feed(stereo(tone(8, frames=40)), config, now=.1)
                self.assertLess(results[-1]['band_rms'], 15)
                self.assertGreater(results[-1]['violation'], config['CASTIGO_MAX'])

    def test_background_below_lower_threshold_still_does_not_score(self):
        engine = self.engine()
        config = dict(self.config, AUTO_SENSITIVITY=True)
        engine.feed(b'\0' * 3840 * 60, config, now=0)
        results = engine.feed(stereo(tone(2, frames=40)), config, now=.1)
        self.assertTrue(all(result['violation'] == 0 for result in results))

    def test_calibration_collects_band_rms_instead_of_raw_volume(self):
        sink = main.VigilanteRuido(main.bot, main.CANAL_LOGS_ID)
        buffer = deque(maxlen=15000)
        users = {}
        with patch.object(main, 'CALIBRANDO', True), patch.object(main, 'BUFFER_CALIBRACION', buffer), \
             patch.object(main, 'CALIBRATION_USERS', users), patch.object(main, 'TELEMETRY') as telemetry, \
             patch('sentinel_audio.SpeechDetector.is_speech', return_value=False):
            sink.write(SimpleNamespace(id=123, name='Test'), SimpleNamespace(pcm=self.pcm * 3))
            bands = [call.kwargs['band_rms'] for call in telemetry.audio.call_args_list]
            raw = [call.args[1] for call in telemetry.audio.call_args_list]
            self.assertEqual(list(buffer), bands)
            self.assertEqual(users[123], bands)
            self.assertNotEqual(bands, raw)
        sink.cleanup()

    def test_diagnostic_keys_are_preserved_with_zcr_added(self):
        result = self.engine().feed(self.pcm, self.config, now=0)[0]
        self.assertEqual(set(result), {'rms', 'band_rms', 'speech', 'threshold', 'noise_floor',
            'status', 'reason', 'violation', 'dbfs', 'peak', 'clipping', 'voice_protected',
            'continuous_seconds', 'duration_ms', 'zcr', 'sensitivity'})
        self.assertEqual(result['zcr'], round(result['zcr'], 3))
        self.assertEqual(result['sensitivity'], 50)

    def test_individual_sensitivity_scales_manual_and_automatic_thresholds(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                thresholds = []
                for sensitivity in (0, 50, 100):
                    engine = self.engine()
                    config = dict(self.config, AUTO_SENSITIVITY=automatic,
                                  USER_SENSITIVITY=sensitivity, UMBRAL_RMS=100)
                    if automatic:
                        engine.floor = 100
                    thresholds.append(engine.feed(self.pcm, config, now=0)[0]['threshold'])
                self.assertGreater(thresholds[0], thresholds[1])
                self.assertGreater(thresholds[1], thresholds[2])


class SpeechDetectorGainTests(unittest.TestCase):
    def test_negligible_energy_is_false_even_with_vad_hangover(self):
        for level in (0, 1, 4):
            with self.subTest(level=level):
                detector = SpeechDetector()
                detector.vad = Mock()
                detector.vad.is_speech.return_value = True
                frame = np.tile(np.array([-level, level], dtype='<i2'), 240).tobytes()
                self.assertFalse(detector.is_speech(frame))
                detector.vad.is_speech.assert_not_called()

    def test_gain_limit_and_unamplified_low_levels(self):
        for level, expected in [(5, 5), (26, 26), (39, 39), (40, 160),
                                (100, 400), (200, 600), (600, 600), (2000, 2000)]:
            with self.subTest(level=level):
                detector = SpeechDetector()
                detector.vad = Mock()
                detector.vad.is_speech.return_value = False
                frame = np.tile(np.array([-level, level], dtype='<i2'), 240).tobytes()
                self.assertFalse(detector.is_speech(frame))
                passed, rate = detector.vad.is_speech.call_args.args
                self.assertEqual(rate, 48000)
                self.assertAlmostEqual(rms_pcm(passed), expected, delta=1)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_listen_failure_cleans_up_voice_connection(self):
        from unittest.mock import AsyncMock
        vc = SimpleNamespace(listen=Mock(side_effect=RuntimeError('receiver failed')), disconnect=AsyncMock())
        channel = SimpleNamespace(id=99, name='Test', guild=SimpleNamespace(id=123, me=object(), voice_client=None),
                                  permissions_for=Mock(return_value=SimpleNamespace(view_channel=True, connect=True)),
                                  connect=AsyncMock(return_value=vc))
        with patch.object(main, 'VOICE_CONNECT_LOCK', __import__('asyncio').Lock()):
            with self.assertRaises(RuntimeError):
                await main.connect_audio(channel)
        vc.disconnect.assert_awaited_once_with(force=True)
        self.assertNotIn(123, main.VOICE_JOINING)

    async def test_missing_permissions_prevent_connection(self):
        from unittest.mock import AsyncMock
        channel = SimpleNamespace(guild=SimpleNamespace(id=123, me=object(), voice_client=None),
                                  permissions_for=Mock(return_value=SimpleNamespace(view_channel=True, connect=False)),
                                  connect=AsyncMock())
        with patch.object(main, 'VOICE_CONNECT_LOCK', __import__('asyncio').Lock()):
            with self.assertRaises(PermissionError):
                await main.connect_audio(channel)
        channel.connect.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
