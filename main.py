"""Sentinel: bot de voz y dashboard local. Python 3.12."""
import asyncio
import collections
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
from contextlib import ExitStack
import math
import os
from pathlib import Path
import sys
import shutil
import struct
import sqlite3
import importlib.metadata
import platform
import subprocess
from threading import Lock, RLock, Thread
import time

import discord
from discord.ext import commands, voice_recv
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO
from werkzeug.serving import make_server
import numpy as np
import requests

from sentinel_core import ConfigStore, Telemetry
from sentinel_audio import AudioEngine, calibrated_threshold
from sentinel_runtime import acquire_instance
from voice_compat import install_voice_compat, voice_metrics

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
TOKEN = os.getenv('DISCORD_TOKEN', 'TU_TOKEN_AQUI')
PREVIEW = '--preview' in sys.argv
ID_SEBAS = int(os.getenv('SENTINEL_OWNER_ID', '0'))
CANAL_LOGS_ID = int(os.getenv('SENTINEL_LOG_CHANNEL_ID', '0'))
STORE = ConfigStore(ROOT / ('backups/demo_sentinel.db' if PREVIEW else 'sentinel.db'),
                    legacy_config=ROOT / ('backups/demo_data.json' if PREVIEW else 'sentinel_data.json'))
CONFIG = STORE.data
TELEMETRY = Telemetry(STORE.database)
SALIDA_AUTORIZADA = False
ULTIMO_LATIDO = time.time()
CALIBRANDO = False
BUFFER_CALIBRACION = collections.deque(maxlen=15000)
COOLDOWN_CASTIGOS = {}
CASTIGOS_ACTIVOS = set()
WATCHDOG_TASK = None
AUDIO_RELOAD_TASK = None
AUDIO_RELOAD_LOCK = asyncio.Lock()
CALIBRATION_GENERATION = 0
CALIBRATION_USERS = {}
CALIBRATION_BASELINES = {}
VOICE_CONNECT_LOCK = asyncio.Lock()
VOICE_JOINING = set()
VOICE_RESTARTING = set()
VOICE_REJOINING = set()
VOICE_LEAVE_AUTHORIZED = set()
VOICE_AUTO_JOIN_SUPPRESSED = set()
VOICE_FOLLOW_TASKS = {}
VOICE_PENDING_FOLLOWS = {}
VOICE_RECENT_MOVE_TARGETS = {}
VOICE_FOLLOW_DEBOUNCE = .08
VOICE_MOVE_TIMEOUT = 2.5
VOICE_RECENT_MOVE_TTL = 30
MUTE_JOURNAL_LOCK = asyncio.Lock()
MUTE_TASKS = {}  # (guild_id, user_id): castigo vivo o tarea de recuperación.
EVENT_TASKS = set()  # Referencias a escrituras originadas en callbacks síncronos.
DIAGNOSTICS_LOCK = Lock()
DIAGNOSTICS_CACHE = None
DIAGNOSTICS_CACHE_AT = 0.0
AUDIO_SOURCE_PATH = ROOT / 'sentinel_audio.py'


def _audio_source_signature():
    try:
        source_stat = AUDIO_SOURCE_PATH.stat()
        return source_stat.st_mtime_ns, source_stat.st_size
    except OSError:
        return None


