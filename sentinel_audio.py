"""Análisis PCM local por usuario. No graba ni envía audio a otros servicios."""
from collections import deque
import math
import time

import numpy as np

from sentinel_core import AudioFilter, SpeechDetector, score_audio


def adaptive_threshold(baseline):
    return min(10000.0, max(5.0, baseline * 1.6, baseline + 5))


def rms_pcm(pcm):
    values = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
    return float(np.sqrt(np.mean(values * values))) if values.size else 0.0


def stereo_to_mono(pcm):
    if len(pcm) % 4:
        raise ValueError('PCM estéreo incompleto')
    stereo = np.frombuffer(pcm, dtype='<i2').reshape(-1, 2).astype(np.float64)
    if not stereo.size:
        return b''
    # La media puede cancelar un micrófono con canales en oposición de fase.
    centred = stereo - np.mean(stereo, axis=0)
    energy = np.mean(centred * centred, axis=0)
    mono = stereo[:, int(np.argmax(energy))]
    return mono.astype('<i2').tobytes()


def calibrated_threshold(values):
    values = np.asarray(values, dtype=np.float64)
    if values.size < 25 or not np.isfinite(values).all() or np.any(values < 0):
        raise ValueError('No hay suficientes muestras válidas de fondo')
    baseline = float(np.percentile(values, 90))
    # Las muestras de fondo se expresan en RMS de la banda filtrada.
    return math.ceil(adaptive_threshold(baseline))


