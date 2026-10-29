"""Adaptador de recepción DAVE para discord-ext-voice-recv 0.5.2a179.

Descifra E2EE antes de Opus. No desactiva cifrado ni entrega datos cifrados
al decodificador. Referencia: PR 54 del proyecto discord-ext-voice-recv.
"""
from collections import OrderedDict
import logging
import time
from threading import Lock

from davey import MediaType
from discord.opus import OpusError
from discord.ext.voice_recv import opus, router

logger = logging.getLogger('sentinel.voice')
_metrics = {'decoded': 0, 'discarded': 0, 'concealed': 0, 'dave_errors': 0, 'last_error': None}
_metrics_lock = Lock()


def voice_metrics():
    with _metrics_lock:
        return dict(_metrics)


class DroppedAudio(Exception):
    pass


class DavePacketDecoder(opus.PacketDecoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._plain_cache = OrderedDict()
        self._warning_time = -float('inf')

    def _payload(self, packet):
        payload = packet.decrypted_data
        if payload is None:
            raise DroppedAudio('Paquete sin datos de audio')
        if packet.is_silence():
            return payload
        voice = self.sink.voice_client
        connection = voice._connection
        if not getattr(connection, 'dave_protocol_version', 0):
            return payload
        identity = self._cached_id or voice._get_id_from_ssrc(self.ssrc)
        session = getattr(connection, 'dave_session', None)
        if identity is None or session is None or not session.ready:
            raise DroppedAudio('DAVE o identidad del hablante aún no disponibles')
        key = (packet.sequence, packet.timestamp)
        if key not in self._plain_cache:
            try:
                self._plain_cache[key] = session.decrypt(identity, MediaType.audio, bytes(payload))
            except Exception as error:
                with _metrics_lock:
                    _metrics['dave_errors'] += 1
                raise DroppedAudio(f'No se pudo descifrar DAVE: {type(error).__name__}') from error
            while len(self._plain_cache) > 8:
                self._plain_cache.popitem(last=False)
        return self._plain_cache[key]

    def _decode_packet(self, packet):
        if packet:
            return packet, self._decoder.decode(self._payload(packet), fec=False)
        next_packet = self._buffer.peek_next()
        if next_packet is not None:
            return packet, self._decoder.decode(self._payload(next_packet), fec=True)
        return packet, self._decoder.decode(None, fec=False)

    def _process_packet(self, packet):
        try:
            result = super()._process_packet(packet)
            with _metrics_lock:
                _metrics['decoded' if packet else 'concealed'] += 1
            return result
        except (DroppedAudio, OpusError) as error:
            with _metrics_lock:
                _metrics['discarded'] += 1
                _metrics['last_error'] = str(error)
            # Un paquete inválido no debe matar el hilo ni fabricar silencio
            # que reduzca las infracciones o mantenga vivo un receptor roto.
            if time.monotonic() - self._warning_time > 5:
                logger.warning('Audio descartado: %s', str(error))
                self._warning_time = time.monotonic()
            self._last_seq, self._last_ts = packet.sequence, packet.timestamp
            return opus.VoiceData(packet, None, pcm=b'')

    def reset(self):
        super().reset()
        self._plain_cache.clear()


def install_voice_compat():
    from importlib.metadata import version
    if version('discord-ext-voice-recv') != '0.5.2a179':
        raise RuntimeError('El adaptador DAVE requiere discord-ext-voice-recv 0.5.2a179. Usa requirements.txt.')
    router.PacketDecoder = DavePacketDecoder