AUDIO_SOURCE_SIGNATURE = _audio_source_signature()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('sentinel')
log_directory = ROOT / 'logs'
log_directory.mkdir(exist_ok=True)
file_handler = RotatingFileHandler(log_directory / 'sentinel.log', maxBytes=2_000_000,
                                  backupCount=3, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
logging.getLogger().addHandler(file_handler)
logging.getLogger('discord.ext.voice_recv').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SENTINEL_SECRET', os.urandom(32).hex())
socketio = SocketIO(app, async_mode='threading')  # Solo origen de la propia página.


def save_db():
    STORE.save()


def _persist_event(message, level):
    try:
        record = TELEMETRY.event(message, level)
        socketio.emit('log_event', record)
    except sqlite3.Error:
        # Un fallo del historial no debe impedir liberar un mute de Discord.
        logger.exception('No se pudo persistir el evento en SQLite.')


def log_event(message, level='info'):
    getattr(logger, 'warning' if level == 'warning' else 'error' if level == 'error' else 'info')(message)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _persist_event(message, level)  # Flask y receptor ya ejecutan en otro hilo.
    else:
        task = loop.create_task(asyncio.to_thread(_persist_event, message, level))
        EVENT_TASKS.add(task)
        def completed(result):
            EVENT_TASKS.discard(result)
            if not result.cancelled() and result.exception() is not None:
                logger.error('Falló la escritura del evento: %s', type(result.exception()).__name__)
        task.add_done_callback(completed)


async def _database_operation(function, *args):
    """Esperar también al escritor si se cancela quien mantiene el lock async."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _log_event_async(message, level='info'):
    await _database_operation(log_event, message, level)


async def registrar_mute(guild_id, user_id, duration, jail_id=None, orig_id=None):
    record = {'guild_id': int(guild_id), 'user_id': int(user_id),
              'unmute_at': time.time() + duration,
              'jail_id': jail_id, 'orig_id': orig_id}
    async with MUTE_JOURNAL_LOCK:
        await _database_operation(STORE.database.put_pending_mute, record)


async def liberar_mute_journal(guild_id, user_id):
    """Eliminar solo después de que Discord haya aceptado mute=False."""
    async with MUTE_JOURNAL_LOCK:
        await _database_operation(STORE.database.delete_pending_mute, guild_id, user_id)


async def _recuperar_mute(record):
    key = (record['guild_id'], record['user_id'])
    released = False
    try:
        while not bot.is_closed():
            remaining = record['unmute_at'] - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            try:
                guild = bot.get_guild(record['guild_id'])
                # Un miembro desconectado puede seguir muteado al volver.
                # Conservar el journal y reintentar, sin fabricar una liberación.
                if guild is None:
                    await asyncio.sleep(10)
                    continue
                member = guild.get_member(record['user_id'])
                if member is None:
                    member = await guild.fetch_member(record['user_id'])
                if not member.voice or not member.voice.channel:
                    await asyncio.sleep(10)
                    continue
                orig = guild.get_channel(record.get('orig_id')) if record.get('orig_id') else None
                if orig and member.voice.channel.id == record.get('jail_id'):
                    try:
                        await member.move_to(orig, reason='Sentinel: recuperación de pausa')
                    except discord.HTTPException:
                        await _log_event_async('No se pudo devolver al canal original durante la recuperación.', 'warning')
                await member.edit(mute=False, reason='Sentinel: recuperación de castigo pendiente')
                await liberar_mute_journal(*key)
                released = True
                await _log_event_async(f'{member.display_name}: mute pendiente liberado.')
                return
            except (discord.HTTPException, OSError, ValueError, sqlite3.Error) as error:
                await _log_event_async(f'Liberación pendiente fallida: {type(error).__name__}; se reintentará.', 'error')
                await asyncio.sleep(10)
    finally:
        if MUTE_TASKS.get(key) is asyncio.current_task():
            MUTE_TASKS.pop(key, None)
        if released:
            CASTIGOS_ACTIVOS.discard(record['user_id'])


async def reconciliar_mutes_pendientes():
    """Reanudar los plazos durables sin duplicar castigos ni tareas en on_ready."""
    if PREVIEW:
        return
    try:
        async with MUTE_JOURNAL_LOCK:
            records = await _database_operation(STORE.database.pending_mutes)
            for record in records:
                key = (record['guild_id'], record['user_id'])
                existing = MUTE_TASKS.get(key)
                if existing is not None and not existing.done():
                    continue
                CASTIGOS_ACTIVOS.add(record['user_id'])
                task = asyncio.create_task(_recuperar_mute(record))
                MUTE_TASKS[key] = task
                def completed(result):
                    if not result.cancelled() and result.exception() is not None:
                        log_event(f'Recuperación de mute fallida: {type(result.exception()).__name__}', 'error')
                task.add_done_callback(completed)
    except (OSError, ValueError, sqlite3.Error) as error:
        await _log_event_async(f'No se pudo leer el journal de mutes: {type(error).__name__}.', 'error')


def safe_submit(coroutine):
    """El receptor de audio y Flask no ejecutan en el hilo del event loop."""
    try:
        if not bot.is_ready() or bot.loop.is_closed():
            raise RuntimeError('Discord todavía no está conectado.')
        future = asyncio.run_coroutine_threadsafe(coroutine, bot.loop)
        def completed(result):
            if not result.cancelled():
                error = result.exception()
                if error:
                    log_event(f'Operación de Discord fallida: {type(error).__name__}', 'error')
        future.add_done_callback(completed)
        return future
    except Exception:
        coroutine.close()
        raise


def runtime_snapshot():
    clients = list(bot.voice_clients) if not PREVIEW else []
    members = [member for client in clients if client.channel for member in client.channel.members]
    snapshot = TELEMETRY.snapshot(None if PREVIEW else members)
    latency = bot.latency if not PREVIEW else 0.042
    snapshot.update({
        'preview': PREVIEW, 'connected': bot.is_ready() if not PREVIEW else True,
        'voice_connected': any(client.is_connected() for client in clients) if not PREVIEW else True,
        'channel': ', '.join(client.channel.name for client in clients if client.channel) if not PREVIEW else 'Sala de pruebas',
        'latency_ms': round(latency * 1000) if math.isfinite(latency) else None,
        'heartbeat_age': 0 if PREVIEW else round(max(0, time.time() - ULTIMO_LATIDO), 1),
        'config': STORE.snapshot(), 'owner_id': str(ID_SEBAS),
        'audio_diagnostics': voice_metrics(),
        'system_diagnostics': system_diagnostics(),
    })
    for user in snapshot['users']:
        user['immune'] = user['id'] in snapshot['config']['WHITELIST']
        if user['immune']:
            user['violation'] = 0
            user['status'] = 'INMUNE'
        user['limit'] = CONFIG['CASTIGO_MAX']
        user['punishments'] = snapshot['config']['STATS'].get(user['id'], 0)
        user['audio_settings'] = STORE.audio_settings(user['id'])
        user['sensitivity'] = user['audio_settings']['sensitivity']
    return snapshot


def system_diagnostics():
    global DIAGNOSTICS_CACHE, DIAGNOSTICS_CACHE_AT
    now = time.monotonic()
    with DIAGNOSTICS_LOCK:
        if DIAGNOSTICS_CACHE is not None and now - DIAGNOSTICS_CACHE_AT < 5:
            return DIAGNOSTICS_CACHE
    packages = {'discord.py': 'discord.py', 'discord-ext-voice-recv': 'discord-ext-voice-recv',
                'Flask': 'Flask', 'Flask-SocketIO': 'Flask-SocketIO', 'NumPy': 'numpy',
                'SciPy': 'scipy', 'WebRTC VAD': 'webrtcvad-wheels'}
    versions = {}
    for label, package in packages.items():
        try:
            versions[label] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[label] = 'Falta'
    checks = []
    if not PREVIEW and bot.is_ready():
        guilds = list(bot.guilds)
        for guild in guilds:
            me = guild.me
            jail = discord.utils.get(guild.voice_channels, name='La Cárcel')
            jail_move = bool(jail and me and jail.permissions_for(me).move_members)
            checks.append({'guild': guild.name,
                'view_channel': bool(me and any(channel.permissions_for(me).view_channel for channel in guild.voice_channels)),
                'connect': bool(me and any(channel.permissions_for(me).connect for channel in guild.voice_channels)),
                'mute_members': bool(me and me.guild_permissions.mute_members),
                'move_members': jail_move, 'jail_configured': bool(jail)})
    recent = [event for event in TELEMETRY.snapshot()['events'] if event['level'] == 'error'][-5:]
    try:
        setup_check = subprocess.run([sys.executable, str(ROOT / 'iniciar_sentinel.py'), '--check'],
            cwd=ROOT, capture_output=True, text=True, timeout=8, check=False)
        setup = {'ok': setup_check.returncode == 0,
                 'message': (setup_check.stdout.strip() or setup_check.stderr.strip())[:500]}
    except (OSError, subprocess.SubprocessError) as error:
        setup = {'ok': False, 'message': f'No se pudo verificar la instalación: {type(error).__name__}'}
    result = {'python': platform.python_version(), 'platform': platform.platform(),
            'dependencies': versions, 'permissions': checks,
            'recent_errors': [{'time': event['time'], 'message': event['msg']} for event in recent],
            'setup_check': setup}
    with DIAGNOSTICS_LOCK:
        DIAGNOSTICS_CACHE = result
        DIAGNOSTICS_CACHE_AT = time.monotonic()
    return result


@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/get_config')
def get_config():
    return jsonify(STORE.snapshot())


@app.route('/api/status')
def get_status():
    return jsonify(runtime_snapshot())


@socketio.on('connect')
def client_connect():
    socketio.emit('state_update', runtime_snapshot(), to=request.sid)


@socketio.on('restart_voice')
def handle_restart_voice(data=None):
    if PREVIEW:
        return {'ok': False, 'error': 'El reinicio de voz no está disponible en la demostración.'}
    client = next((vc for vc in bot.voice_clients if vc.is_connected() and vc.channel), None)
    if client is None:
        return {'ok': False, 'error': 'Sentinel no está conectado a un canal de voz.'}
    try:
        safe_submit(restart_process(client))
        return {'ok': True}
    except RuntimeError as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('connect_voice')
def handle_connect_voice(data=None):
    if PREVIEW:
        return {'ok': False, 'error': 'La conexión manual no está disponible en la demostración.'}
    try:
        future = safe_submit(_connect_voice_to_occupied_channel())
        result = future.result(timeout=20)
        return result
    except TimeoutError:
        return {'ok': False, 'error': 'La conexión está tardando. Revisa el estado antes de volver a intentarlo.'}
    except RuntimeError as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('update_config')
def handle_cfg(data):
    try:
        if not isinstance(data, dict):
            raise ValueError('Solicitud inválida.')
        STORE.update(data.get('key'), data.get('value'))
        if data.get('key') == 'AUTO_UNIR_VOZ' and data.get('value') is True:
            VOICE_AUTO_JOIN_SUPPRESSED.clear()
            if bot.is_ready():
                try:
                    safe_submit(_auto_join_occupied_channels())
                except RuntimeError:
                    pass
        socketio.emit('config_update', STORE.snapshot())
        log_event(f"Ajuste actualizado: {data['key']} = {data['value']}")
        return {'ok': True, 'config': STORE.snapshot()}
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('set_whitelist')
def handle_whitelist(data):
    try:
        if not isinstance(data, dict):
            raise ValueError('Solicitud inválida.')
        STORE.whitelist(data.get('id'), data.get('enabled'))
        socketio.emit('config_update', STORE.snapshot())
        log_event('Lista de inmunidad actualizada.')
        return {'ok': True, 'config': STORE.snapshot()}
    except (ValueError, OSError, sqlite3.Error) as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('set_user_sensitivity')
def handle_user_sensitivity(data):
    try:
        if not isinstance(data, dict):
            raise ValueError('Solicitud inválida.')
        value = STORE.set_sensitivity(data.get('id'), data.get('value'))
        socketio.emit('state_update', runtime_snapshot())
        log_event(f"Sensibilidad individual actualizada para {data['id']}: {value}/100.")
        return {'ok': True, 'value': value}
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('set_user_audio_setting')
def handle_user_audio_setting(data):
    try:
        if not isinstance(data, dict):
            raise ValueError('Solicitud inválida.')
        settings = STORE.set_audio_setting(data.get('id'), data.get('key'), data.get('value'))
        socketio.emit('state_update', runtime_snapshot())
        log_event(f"Ajuste individual actualizado para {data['id']}: {data['key']}.")
        return {'ok': True, 'settings': settings}
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('set_user_profile')
def handle_user_profile(data):
    try:
        if not isinstance(data, dict):
            raise ValueError('Solicitud inválida.')
        settings = STORE.set_audio_profile(data.get('id'), data.get('profile'))
        socketio.emit('state_update', runtime_snapshot())
        log_event(f"Perfil individual {data['profile']} aplicado a {data['id']}.")
        return {'ok': True, 'settings': settings}
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        return {'ok': False, 'error': str(error)}


@socketio.on('toggle_godmode')
def handle_god():
    return handle_whitelist({'id': str(ID_SEBAS), 'enabled': ID_SEBAS not in CONFIG['WHITELIST']})


@socketio.on('start_calibration')
def handle_cal(data=None):
    global CALIBRANDO
    with TELEMETRY.lock:
        if CALIBRANDO:
            return {'ok': False, 'error': 'Ya hay una calibración en curso.'}
        if not PREVIEW and not any(vc.is_connected() and vc.is_listening() for vc in bot.voice_clients):
            return {'ok': False, 'error': 'Conecta el bot a un canal con !vigilar.'}
        CALIBRANDO = True
        BUFFER_CALIBRACION.clear()
        CALIBRATION_USERS.clear()
        TELEMETRY.calibration = {'active': True, 'progress': 0, 'samples': 0}
    log_event('Calibración iniciada. Mantengan silencio durante 5 segundos.')
    def finish():
        global CALIBRANDO, CALIBRATION_GENERATION, CALIBRATION_BASELINES
        try:
            for tick in range(50):
                time.sleep(0.1)
                with TELEMETRY.lock:
                    TELEMETRY.calibration.update(progress=(tick + 1) * 2, samples=len(BUFFER_CALIBRACION))
            with TELEMETRY.lock:
                values = list(BUFFER_CALIBRACION)
                user_samples = {identity: samples[:] for identity, samples in CALIBRATION_USERS.items()}
            if len(values) < 25:
                log_event('Calibración cancelada: no hay suficientes muestras de fondo. El umbral no cambió.', 'warning')
            else:
                value = calibrated_threshold(values)
                STORE.update('UMBRAL_RMS', value)
                CALIBRATION_BASELINES = {identity: float(np.percentile(samples, 90))
                    for identity, samples in user_samples.items() if len(samples) >= 25}
                CALIBRATION_GENERATION += 1
                socketio.emit('config_update', STORE.snapshot())
                log_event(f'Calibración completada: umbral {value} RMS, {len(values)} muestras sin voz.')
        except Exception as error:
            log_event(f'No se pudo calibrar: {type(error).__name__}', 'error')
        finally:
            with TELEMETRY.lock:
                CALIBRANDO = False
                TELEMETRY.calibration['active'] = False
    Thread(target=finish, daemon=True).start()
    return {'ok': True}


@app.route('/simulate_attack', methods=['POST'])
def simulate_attack():
    # Evita ejecutar pruebas destructivas desde formularios en otros sitios.
    if request.headers.get('X-Sentinel-Action') != 'chaos':
        return jsonify(error='Solicitud inválida.'), 403
    data = request.get_json(silent=True) or {}
    kind = data.get('type') if isinstance(data, dict) else None
    if kind not in ('kick', 'deafen'):
        return jsonify(error='Prueba desconocida.'), 400
    if PREVIEW:
        return jsonify(error='Las pruebas de Discord están desactivadas en la demostración.'), 409
    if not bot.voice_clients:
        return jsonify(error='El bot no está en un canal de voz.'), 409
    try:
        safe_submit(ejecutar_ataque(kind))
        return jsonify(status='scheduled')
    except RuntimeError as error:
        return jsonify(error=str(error)), 409


async def ejecutar_ataque(tipo):
    if not bot.voice_clients:
        return
    vc = bot.voice_clients[0]
    await _log_event_async(f'Prueba de autodefensa: {tipo}', 'warning')
    if tipo == 'kick':
        await vc.disconnect()
    elif tipo == 'deafen':
        await vc.guild.me.edit(deafen=True, mute=True)


class VigilanteRuido(voice_recv.AudioSink):
    def __init__(self, bot, logs_id):
        super().__init__()
        self.bot, self.logs_id = bot, logs_id
        self.viol = collections.defaultdict(int)
        self.cont = collections.defaultdict(int)
        self.engines = {}
        self._engine_lock = RLock()
        self.calibration_generation = CALIBRATION_GENERATION
        self._frame_warning = 0

    def wants_opus(self):
        return False

    def write(self, user, data):
        with self._engine_lock:
            self._write_locked(user, data)

    def _write_locked(self, user, data):
        global ULTIMO_LATIDO
        if not user or not data.pcm:
            return
        # FEC/PLC sintetiza muestras ante pérdidas: no castigar por audio inventado.
        if getattr(data, 'packet', None) is not None and not data.packet:
            return
        ULTIMO_LATIDO = time.time()
        try:
            if self.calibration_generation != CALIBRATION_GENERATION:
                for identity, engine in self.engines.items():
                    if identity in CALIBRATION_BASELINES:
                        engine.calibrate(CALIBRATION_BASELINES[identity])
                    else:
                        engine.reset_detection()
                self.calibration_generation = CALIBRATION_GENERATION
            if user.id not in self.engines:
                self.engines[user.id] = AudioEngine()
                if user.id in CALIBRATION_BASELINES:
                    self.engines[user.id].calibrate(CALIBRATION_BASELINES[user.id])
            engine = self.engines[user.id]
            with STORE.lock:
                config = dict(CONFIG)
                config['WHITELIST'] = list(CONFIG['WHITELIST'])
            user_settings = STORE.audio_settings(str(user.id))
            config.update({
                'USER_SENSITIVITY': user_settings['sensitivity'],
                'USER_THRESHOLD_RMS': user_settings['threshold_rms'],
                'USER_PENALTY_RATE': user_settings['penalty_rate'],
                'USER_GRACE_SECONDS': user_settings['grace_seconds'],
                'USER_MUTE_DURATION': user_settings['mute_duration'],
                'USER_VOICE_PROTECTION': user_settings['protect_voice'],
            })
            results = engine.feed(data.pcm, config)
        except (ValueError, TypeError) as error:
            if time.monotonic() - self._frame_warning > 5:
                logger.warning('Frame de audio descartado: %s', type(error).__name__)
                self._frame_warning = time.monotonic()
            return
        violation_triggered = False
        for result in results:
            if CALIBRANDO:
                if result['speech'] is False and not result['voice_protected']:
                    with TELEMETRY.lock:
                        BUFFER_CALIBRACION.append(result['band_rms'])
                        CALIBRATION_USERS.setdefault(user.id, []).append(result['band_rms'])
                engine.reset_detection()
                result.update(status='CALIBRANDO', violation=0, reason='Midiendo el fondo: castigos suspendidos.')
            elif user.id in config['WHITELIST']:
                engine.reset_detection()
                result.update(status='INMUNE', violation=0, reason='Usuario protegido.')
            elif user.id in CASTIGOS_ACTIVOS:
                engine.reset_detection()
                result.update(status='CASTIGADO', violation=0, reason='Castigo en curso.')
            self.viol[user.id], self.cont[user.id] = result['violation'], engine.noise_frames
            violation_triggered = violation_triggered or result['violation'] > config['CASTIGO_MAX']
            TELEMETRY.audio(user, result['rms'], result['status'], result['violation'], config['CASTIGO_MAX'],
                **{key: value for key, value in result.items() if key not in ('rms', 'status', 'violation')})
        if not results or CALIBRANDO or user.id in config['WHITELIST']:
            return
        if violation_triggered and config['AUTO_CASTIGOS'] and user.id not in CASTIGOS_ACTIVOS:
            if user.id in CONFIG['WHITELIST'] or not CONFIG['AUTO_CASTIGOS']:
                engine.reset_detection()
                return
            now = time.time()
            # Mantener la protección V25: bloquear nuevos castigos durante 5 segundos.
            if now - COOLDOWN_CASTIGOS.get(user.id, 0) > 5:
                COOLDOWN_CASTIGOS[user.id] = now
                self.viol[user.id] = 0
                self.cont[user.id] = 0
                engine.reset_detection()
                CASTIGOS_ACTIVOS.add(user.id)
                try:
                    safe_submit(self.castigar(user))
                except RuntimeError:
                    CASTIGOS_ACTIVOS.discard(user.id)
                    logger.warning('Castigo descartado: Discord no está conectado.')

    async def castigar(self, member):
        muted_by_us = False
        key = None
        jail = orig = None
        try:
            if member.id in CONFIG['WHITELIST'] or not CONFIG['AUTO_CASTIGOS']:
                return
            if not member.voice or member.voice.mute:
                return
            orig = member.voice.channel
            duration = STORE.audio_settings(str(member.id))['mute_duration'] or CONFIG['MUTE_TIME']
            jail = discord.utils.get(member.guild.voice_channels, name='La Cárcel')
            key = (member.guild.id, member.id)
            existing = MUTE_TASKS.get(key)
            if existing is not None and existing is not asyncio.current_task() and not existing.done():
                return
            MUTE_TASKS[key] = asyncio.current_task()
            await member.edit(mute=True, reason='Sentinel: acumulación de ruido')
            muted_by_us = True
            await registrar_mute(member.guild.id, member.id, duration,
                                 jail_id=jail.id if jail else None,
                                 orig_id=orig.id if orig else None)
            await _database_operation(STORE.increment_stat, str(member.id))
            await _log_event_async(f'{member.display_name}: silenciado durante {duration} segundos.', 'warning')
            if jail:
                try:
                    await member.move_to(jail, reason='Sentinel: pausa por ruido')
                except discord.HTTPException:
                    await _log_event_async('No se pudo mover a La Cárcel; el mute permanece activo.', 'warning')
            channel = self.bot.get_channel(self.logs_id)
            if channel:
                try:
                    await channel.send(embed=discord.Embed(title='Silenciado por ruido',
                        description=f'{member.mention} · {duration} segundos', color=0xf3aa55))
                except discord.HTTPException:
                    await _log_event_async('No se pudo escribir en el canal de registros.', 'warning')
            await asyncio.sleep(duration)
        except discord.Forbidden:
            await _log_event_async('Discord rechazó el castigo: revisa permisos y jerarquía de roles.', 'error')
        except Exception as error:
            await _log_event_async(f'Error durante el castigo: {type(error).__name__}', 'error')
        finally:
            try:
                if muted_by_us and member.voice:
                    # Si alguien lo movió manualmente, no deshacer ese movimiento.
                    if jail and orig and member.voice.channel == jail:
                        try:
                            await member.move_to(orig, reason='Sentinel: fin de pausa')
                        except discord.HTTPException:
                            await _log_event_async('No se pudo devolver al canal original.', 'warning')
                    await member.edit(mute=False, reason='Sentinel: fin de castigo')
                    await liberar_mute_journal(member.guild.id, member.id)
                    await _log_event_async(f'{member.display_name}: liberado.')
            except discord.HTTPException:
                await _log_event_async('No se pudo retirar el mute; requiere revisión manual.', 'error')
            except (OSError, ValueError, sqlite3.Error) as error:
                await _log_event_async(f'No se pudo cerrar el journal de mute: {type(error).__name__}.', 'error')
            finally:
                owns_mute = key is None or MUTE_TASKS.get(key) in (None, asyncio.current_task())
                if key is not None and MUTE_TASKS.get(key) is asyncio.current_task():
                    MUTE_TASKS.pop(key, None)
                if owns_mute:
                    CASTIGOS_ACTIVOS.discard(member.id)
                if muted_by_us:
                    # Conserva y reintenta registros cuyo mute o guardado falló.
                    await reconciliar_mutes_pendientes()

    def cleanup(self):
        with self._engine_lock:
            self.engines.clear()
            self.viol.clear()
            self.cont.clear()


def _load_audio_detector(source_signature):
    module_name = f'_sentinel_audio_live_{source_signature[0]}_{source_signature[1]}'
    spec = importlib.util.spec_from_file_location(module_name, AUDIO_SOURCE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError('No se pudo preparar el nuevo motor de audio.')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine_class = getattr(module, 'AudioEngine', None)
    if engine_class is None or not callable(getattr(engine_class, 'migrate_state_from', None)):
        raise TypeError('El nuevo motor no implementa migrate_state_from().')
    return module


async def _apply_audio_detector_update(source_signature):
    global AudioEngine, calibrated_threshold, AUDIO_SOURCE_SIGNATURE
    async with AUDIO_RELOAD_LOCK:
        if source_signature == AUDIO_SOURCE_SIGNATURE:
            return
        module = await asyncio.to_thread(_load_audio_detector, source_signature)
        engine_class = module.AudioEngine
        calibration_function = getattr(module, 'calibrated_threshold', None)
        if not callable(calibration_function):
            raise TypeError('El nuevo motor no incluye calibrated_threshold().')
        active_sinks = []
        for client in bot.voice_clients:
            reader = getattr(client, '_reader', None)
            sink = getattr(reader, 'sink', None)
            if not isinstance(sink, VigilanteRuido):
                continue
            if sink not in active_sinks:
                active_sinks.append(sink)
        # Pause sink writes for the short state swap. This keeps an audio
        # callback from creating an old-version engine midway through reload.
        with ExitStack() as locks:
            for sink in active_sinks:
                locks.enter_context(sink._engine_lock)
            staged = []
            for sink in active_sinks:
                replacements = {}
                for identity, previous in list(sink.engines.items()):
                    replacement = engine_class()
                    replacement.migrate_state_from(previous)
                    replacements[identity] = replacement
                staged.append((sink, replacements))
            # Swap only engine objects. The Discord voice client and its audio
            # reader remain connected and listening throughout the update.
            for sink, replacements in staged:
                sink.engines.update(replacements)
            AudioEngine = engine_class
            calibrated_threshold = calibration_function
        AUDIO_SOURCE_SIGNATURE = source_signature
        await _log_event_async(
            'Motor de audio actualizado en caliente; conexión de voz conservada.')


async def watch_audio_detector_source():
    """Apply stable sentinel_audio.py edits without disconnecting from voice."""
    observed = AUDIO_SOURCE_SIGNATURE
    pending = None
    while not bot.is_closed():
        await asyncio.sleep(1)
        current = _audio_source_signature()
        if current is None or current == observed:
            pending = None
            continue
        if current != pending:
            pending = current
            continue
        try:
            await _apply_audio_detector_update(current)
            observed = current
        except Exception as error:
            observed = current
            await _log_event_async(
                f'Actualización del motor de audio rechazada; sigue activa la versión anterior: {type(error).__name__}.',
                'warning')
        pending = None


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)


def _senalizacion_saludable(vc):
    ws = getattr(vc, 'ws', None)
    if not vc.is_connected() or ws is None:
        return False
    # discord.py 2.7.1 expone el cierre en ws.ws.closed, no en is_closed.
    closed = getattr(ws, 'is_closed', None)
    if closed is None:
        closed = getattr(getattr(ws, 'ws', None), 'closed', True)
    if callable(closed):
        closed = closed()
    if closed:
        return False
    heartbeat = getattr(ws, '_keep_alive', None)
    # latencia finita prueba un ACK; su antigüedad detecta sockets abiertos
    # que dejaron de responder. _last_ack usa perf_counter en discord.py.
    return (heartbeat is not None and math.isfinite(ws.latency)
            and time.perf_counter() - heartbeat._last_ack <= 30)


async def watchdog():
    global ULTIMO_LATIDO
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(10)
        if bot.voice_clients:
            vc = bot.voice_clients[0]
            if vc.channel and len(vc.channel.members) > 1:
                if time.time() - ULTIMO_LATIDO > 30:
                    if _senalizacion_saludable(vc):
                        ULTIMO_LATIDO = time.time()  # Silencio DTX, señalización viva.
                        continue
                    await _log_event_async(f'WATCHDOG: {time.time() - ULTIMO_LATIDO:.1f} segundos sin audio. Reiniciando...', 'error')
                    os.execv(sys.executable, ['python'] + sys.argv) # NUCLEAR REBOOT


@bot.event
async def on_ready():
    global WATCHDOG_TASK, AUDIO_RELOAD_TASK
    print('SENTINEL V26 ONLINE', flush=True)
    await _log_event_async(f'Discord conectado: {bot.user.name}.')
    await reconciliar_mutes_pendientes()
    if WATCHDOG_TASK is None or WATCHDOG_TASK.done():
        WATCHDOG_TASK = bot.loop.create_task(watchdog())
    loop = getattr(bot, 'loop', None)
    if (not PREVIEW and loop is not None
            and (AUDIO_RELOAD_TASK is None or AUDIO_RELOAD_TASK.done())):
        AUDIO_RELOAD_TASK = loop.create_task(watch_audio_detector_source())
    channel_id = os.getenv('SENTINEL_RESUME_CHANNEL')
    if channel_id and channel_id.isdecimal() and not bot.voice_clients:
        channel = bot.get_channel(int(channel_id))
        if isinstance(channel, discord.VoiceChannel):
            try:
                await connect_audio(channel)
                await _log_event_async(f'Escucha recuperada en {channel.name} después del reinicio.')
            except Exception as error:
                await _log_event_async(f'No se pudo recuperar la escucha: {type(error).__name__}. Usa !vigilar.', 'warning')
    # Si es un arranque normal y no hay canal guardado, comenzar a vigilar
    # donde ya haya personas conectadas. Ignorar bots para evitar canales
    # ocupados únicamente por integraciones.
    if CONFIG.get('AUTO_UNIR_VOZ', True) and not channel_id and not bot.voice_clients:
        for guild in getattr(bot, 'guilds', ()):
            voice_channels = [*getattr(guild, 'voice_channels', ()),
                              *getattr(guild, 'stage_channels', ())]
            channel = next((voice_channel for voice_channel in voice_channels
                            if any(not member.bot for member in voice_channel.members)), None)
            if channel is None:
                continue
            await _auto_join_voice_channel(channel, 'Inicio automático')
            break


@bot.event
async def on_voice_state_update(member, before, after):
    guild_id = member.guild.id
    bot_id = getattr(getattr(bot, 'user', None), 'id', None)
    is_sentinel = member.id == bot_id
    if is_sentinel and getattr(after, 'mute', False):
        try:
            await member.edit(mute=False)
        except discord.HTTPException:
            await _log_event_async('No se pudo retirar el mute del bot.', 'error')

    protected = (SALIDA_AUTORIZADA or guild_id in VOICE_JOINING
                 or guild_id in VOICE_RESTARTING or guild_id in VOICE_REJOINING
                 or guild_id in VOICE_LEAVE_AUTHORIZED)
    if is_sentinel and before.channel != after.channel and after.channel and not protected:
        # Un moderador puede mover al bot directamente. Si la conexión o el
        # receptor no siguieron el cambio, reutiliza el cliente existente.
        VOICE_REJOINING.add(guild_id)
        try:
            current = member.guild.voice_client
            if not (current and current.is_connected() and current.channel == after.channel
                    and current.is_listening()):
                await connect_audio(after.channel)
                await _log_event_async(
                    f'DEFENSA: Sentinel siguió el traslado a {after.channel.name} sin reiniciar el proceso.')
        except Exception as error:
            await _log_event_async(
                f'No se pudo seguir el traslado a {after.channel.name}: {type(error).__name__}.', 'error')
        finally:
            VOICE_REJOINING.discard(guild_id)

    if (is_sentinel and before.channel and not after.channel and not protected
            and CONFIG.get('AUTO_REENTRAR_VOZ', True)):
        VOICE_REJOINING.add(guild_id)
        try:
            channel = _select_voice_rejoin_channel(member.guild, before.channel)
            if channel is None:
                await _log_event_async(
                    'DEFENSA: Sentinel fue desconectado, pero no hay un canal con personas al que volver.',
                    'warning')
                return
            await _log_event_async(
                f'DEFENSA: desconexión inesperada detectada; volviendo a {channel.name}.', 'warning')
            # Empezar inmediatamente; los intentos siguientes dejan un margen
            # corto para que Discord limpie la sesión expulsada.
            delays = (0, .35, .75)
            last_error = None
            for delay in delays:
                if delay:
                    await asyncio.sleep(delay)
                current = member.guild.voice_client
                if (current and current.is_connected() and current.channel == channel
                        and current.is_listening()):
                    return
                try:
                    await connect_audio(channel)
                    await _log_event_async(f'DEFENSA: Sentinel volvió a {channel.name} sin reiniciar el proceso.')
                    return
                except Exception as error:
                    last_error = error
            await _log_event_async(
                f'No se pudo volver a {channel.name}: {type(last_error).__name__ if last_error else "Error"}.',
                'error')
        finally:
            VOICE_REJOINING.discard(guild_id)

    if (is_sentinel and before.channel and not after.channel and not protected
            and not CONFIG.get('AUTO_REENTRAR_VOZ', True)):
        await _log_event_async(
            'Autorreconexión desactivada: Sentinel permanecerá fuera del canal de voz.',
            'info')

    # Si Sentinel está desconectado, entra cuando una persona llega a voz.
    # También traslada una conexión inactiva si la persona llega a otro canal.
    if (not is_sentinel and not getattr(member, 'bot', False)
            and before.channel is None and after.channel
            and CONFIG.get('AUTO_UNIR_VOZ', True)):
        await _auto_join_voice_channel(after.channel, 'Entrada automática')

    # Seguir a las personas cuando se cambia de canal el grupo completo. Si
    # quedan usuarios en el canal vigilado, Sentinel permanece allí.
    if (not is_sentinel and not getattr(member, 'bot', False)
            and before.channel and after.channel and before.channel != after.channel):
        current = member.guild.voice_client
        recent = VOICE_RECENT_MOVE_TARGETS.get(guild_id)
        recent_chain = (recent and recent[0] == before.channel
                        and time.monotonic() - recent[1] <= VOICE_RECENT_MOVE_TTL)
        if current and current.channel == before.channel:
            VOICE_RECENT_MOVE_TARGETS[guild_id] = (after.channel, time.monotonic())
            if current.is_connected():
                _queue_voice_follow(member.guild, before.channel, after.channel)
        elif recent_chain:
            # Si una persona cambia de nuevo durante la espera, conserva el
            # último destino para no seguir un canal que ya quedó vacío.
            VOICE_RECENT_MOVE_TARGETS[guild_id] = (after.channel, time.monotonic())
            pending = VOICE_PENDING_FOLLOWS.get(guild_id)
            task = VOICE_FOLLOW_TASKS.get(guild_id)
            if ((pending and pending[0] is member.guild and pending[2] == before.channel)
                    or (task and not task.done())):
                # Durante move_to la cola se vacía mientras el traslado sigue
                # pendiente. Aun así, captura este nuevo destino para que el
                # seguidor no termine moviéndose a una sala intermedia.
                current = member.guild.voice_client
                source = current.channel if current and current.channel else before.channel
                _queue_voice_follow(member.guild, source, after.channel)


def _human_count(channel):
    return sum(not getattr(member, 'bot', False) for member in getattr(channel, 'members', ()))


async def _auto_join_voice_channel(channel, event_name):
    guild = channel.guild
    guild_id = guild.id
    if (not CONFIG.get('AUTO_UNIR_VOZ', True)
            or guild_id in VOICE_AUTO_JOIN_SUPPRESSED
            or guild_id in VOICE_JOINING or guild_id in VOICE_REJOINING
            or guild_id in VOICE_RESTARTING or guild_id in VOICE_LEAVE_AUTHORIZED):
        return False

    current = guild.voice_client
    if current and current.is_connected():
        if current.channel == channel and current.is_listening():
            return False
        if current.channel != channel and _human_count(current.channel):
            return False

    try:
        await connect_audio(channel)
        await _log_event_async(f'{event_name}: Sentinel está escuchando {channel.name}.')
        return True
    except Exception as error:
        await _log_event_async(
            f'No se pudo iniciar automáticamente en {channel.name}: {type(error).__name__}.',
            'warning')
        return False


async def _auto_join_occupied_channels():
    """When enabled in the dashboard, join occupied channels right away."""
    if not CONFIG.get('AUTO_UNIR_VOZ', True):
        return
    for guild in getattr(bot, 'guilds', ()):
        channels = [*getattr(guild, 'voice_channels', ()),
                    *getattr(guild, 'stage_channels', ())]
        channel = next((item for item in channels if _human_count(item)), None)
        if channel is not None:
            await _auto_join_voice_channel(channel, 'Entrada automática activada')


async def _connect_voice_to_occupied_channel():
    """Join an occupied voice channel on explicit request from the dashboard."""
    connected_client = next((client for client in getattr(bot, 'voice_clients', ())
                             if client.is_connected() and client.channel), None)
    if connected_client:
        return {'ok': True, 'channel': connected_client.channel.name, 'already_connected': True}

    channels = []
    for guild in getattr(bot, 'guilds', ()):
        channels.extend([*getattr(guild, 'voice_channels', ()),
                         *getattr(guild, 'stage_channels', ())])
    occupied = sorted((channel for channel in channels if _human_count(channel)),
                      key=_human_count, reverse=True)
    if not occupied:
        return {'ok': False, 'error': 'No hay nadie en un canal de voz. Entra a uno y vuelve a intentarlo.'}

    last_error = None
    for channel in occupied:
        try:
            await connect_audio(channel)
            await _log_event_async(f'Conexión manual desde la web: Sentinel está escuchando {channel.name}.')
            return {'ok': True, 'channel': channel.name}
        except Exception as error:
            last_error = error
    await _log_event_async(
        f'No se pudo conectar desde la web: {type(last_error).__name__ if last_error else "Error"}.',
        'error')
    return {'ok': False, 'error': 'No se pudo conectar. Revisa que Sentinel tenga permisos para ver y conectar al canal.'}


def _recent_occupied_move_target(guild):
    recent = VOICE_RECENT_MOVE_TARGETS.get(guild.id)
    if recent:
        channel, moved_at = recent
        if time.monotonic() - moved_at <= VOICE_RECENT_MOVE_TTL and _human_count(channel):
            return channel
        VOICE_RECENT_MOVE_TARGETS.pop(guild.id, None)
    return None


def _select_voice_rejoin_channel(guild, previous_channel):
    """Prefer the last occupied channel, then another occupied voice channel."""
    if previous_channel and _human_count(previous_channel):
        return previous_channel
    recent_target = _recent_occupied_move_target(guild)
    if recent_target:
        return recent_target

    channels = [*getattr(guild, 'voice_channels', ()), *getattr(guild, 'stage_channels', ())]
    occupied = [channel for channel in channels if _human_count(channel)]
    return max(occupied, key=_human_count, default=None)


def _queue_voice_follow(guild, source_channel, target_channel):
    guild_id = guild.id
    VOICE_PENDING_FOLLOWS[guild_id] = (guild, source_channel, target_channel)
    task = VOICE_FOLLOW_TASKS.get(guild_id)
    if task is None or task.done():
        VOICE_FOLLOW_TASKS[guild_id] = asyncio.create_task(_follow_voice_moves(guild_id))


async def _follow_voice_moves(guild_id):
    """Coalesce rapid moves and follow the latest occupied channel."""
    recovery_attempts = 0
    follow_latest = False
    try:
        while True:
            await asyncio.sleep(VOICE_FOLLOW_DEBOUNCE)
            pending = VOICE_PENDING_FOLLOWS.pop(guild_id, None)
            if pending is None:
                return
            guild, _, target_channel = pending
            current = guild.voice_client
            connected = bool(current and current.is_connected() and current.channel)
            source_channel = current.channel if connected else None
            # La llamada anterior pudo haber movido ya a Sentinel mientras
            # llegaban más eventos. Usa siempre su ubicación real, no el origen
            # antiguo que quedó guardado en la cola.
            if connected and source_channel == target_channel:
                if not current.is_listening():
                    await connect_audio(target_channel)
                continue
            if connected and _human_count(source_channel) and not follow_latest:
                # Si alguien sigue en el canal vigilado, se mantiene allí.
                VOICE_PENDING_FOLLOWS.pop(guild_id, None)
                continue
            latest_target = _recent_occupied_move_target(guild)
            if latest_target and latest_target != source_channel:
                target_channel = latest_target
            if (target_channel is None or target_channel == source_channel
                    or not _human_count(target_channel)):
                continue

            VOICE_REJOINING.add(guild_id)
            try:
                await connect_audio(target_channel)
            except Exception as error:
                # Si el grupo volvió a cambiar durante move_to, abandona el
                # destino vencido y procesa el último canal que siga ocupado.
                queued = VOICE_PENDING_FOLLOWS.get(guild_id)
                latest_target = _recent_occupied_move_target(guild)
                if queued and queued[2] and _human_count(queued[2]):
                    latest_target = queued[2]
                current = guild.voice_client
                if latest_target and latest_target != target_channel:
                    source = current.channel if current and current.channel else target_channel
                    VOICE_PENDING_FOLLOWS[guild_id] = (guild, source, latest_target)
                    recovery_attempts = 0
                    follow_latest = True
                    continue
                still_needs_move = (current and target_channel and _human_count(target_channel)
                                    and (not current.is_connected()
                                         or (current.channel != target_channel
                                             and not _human_count(current.channel))))
                if still_needs_move and recovery_attempts < 1:
                    # Un timeout de move_to no siempre significa que Discord
                    # haya cancelado el cambio. Reintenta una sola vez sobre el
                    # mismo cliente; nunca abras otra sesión en paralelo.
                    recovery_attempts += 1
                    VOICE_PENDING_FOLLOWS[guild_id] = (
                        guild, current.channel if current.channel else target_channel, target_channel)
                    await asyncio.sleep(.15)
                    continue
                await _log_event_async(
                    f'No se pudo seguir al grupo a {target_channel.name}: {type(error).__name__}.', 'error')
                return
            finally:
                VOICE_REJOINING.discard(guild_id)

            current = guild.voice_client
            if not (current and current.is_connected() and current.channel):
                return
            recovery_attempts = 0
            queued = VOICE_PENDING_FOLLOWS.get(guild_id)
            latest_target = _recent_occupied_move_target(guild)
            if queued and queued[2] and _human_count(queued[2]):
                latest_target = queued[2]
            if latest_target and latest_target != current.channel and _human_count(latest_target):
                VOICE_PENDING_FOLLOWS[guild_id] = (guild, current.channel, latest_target)
                # Hubo otro cambio mientras Discord confirmaba el traslado.
                # Sigue el destino nuevo aunque la caché aún muestre miembros
                # en la sala intermedia.
                follow_latest = True
                continue
            if _human_count(current.channel):
                # Si queda alguien y no se detectó un movimiento más reciente,
                # Sentinel permanece en el canal vigilado.
                VOICE_PENDING_FOLLOWS.pop(guild_id, None)
                return
            VOICE_PENDING_FOLLOWS.pop(guild_id, None)
            return
    finally:
        task = asyncio.current_task()
        if VOICE_FOLLOW_TASKS.get(guild_id) is task:
            VOICE_FOLLOW_TASKS.pop(guild_id, None)
            if guild_id in VOICE_PENDING_FOLLOWS:
                pending = VOICE_PENDING_FOLLOWS[guild_id]
                VOICE_FOLLOW_TASKS[guild_id] = asyncio.create_task(_follow_voice_moves(guild_id))


def _cancel_voice_follow(guild_id):
    VOICE_PENDING_FOLLOWS.pop(guild_id, None)
    task = VOICE_FOLLOW_TASKS.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


async def connect_audio(channel):
    """Conecta o mueve Sentinel, conservando el cliente de voz y su receptor."""
    global ULTIMO_LATIDO
    async with VOICE_CONNECT_LOCK:
        guild = channel.guild
        guild_id = guild.id
        existing = guild.voice_client
        if guild_id in VOICE_FOLLOW_TASKS:
            channel = _latest_voice_follow_target(guild, channel)
        permissions = channel.permissions_for(channel.guild.me)
        if not permissions.view_channel or not permissions.connect:
            raise PermissionError('Faltan permisos Ver canal o Conectar')
        VOICE_JOINING.add(guild_id)
        vc = None
        try:
            def stopped(error):
                if error:
                    log_event(f'Receptor de audio detenido: {type(error).__name__}. El watchdog supervisará la recuperación.', 'error')

            if existing and (existing.is_connected() or guild_id in VOICE_FOLLOW_TASKS):
                vc = existing
                if not existing.is_connected() or existing.channel != channel:
                    await existing.move_to(channel, timeout=VOICE_MOVE_TIMEOUT)
                    if not existing.is_connected() or existing.channel != channel:
                        latest = _latest_voice_follow_target(guild, channel)
                        if guild_id in VOICE_FOLLOW_TASKS and latest != channel:
                            # Cambió el grupo durante el traslado: vuelve a
                            # apuntar el mismo cliente al destino final, sin
                            # abrir una segunda sesión de voz en paralelo.
                            channel = latest
                            await existing.move_to(channel, timeout=VOICE_MOVE_TIMEOUT)
                        if not existing.is_connected() or existing.channel != channel:
                            raise TimeoutError(
                                f'Discord no confirmó el traslado a {channel.name} '
                                f'en {VOICE_MOVE_TIMEOUT:g} s.')
                if not existing.is_listening():
                    existing.listen(VigilanteRuido(bot, CANAL_LOGS_ID), after=stopped)
            if not existing or not existing.is_connected():
                if existing:
                    # Fuera de un seguimiento de canal, esta es una desconexión
                    # real (kick, salida o fallo total): espera la confirmación
                    # normal de Discord antes de abrir un cliente nuevo.
                    await existing.disconnect(force=True)
                ULTIMO_LATIDO = time.time()
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
                vc.listen(VigilanteRuido(bot, CANAL_LOGS_ID), after=stopped)
            ULTIMO_LATIDO = time.time()
            os.environ['SENTINEL_RESUME_CHANNEL'] = str(channel.id)
            await _log_event_async(f'Monitoreando {channel.name}.')
            return vc
        except Exception:
            # Si se reutilizó el cliente conectado, no destruirlo ante un fallo
            # al moverlo o al reanudar el listener.
            if vc and vc is not existing:
                await vc.disconnect(force=True)
            raise
        finally:
            VOICE_JOINING.discard(guild_id)


def _latest_voice_follow_target(guild, fallback):
    """Choose the newest occupied target while a follow task is active."""
    pending = VOICE_PENDING_FOLLOWS.get(guild.id)
    if pending and pending[2] and _human_count(pending[2]):
        return pending[2]
    recent = _recent_occupied_move_target(guild)
    return recent or fallback


async def restart_process(client):
    """Restart the bot process and resume its current voice channel on startup."""
    channel = client.channel
    guild_id = channel.guild.id
    VOICE_RESTARTING.add(guild_id)
    try:
        os.environ['SENTINEL_RESUME_CHANNEL'] = str(channel.id)
        await _log_event_async(f'Reinicio solicitado desde el panel; Sentinel volverá a {channel.name}.', 'warning')
        if client.is_connected():
            await client.disconnect(force=True)
        await asyncio.sleep(.5)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception as error:
        await _log_event_async(f'No se pudo reiniciar Sentinel: {type(error).__name__}.', 'error')
    finally:
        VOICE_RESTARTING.discard(guild_id)


async def _clear_authorized_voice_leave(guild_id):
    # Keep the marker briefly because Discord may deliver the state event
    # after the command's disconnect call has already returned.
    await asyncio.sleep(5)
    VOICE_LEAVE_AUTHORIZED.discard(guild_id)


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(mute_members=True)
async def vigilar(ctx):
    global ULTIMO_LATIDO
    if not ctx.author.voice:
        await ctx.send('Entra a un canal de voz antes de usar !vigilar.')
        return
    if ctx.voice_client and ctx.voice_client.is_connected() and ctx.voice_client.is_listening():
        await ctx.send('Ya estoy conectado y seguiré los cambios de canal. Usa !salir para detenerme.')
        return
    try:
        permissions = ctx.author.voice.channel.permissions_for(ctx.guild.me)
        if not permissions.view_channel or not permissions.connect:
            await ctx.send('Necesito los permisos Ver canal y Conectar en tu canal de voz.')
            return
        VOICE_AUTO_JOIN_SUPPRESSED.discard(ctx.guild.id)
        await connect_audio(ctx.author.voice.channel)
        await ctx.send('Sentinel activado. Panel: http://localhost:5000')
    except Exception as error:
        await _log_event_async(f'No se pudo iniciar la escucha: {type(error).__name__}', 'error')
        await ctx.send('No pude escuchar el canal. Revisa los permisos y la consola.')


@bot.command()
@commands.guild_only()
@commands.has_guild_permissions(mute_members=True)
async def salir(ctx):
    global SALIDA_AUTORIZADA
    os.environ.pop('SENTINEL_RESUME_CHANNEL', None)
    VOICE_AUTO_JOIN_SUPPRESSED.add(ctx.guild.id)
    _cancel_voice_follow(ctx.guild.id)
    VOICE_RECENT_MOVE_TARGETS.pop(ctx.guild.id, None)
    if ctx.voice_client:
        VOICE_LEAVE_AUTHORIZED.add(ctx.guild.id)
        SALIDA_AUTORIZADA = True
        try:
            await ctx.voice_client.disconnect()
            await _log_event_async('Monitoreo detenido por un moderador.')
        finally:
            SALIDA_AUTORIZADA = False
            asyncio.create_task(_clear_authorized_voice_leave(ctx.guild.id))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send('Necesitas el permiso Silenciar miembros para controlar Sentinel.')
    else:
        await _log_event_async(f'Comando fallido: {type(error).__name__}', 'error')


def broadcast_state():
    while True:
        try:
            snapshot = runtime_snapshot()
            snapshot.pop('events', None)  # El historial viaja al conectar; los nuevos eventos, por separado.
            socketio.emit('state_update', snapshot)
        except Exception as error:
            logger.warning('No se pudo emitir telemetría: %s', type(error).__name__)
        socketio.sleep(0.1)  # 10 actualizaciones por segundo, independiente del DSP.


def prepare_opus():
    if discord.opus.is_loaded():
        return
    path = ROOT / 'libopus.dll'
    architecture = 'x64' if struct.calcsize('P') == 8 else 'x86'
    bundled = Path(discord.__file__).parent / 'bin' / f'libopus-0.{architecture}.dll'
    if sys.platform == 'win32' and bundled.exists():
        if path.exists() and path.read_bytes()[:2] != b'MZ':
            backup = ROOT / 'backups' / 'libopus_descarga_invalida.html'
            backup.parent.mkdir(exist_ok=True)
            shutil.copy2(path, backup)
            shutil.copy2(bundled, path)
            logger.info('Opus reparado con la DLL incluida en discord.py (%s).', architecture)
        discord.opus.load_opus(str(bundled))
        return
    if not path.exists() or path.read_bytes()[:2] != b'MZ':
        response = requests.get(f'https://raw.githubusercontent.com/Rapptz/discord.py/master/discord/bin/libopus-0.{architecture}.dll', timeout=20)
        response.raise_for_status()
        if not response.content.startswith(b'MZ'):
            raise RuntimeError('La descarga de libopus no es una DLL válida.')
        path.write_bytes(response.content)
    discord.opus.load_opus(str(path))


def demo_audio():
    from types import SimpleNamespace
    users = [SimpleNamespace(id=100000000000000001, name='Usuario demo', display_name='Usuario demo'),
             SimpleNamespace(id=100000000000000002, name='Alex demo', display_name='Alex demo'),
             SimpleNamespace(id=100000000000000003, name='Martina demo', display_name='Martina demo')]
    log_event('Demostración local. Estos datos son simulados; no hay conexión a Discord.')
    while True:
        now = time.time()
        for index, user in enumerate(users):
            rms = int(180 + abs(math.sin(now * 1.8 + index * 2)) ** 5 * (1900 if index == 1 else 950))
            threshold = 220 if CONFIG['AUTO_SENSITIVITY'] else CONFIG['UMBRAL_RMS']
            status = 'RUIDO' if index == 1 and rms > threshold else 'VOZ' if rms > threshold else 'SILENCIO'
            TELEMETRY.audio(user, rms, status, 34 if status == 'RUIDO' else 0, CONFIG['CASTIGO_MAX'],
                threshold=threshold, noise_floor=100, dbfs=round(20 * math.log10(max(rms, 1) / 32768), 1),
                reason='Audio simulado; no hay castigos de Discord.', clipping=0, continuous_seconds=0)
            if CALIBRANDO:
                with TELEMETRY.lock:
                    BUFFER_CALIBRACION.append(140 + int(abs(math.sin(now)) * 30))
        socketio.sleep(0.1)


def create_http_server():
    host = os.getenv('SENTINEL_HOST', '127.0.0.1')
    port = int(os.getenv('SENTINEL_PORT', '5000'))
    server = make_server(host, port, app, threaded=True)
    # run_simple marca el socket heredable para su reloader. Sentinel no usa
    # reloader: heredar ese socket con execv deja el puerto ocupado en Windows.
    server.socket.set_inheritable(False)
    return server


def serve(server=None):
    server = server or create_http_server()
    logger.info('Panel disponible en http://%s:%s', server.server_address[0], server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    try:
        INSTANCE_GUARD = acquire_instance(ROOT / ('.sentinel-demo.lock' if PREVIEW else '.sentinel.lock'))
    except RuntimeError as error:
        raise SystemExit(str(error))
    if STORE.warning:
        log_event(STORE.warning, 'warning')
    socketio.start_background_task(broadcast_state)
    if PREVIEW:
        socketio.start_background_task(demo_audio)
        serve()
    else:
        if TOKEN == 'TU_TOKEN_AQUI' or not TOKEN:
            raise SystemExit('Configura DISCORD_TOKEN en el archivo .env.')
        try:
            prepare_opus()
        except Exception as error:
            raise SystemExit(f'No se pudo cargar libopus: {type(error).__name__}. Revisa la DLL o tu conexión.')
        install_voice_compat()
        http_server = create_http_server()
        Thread(target=serve, args=(http_server,), daemon=True).start()
        while True:
            try:
                bot.run(TOKEN)
                break
            except KeyboardInterrupt:
                sys.exit(0)
            except discord.LoginFailure:
                raise SystemExit('Token rechazado por Discord. Actualiza DISCORD_TOKEN en .env.')
            except discord.PrivilegedIntentsRequired:
                raise SystemExit('Activa Message Content Intent en Discord Developer Portal > Bot.')
            except Exception as error:
                logger.exception('Fallo no controlado del bot; se ejecutará el reinicio nuclear.')
                log_event(f'Crash detectado: {type(error).__name__}', 'error')
                time.sleep(5)
                os.execv(sys.executable, ['python'] + sys.argv)