class AudioEngine:
    RATE = 48000
    FRAME_MS = 20
    FRAME_BYTES = 1920  # 960 muestras mono int16.

    def __init__(self):
        self.pending = b''
        self.filter = AudioFilter()
        self.vad = SpeechDetector()
        self.last_input = None
        self.floor_samples = deque(maxlen=250)
        self.floor = 0.0
        self.warmup = 0
        self.voice_hold = 0
        self.noise_evidence = deque(maxlen=20)
        # Track short attacks so repeated clicks/golpes can be separated from
        # a single transient. Sixty 20 ms frames forman una ventana de 1,2 s.
        self.frame_index = 0
        self.transient_events = deque()
        self.last_transient_frame = None
        self.transient_sequence_start = None
        self.points = 0
        self.noise_frames = 0
        self.audible_ms = 0
        self.silent_ms = 0
        self.noise_streak_ms = 0
        self.profile = None

    def reset_detection(self):
        self.points = self.noise_frames = self.audible_ms = self.silent_ms = self.noise_streak_ms = 0
        self.noise_evidence.clear()
        self.frame_index = 0
        self.transient_events.clear()
        self.last_transient_frame = None
        self.transient_sequence_start = None

    def migrate_state_from(self, previous):
        """Carry stable per-user state across an in-place detector code update."""
        for name in ('points', 'noise_frames', 'floor', 'warmup', 'last_input',
                     'profile', 'audible_ms', 'silent_ms', 'noise_streak_ms', 'voice_hold'):
            if hasattr(previous, name):
                setattr(self, name, getattr(previous, name))
        if hasattr(previous, 'floor_samples'):
            self.floor_samples.extend(previous.floor_samples)

    def calibrate(self, baseline):
        self.floor = max(0.0, float(baseline))
        self.floor_samples.clear()
        self.warmup = 50
        self.reset_detection()

    def feed(self, pcm, config, now=None):
        now = time.monotonic() if now is None else now
        mono = stereo_to_mono(pcm)
        if not mono:
            return []
        profile = (config.get('AUTO_SENSITIVITY', True), config.get('AUDIO_MODE', 'noise'),
                   config['UMBRAL_RMS'], config.get('CONTINUOUS_SECONDS', 15), config['AUTO_CASTIGOS'],
                   config.get('USER_SENSITIVITY', 50), config.get('USER_THRESHOLD_RMS', 0),
                   config.get('USER_PENALTY_RATE', 1), config.get('USER_GRACE_SECONDS', 2),
                   config.get('USER_MUTE_DURATION', 0), config.get('USER_VOICE_PROTECTION', True))
        if profile != self.profile:
            self.reset_detection()
            self.profile = profile
        if self.last_input is not None:
            elapsed = now - self.last_input
            if elapsed > .5:
                # DTX: descartar fragmentos y voz residual sin borrar infracciones.
                self.pending = b''
                self.voice_hold = 0
                self.noise_streak_ms = 0
                self.frame_index += int(elapsed * 1000 / self.FRAME_MS)
                points_to_drain = int(elapsed * 10)
                self.points = max(0, self.points - points_to_drain)
                if elapsed > 2.0:
                    self.noise_evidence.clear()
                    self.noise_frames = 0
                    self.audible_ms = 0
                    self.frame_index = 0
                    self.transient_events.clear()
                    self.last_transient_frame = None
                    self.transient_sequence_start = None
                    self.filter = AudioFilter()
                    self.vad = SpeechDetector()
        self.last_input = now
        self.pending += mono
        results = []
        while len(self.pending) >= self.FRAME_BYTES:
            frame, self.pending = self.pending[:self.FRAME_BYTES], self.pending[self.FRAME_BYTES:]
            results.append(self.analyse(frame, config))
        return results

    def analyse(self, mono, config):
        samples = np.frombuffer(mono, dtype='<i2').astype(np.float64)
        # El offset DC no es sonido audible y no debe generar infracciones.
        centred = samples - float(np.mean(samples))
        raw = float(np.sqrt(np.mean(centred * centred)))
        filtered = self.filter.process(mono)
        band_values = np.frombuffer(filtered, dtype='<i2').astype(np.float64)
        band_rms = rms_pcm(filtered)
        band_peak = float(np.max(np.abs(band_values))) if band_values.size else 0.0
        crest_factor = band_peak / max(band_rms, 1.0)
        normalized = centred / max(float(np.max(np.abs(centred))), 1.0)
        zcr = float(np.mean(normalized[:-1] * normalized[1:] < 0)) if normalized.size > 1 else 0.0
        speech = self.vad.is_speech(mono)
        clipping = float(np.mean(np.abs(samples) >= 32760))
        automatic = config.get('AUTO_SENSITIVITY', True)
        self.warmup += 1
        if self.warmup <= 50:
            # Only learn near-digital silence. Otherwise a speaker, fan, or
            # other audible background present at startup would become the
            # reference level and mask itself indefinitely.
            if speech is False and band_rms <= 5.5:
                self.floor_samples.append(band_rms)
            if self.floor_samples:
                self.floor = float(np.percentile(self.floor_samples, 20))
        sensitivity = min(100, max(0, int(config.get('USER_SENSITIVITY', 50))))
        # 50 es neutral; 0 duplica el umbral y 100 lo reduce a la mitad.
        sensitivity_factor = 2.0 ** ((50 - sensitivity) / 50)
        gate = (adaptive_threshold(self.floor) if automatic
                else min(10000.0, max(5.0, float(config['UMBRAL_RMS']))))
        personal_threshold = int(config.get('USER_THRESHOLD_RMS', 0))
        if personal_threshold > 0:
            gate = float(personal_threshold)
        gate = min(10000.0, max(5.0, gate * sensitivity_factor))
        if self.warmup > 50 and speech is False and band_rms <= 5.5:
            self.floor_samples.append(band_rms)
            target = float(np.percentile(self.floor_samples, 20))
            self.floor = .98 * self.floor + .02 * target
        # Reject filter leakage from a loud source below 300 Hz. A real voice
        # or in-band noise has substantially more energy after the band-pass.
        out_of_band = raw > max(100.0, gate * 4) and band_rms / max(raw, 1.0) < .005
        audible = band_rms >= gate and not out_of_band
        self.frame_index += 1
        transient_candidate = audible and crest_factor >= 6.0 and speech is not True
        if transient_candidate and (self.last_transient_frame is None
                                    or self.frame_index - self.last_transient_frame >= 3):
            if (self.last_transient_frame is None
                    or self.frame_index - self.last_transient_frame > 60):
                self.transient_sequence_start = self.frame_index
            self.transient_events.append(self.frame_index)
            self.last_transient_frame = self.frame_index
        while self.transient_events and self.frame_index - self.transient_events[0] > 60:
            self.transient_events.popleft()
        repeated_transient = transient_candidate and len(self.transient_events) >= 3
        transient_age_ms = ((self.frame_index - self.transient_sequence_start) * self.FRAME_MS
                            if repeated_transient and self.transient_sequence_start is not None else 0)
        percussive = ((audible and zcr > .28 and speech is not True and not transient_candidate)
                      or repeated_transient)
        protect_voice = config.get('USER_VOICE_PROTECTION', True)
        if percussive:
            self.voice_hold = 0  # Un clic confirmado no hereda protección de voz.
        elif speech and protect_voice:
            self.voice_hold = 10  # Protege pausas breves de 200 ms dentro del habla.
        else:
            self.voice_hold = max(0, self.voice_hold - 1)
        protected_speech = bool(protect_voice and (speech or self.voice_hold))
        if audible:
            self.audible_ms += self.FRAME_MS
            self.silent_ms = 0
        else:
            self.silent_ms += self.FRAME_MS
            if self.silent_ms >= 500:
                self.audible_ms = 0
                self.noise_streak_ms = 0
        self.noise_evidence.append(audible and not protected_speech)
        if audible and not protected_speech:
            self.noise_streak_ms += self.FRAME_MS
        else:
            self.noise_streak_ms = 0
        sustained_audio_override = (config.get('AUDIO_MODE', 'noise') == 'noise'
                                    and protect_voice and audible and self.audible_ms >= 5000)
        percussive_age_ms = transient_age_ms if repeated_transient else self.noise_streak_ms
        if (percussive and percussive_age_ms
                < int(config.get('USER_GRACE_SECONDS', 0)) * 1000):
            grace = int(config.get('USER_GRACE_SECONDS', 0))
            status, reason = 'GRACIA', f'Periodo de gracia: {percussive_age_ms / 1000:.1f} / {grace} s.'
        elif percussive:
            self.points, self.noise_frames, status = self._score_confirmed_noise(
                band_rms, gate, config, accelerate=repeated_transient)
            status = 'RUIDO'
            reason = ('Clics o golpes repetidos: 3 o más impulsos en una ventana de 1,2 s.'
                      if repeated_transient else
                      'Transitorio de alta tasa de cruces por cero sin voz confirmada.')
        elif transient_candidate:
            status, reason = 'ANALIZANDO', 'Golpe aislado: esperando repetición antes de clasificarlo como ruido molesto.'
        elif speech is None:
            status, reason = 'ANALIZANDO', 'Esperando un bloque completo de voz.'
        elif (config.get('AUDIO_MODE', 'noise') == 'continuous' and audible
              and (not protected_speech or 'USER_VOICE_PROTECTION' not in config)
              and self.audible_ms >= config.get('CONTINUOUS_SECONDS', 15) * 1000):
            self.points += math.ceil(4 * int(config.get('USER_PENALTY_RATE', 1)))
            status, reason = 'AUDIO CONTINUO', 'Superó el tiempo de audio continuo configurado (incluye voces).'
        elif sustained_audio_override:
            self.points, self.noise_frames, _ = self._score_confirmed_noise(
                band_rms, gate, config)
            status, reason = 'RUIDO CONTINUO', 'Audio audible persistente durante 5 s; se aplica penalización aunque el VAD lo clasifique como voz.'
        elif audible and not protected_speech and sum(self.noise_evidence) < 4:
            status, reason = 'ANALIZANDO', 'Ruido breve: esperando confirmar que se repite.'
        elif (audible and not protected_speech
              and self.noise_streak_ms < int(config.get('USER_GRACE_SECONDS', 0)) * 1000):
            grace = int(config.get('USER_GRACE_SECONDS', 0))
            status, reason = 'GRACIA', f'Periodo de gracia: {self.noise_streak_ms / 1000:.1f} / {grace} s.'
        else:
            if audible and not protected_speech:
                self.points, self.noise_frames, status = self._score_confirmed_noise(
                    band_rms, gate, config)
            else:
                effective = dict(config, UMBRAL_RMS=gate)
                self.points, self.noise_frames, status = score_audio(
                    0.0 if out_of_band else band_rms, protected_speech,
                    self.points, self.noise_frames, effective)
            reason = ('Energía fuera de la banda audible de 300–4.000 Hz.' if out_of_band else
                      'Voz detectada: protegida en el modo de ruido.' if protected_speech and audible
                      else 'La energía de 300–4.000 Hz está por debajo del umbral.' if not audible
                      else 'Ruido audible en banda por encima del umbral y sin voz detectada.')
            if protected_speech and audible and config.get('AUDIO_MODE', 'noise') == 'continuous':
                reason = f"Audio continuo: {self.audible_ms / 1000:.1f} / {config.get('CONTINUOUS_SECONDS', 15)} s."
        return {'rms': round(raw), 'band_rms': round(band_rms), 'zcr': round(zcr, 3),
                'dbfs': round(20 * math.log10(max(raw, 1e-6) / 32768), 1),
                'peak': int(np.max(np.abs(samples))), 'clipping': round(clipping, 4),
                'speech': speech, 'threshold': round(gate, 1), 'noise_floor': round(self.floor, 1),
                'sensitivity': sensitivity,
                'voice_protected': protected_speech,
                'continuous_seconds': round(self.audible_ms / 1000, 2),
                'status': status, 'reason': reason, 'violation': self.points,
                'duration_ms': self.FRAME_MS}

    def _score_confirmed_noise(self, band_rms, gate, config, *, accelerate=True):
        """Give repeated, confirmed noise a faster path to the existing mute limit."""
        effective = dict(config, UMBRAL_RMS=gate)
        previous_points = self.points
        points, continuous, status = score_audio(
            band_rms, False, self.points, self.noise_frames, effective)
        # The base score is 4 per 20 ms (5 after the legacy continuous limit).
        # Add 4 more once repeated evidence confirms it is persistent. This
        # raises the rate to 8/9 points per frame without changing voice/silence.
        if status == 'RUIDO CONTINUO' or (status == 'RUIDO' and accelerate):
            points += 4
        rate = max(1, min(3, int(config.get('USER_PENALTY_RATE', 1))))
        points = previous_points + math.ceil((points - previous_points) * rate)
        self.points, self.noise_frames = points, continuous
        return points, continuous, status
