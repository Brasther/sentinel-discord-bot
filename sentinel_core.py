"""Configuración, DSP y telemetría sin conexiones a Discord."""
import collections
import copy
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
import time

import numpy as np
from scipy.signal import butter, sosfilt
import webrtcvad

DEFAULT_CONFIG = {
    'UMBRAL_RMS': 1000, 'CASTIGO_MAX': 50, 'TIEMPO_TOLERANCIA': 100,
    'MUTE_TIME': 10, 'WHITELIST': [], 'STATS': {}, 'AUTO_CASTIGOS': True,
    'AUTO_UNIR_VOZ': True,
    'AUTO_REENTRAR_VOZ': True,
    'AUTO_SENSITIVITY': True, 'AUDIO_MODE': 'noise', 'CONTINUOUS_SECONDS': 15,
}
LIMITS = {'UMBRAL_RMS': (5, 10000), 'CASTIGO_MAX': (10, 500),
          'TIEMPO_TOLERANCIA': (10, 1000), 'MUTE_TIME': (1, 300),
          'CONTINUOUS_SECONDS': (5, 120)}


def validate_setting(key, value):
    if key in ('AUTO_CASTIGOS', 'AUTO_SENSITIVITY', 'AUTO_UNIR_VOZ', 'AUTO_REENTRAR_VOZ'):
        if type(value) is not bool:
            raise ValueError('El modo automático debe ser verdadero o falso.')
        return value
    if key == 'AUDIO_MODE':
        if value not in ('noise', 'continuous'):
            raise ValueError('Modo de audio desconocido.')
        return value
    if key not in LIMITS:
        raise ValueError('Este ajuste no se puede modificar.')
    low, high = LIMITS[key]
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{key} debe ser un entero entre {low} y {high}.')
    return value


def discord_id(value):
    # El navegador solo recibe y envía cadenas, nunca números de 18 dígitos.
    if not isinstance(value, str) or not value.isdecimal() or not 1 <= int(value) < 2**64:
        raise ValueError('ID de Discord inválido. Escríbelo como texto.')
    return int(value)


def _pending_record(record):
    if not isinstance(record, dict):
        raise ValueError('Registro de mute inválido')
    result = {}
    for field in ('guild_id', 'user_id', 'jail_id', 'orig_id'):
        value = record.get(field)
        if value is None and field in ('jail_id', 'orig_id'):
            result[field] = None
        elif type(value) is int and 0 < value < 2**63:
            result[field] = value
        else:
            raise ValueError('Identidad o canal de mute inválido')
    deadline = record.get('unmute_at')
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError('Plazo de mute inválido')
    result['unmute_at'] = deadline
    return result


class SQLiteDatabase:
    """Una conexión compartida, serializada entre hilos; transacciones explícitas."""
    def __init__(self, path=':memory:', *, legacy_config=None, legacy_mutes=None):
        self.path = str(path)
        self.lock = RLock()
        self.warnings = []
        if self.path != ':memory:':
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None,
                                          check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute('PRAGMA busy_timeout = 5000')
            self.connection.execute('PRAGMA journal_mode = WAL')
            self.connection.execute('PRAGMA synchronous = NORMAL')
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS whitelist (user_id TEXT PRIMARY KEY, created_at REAL);
                CREATE TABLE IF NOT EXISTS stats (user_id TEXT PRIMARY KEY, punishments INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS user_sensitivity (
                    user_id TEXT PRIMARY KEY, sensitivity INTEGER NOT NULL, updated_at REAL);
                CREATE TABLE IF NOT EXISTS user_audio_settings (
                    user_id TEXT PRIMARY KEY, profile TEXT NOT NULL DEFAULT 'custom',
                    threshold_rms INTEGER NOT NULL DEFAULT 0, penalty_rate INTEGER NOT NULL DEFAULT 1,
                    grace_seconds INTEGER NOT NULL DEFAULT 2, mute_duration INTEGER NOT NULL DEFAULT 0,
                    protect_voice INTEGER NOT NULL DEFAULT 1, updated_at REAL);
                CREATE TABLE IF NOT EXISTS pending_mutes (
                    guild_id INTEGER, user_id INTEGER, unmute_at REAL,
                    jail_id INTEGER, orig_id INTEGER, PRIMARY KEY (guild_id, user_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, level TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS migrations (source TEXT PRIMARY KEY);
            ''')
            self.connection.execute('''INSERT OR IGNORE INTO user_audio_settings (user_id, profile, updated_at)
                SELECT user_id, 'custom', updated_at FROM user_sensitivity''')
            if legacy_config is not None:
                self._migrate_file(Path(legacy_config), self._import_config)
            if legacy_mutes is not None:
                self._migrate_file(Path(legacy_mutes), self._import_mutes)
            with self.transaction() as connection:
                connection.executemany('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
                    [(key, json.dumps(value)) for key, value in DEFAULT_CONFIG.items()
                     if key not in ('WHITELIST', 'STATS')])
        except BaseException:
            self.connection.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.execute('COMMIT')
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute('ROLLBACK')
                raise

    def close(self):
        with self.lock:
            self.connection.close()

    def _migrate_file(self, source, importer):
        if not source.exists():
            return
        identity = str(source.resolve())
        try:
            with self.transaction() as connection:
                imported = connection.execute('SELECT 1 FROM migrations WHERE source = ?', (identity,)).fetchone()
                if imported is None:
                    importer(connection, json.loads(source.read_text(encoding='utf-8-sig')))
                    connection.execute('INSERT INTO migrations (source) VALUES (?)', (identity,))
        except (ValueError, TypeError, AttributeError, OSError) as error:
            self.warnings.append(f'No se pudo migrar {source.name}: {error}')
            return
        # El marcador y los datos se confirman juntos. Si el proceso cae antes
        # del rename, el próximo arranque solo reintenta renombrar, sin importar.
        try:
            if source.exists():
                source.rename(source.with_suffix(source.suffix + '.migrated'))
        except OSError as error:
            self.warnings.append(f'Migración confirmada; no se pudo renombrar {source.name}: {error}')

    def _import_config(self, connection, saved):
        if not isinstance(saved, dict):
            raise ValueError('Configuración JSON inválida')
        settings = [(key, json.dumps(validate_setting(key, saved[key])))
                    for key in (*LIMITS, 'AUTO_CASTIGOS', 'AUTO_SENSITIVITY', 'AUTO_UNIR_VOZ',
                                'AUTO_REENTRAR_VOZ', 'AUDIO_MODE') if key in saved]
        whitelist = [(str(discord_id(str(identity))), time.time()) for identity in saved.get('WHITELIST', [])]
        stats = [(str(discord_id(str(identity))), count) for identity, count in saved.get('STATS', {}).items()
                 if type(count) is int and 0 <= count < 2**63]
        connection.executemany('''INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value''', settings)
        connection.executemany('INSERT OR IGNORE INTO whitelist (user_id, created_at) VALUES (?, ?)', whitelist)
        connection.executemany('INSERT OR IGNORE INTO stats (user_id, punishments) VALUES (?, ?)', stats)

    def _import_mutes(self, connection, saved):
        if not isinstance(saved, dict):
            raise ValueError('Journal JSON inválido')
        records = [_pending_record(record) for record in saved.values()]
        for key, record in zip(saved, records):
            if key != f"{record['guild_id']}:{record['user_id']}":
                raise ValueError('Clave de mute inválida')
        connection.executemany('''INSERT OR IGNORE INTO pending_mutes
            (guild_id, user_id, unmute_at, jail_id, orig_id) VALUES (?, ?, ?, ?, ?)''',
            [(r['guild_id'], r['user_id'], r['unmute_at'], r['jail_id'], r['orig_id']) for r in records])

    def pending_mutes(self):
        with self.lock:
            return [dict(row) for row in self.connection.execute(
                'SELECT guild_id, user_id, unmute_at, jail_id, orig_id FROM pending_mutes ORDER BY guild_id, user_id')]

    def put_pending_mute(self, record):
        record = _pending_record(record)
        with self.transaction() as connection:
            connection.execute('''INSERT INTO pending_mutes (guild_id, user_id, unmute_at, jail_id, orig_id)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT (guild_id, user_id) DO UPDATE SET
                unmute_at = excluded.unmute_at, jail_id = excluded.jail_id, orig_id = excluded.orig_id''',
                (record['guild_id'], record['user_id'], record['unmute_at'], record['jail_id'], record['orig_id']))

    def delete_pending_mute(self, guild_id, user_id):
        with self.transaction() as connection:
            connection.execute('DELETE FROM pending_mutes WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))

    def recent_events(self):
        with self.lock:
            rows = self.connection.execute('SELECT id, timestamp, level, message FROM events ORDER BY id DESC LIMIT 200').fetchall()
        return [{'id': r['id'], 'time': r['timestamp'], 'level': r['level'], 'msg': r['message']} for r in reversed(rows)]

    def insert_event(self, message, level):
        timestamp = time.time()
        with self.transaction() as connection:
            cursor = connection.execute('INSERT INTO events (timestamp, level, message) VALUES (?, ?, ?)',
                                        (timestamp, str(level), str(message)))
            identity = cursor.lastrowid
        return {'id': identity, 'time': timestamp, 'level': str(level), 'msg': str(message)}


class ConfigStore:
    def __init__(self, path, *, legacy_config=None, legacy_mutes=None):
        # Aceptar también una ruta .json antigua, pero escribir solo en SQLite.
        if str(path) != ':memory:':
            path = Path(path)
            if path.suffix == '.json':
                legacy_config = legacy_config or path
                path = path.with_suffix('.db')
            legacy_config = legacy_config or path.parent / 'sentinel_data.json'
            legacy_mutes = legacy_mutes or path.parent / 'pending_mutes.json'
        self.path = path
        self.database = SQLiteDatabase(path, legacy_config=legacy_config, legacy_mutes=legacy_mutes)
        self.lock = self.database.lock
        self.warning = '; '.join(self.database.warnings) or None
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        try:
            with self.lock:
                for row in self.database.connection.execute('SELECT key, value FROM config'):
                    self.data[row['key']] = validate_setting(row['key'], json.loads(row['value']))
                self.data['WHITELIST'] = [discord_id(row['user_id']) for row in self.database.connection.execute(
                    'SELECT user_id FROM whitelist ORDER BY created_at, user_id')]
                self.data['STATS'] = {row['user_id']: row['punishments'] for row in self.database.connection.execute(
                    'SELECT user_id, punishments FROM stats')}
        except BaseException:
            self.database.close()
            raise

    def close(self):
        self.database.close()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.data)
            result['WHITELIST'] = [str(identity) for identity in result['WHITELIST']]
            return result

    def save(self):
        """Compatibilidad para consumidores que aún modifican data bajo lock."""
        with self.lock:
            settings = [(key, json.dumps(validate_setting(key, self.data[key])))
                        for key in DEFAULT_CONFIG if key not in ('WHITELIST', 'STATS')]
            whitelist = [(str(discord_id(str(identity))), time.time()) for identity in self.data['WHITELIST']]
            stats = [(str(discord_id(str(identity))), count) for identity, count in self.data['STATS'].items()]
            if any(type(count) is not int or not 0 <= count < 2**63 for _, count in stats):
                raise ValueError('Contador de castigos inválido')
            with self.database.transaction() as connection:
                connection.executemany('''INSERT INTO config (key, value) VALUES (?, ?)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value''', settings)
                connection.execute('DELETE FROM whitelist')
                connection.executemany('INSERT INTO whitelist (user_id, created_at) VALUES (?, ?)', whitelist)
                connection.execute('DELETE FROM stats')
                connection.executemany('INSERT INTO stats (user_id, punishments) VALUES (?, ?)', stats)

    def update(self, key, value):
        value = validate_setting(key, value)
        with self.lock:
            with self.database.transaction() as connection:
                connection.execute('UPDATE config SET value = ? WHERE key = ?', (json.dumps(value), key))
            self.data[key] = value  # La caché cambia solo después del COMMIT.

    def whitelist(self, identity, enabled):
        user_id = discord_id(identity)
        if type(enabled) is not bool:
            raise ValueError('La inmunidad debe ser verdadera o falsa.')
        with self.lock:
            with self.database.transaction() as connection:
                if enabled:
                    connection.execute('INSERT OR IGNORE INTO whitelist (user_id, created_at) VALUES (?, ?)',
                                       (str(user_id), time.time()))
                else:
                    connection.execute('DELETE FROM whitelist WHERE user_id = ?', (str(user_id),))
            if enabled and user_id not in self.data['WHITELIST']:
                self.data['WHITELIST'].append(user_id)
            elif not enabled and user_id in self.data['WHITELIST']:
                self.data['WHITELIST'].remove(user_id)

    def increment_stat(self, identity):
        identity = str(discord_id(str(identity)))
        with self.lock:
            with self.database.transaction() as connection:
                count = connection.execute('''INSERT INTO stats (user_id, punishments) VALUES (?, 1)
                    ON CONFLICT (user_id) DO UPDATE SET punishments = stats.punishments + 1
                    RETURNING punishments''', (identity,)).fetchone()[0]
            self.data['STATS'][identity] = count
            return count

    def sensitivities(self):
        with self.lock:
            return {row['user_id']: row['sensitivity'] for row in self.database.connection.execute(
                'SELECT user_id, sensitivity FROM user_sensitivity')}

    def set_sensitivity(self, identity, value):
        user_id = str(discord_id(identity))
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError('La sensibilidad debe ser un entero entre 0 y 100.')
        with self.database.transaction() as connection:
            connection.execute('''INSERT INTO user_sensitivity (user_id, sensitivity, updated_at)
                VALUES (?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET
                sensitivity = excluded.sensitivity, updated_at = excluded.updated_at''',
                (user_id, value, time.time()))
            connection.execute('''INSERT INTO user_audio_settings (user_id, profile, updated_at)
                VALUES (?, 'custom', ?) ON CONFLICT (user_id) DO UPDATE SET
                profile = 'custom', updated_at = excluded.updated_at''', (user_id, time.time()))
        return value

    def sensitivity(self, identity):
        user_id = str(discord_id(str(identity)))
        with self.lock:
            row = self.database.connection.execute(
                'SELECT sensitivity FROM user_sensitivity WHERE user_id = ?', (user_id,)).fetchone()
        return row['sensitivity'] if row else 50

    @staticmethod
    def default_audio_settings():
        return {'profile': 'normal', 'threshold_rms': 0, 'penalty_rate': 1,
                'grace_seconds': 2, 'mute_duration': 0, 'protect_voice': True,
                'sensitivity': 50}

    def audio_settings(self, identity):
        user_id = str(discord_id(str(identity)))
        result = self.default_audio_settings()
        with self.lock:
            row = self.database.connection.execute(
                'SELECT profile, threshold_rms, penalty_rate, grace_seconds, mute_duration, protect_voice '
                'FROM user_audio_settings WHERE user_id = ?', (user_id,)).fetchone()
            sensitivity = self.database.connection.execute(
                'SELECT sensitivity FROM user_sensitivity WHERE user_id = ?', (user_id,)).fetchone()
        if row:
            result.update(dict(row))
            result['protect_voice'] = bool(result['protect_voice'])
        if sensitivity:
            result['sensitivity'] = sensitivity['sensitivity']
        return result

    def set_audio_setting(self, identity, key, value):
        user_id = str(discord_id(identity))
        validators = {
            'threshold_rms': lambda v: type(v) is int and 0 <= v <= 10000,
            'penalty_rate': lambda v: type(v) is int and v in (1, 2, 3),
            'grace_seconds': lambda v: type(v) is int and 0 <= v <= 30,
            'mute_duration': lambda v: type(v) is int and 0 <= v <= 300,
            'protect_voice': lambda v: type(v) is bool,
        }
        if key not in validators or not validators[key](value):
            raise ValueError(f'Ajuste individual inválido: {key}.')
        with self.database.transaction() as connection:
            connection.execute('''INSERT INTO user_audio_settings (user_id, profile, updated_at)
                VALUES (?, 'custom', ?) ON CONFLICT (user_id) DO UPDATE SET
                profile = 'custom', updated_at = excluded.updated_at''', (user_id, time.time()))
            column_value = int(value) if key == 'protect_voice' else value
            connection.execute(f'UPDATE user_audio_settings SET {key} = ?, updated_at = ? WHERE user_id = ?',
                               (column_value, time.time(), user_id))
        return self.audio_settings(user_id)

    def set_audio_profile(self, identity, profile):
        user_id = str(discord_id(identity))
        presets = {
            'tolerant': (35, 0, 1, 5, 5, True),
            'normal': (50, 0, 1, 2, 0, True),
            'strict': (75, 0, 2, 0, 20, False),
        }
        if profile not in presets:
            raise ValueError('Perfil individual desconocido.')
        sensitivity, threshold, rate, grace, mute, protect = presets[profile]
        with self.database.transaction() as connection:
            connection.execute('''INSERT INTO user_audio_settings
                (user_id, profile, threshold_rms, penalty_rate, grace_seconds, mute_duration, protect_voice, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET
                profile = excluded.profile, threshold_rms = excluded.threshold_rms,
                penalty_rate = excluded.penalty_rate, grace_seconds = excluded.grace_seconds,
                mute_duration = excluded.mute_duration, protect_voice = excluded.protect_voice,
                updated_at = excluded.updated_at''',
                (user_id, profile, threshold, rate, grace, mute, int(protect), time.time()))
            connection.execute('''INSERT INTO user_sensitivity (user_id, sensitivity, updated_at)
                VALUES (?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET
                sensitivity = excluded.sensitivity, updated_at = excluded.updated_at''',
                (user_id, sensitivity, time.time()))
        return self.audio_settings(user_id)


class AudioFilter:
    def __init__(self, rate=48000, lowcut=300, highcut=4000, order=5):
        self.sos = butter(order, [lowcut, highcut], fs=rate, btype='band', output='sos')
        self.zi = np.zeros((len(self.sos), 2))

    def process(self, pcm):
        samples = np.frombuffer(pcm, dtype='<i2')
        if not samples.size:
            return b''
        filtered, self.zi = sosfilt(self.sos, samples, zi=self.zi)
        return np.clip(filtered, -32768, 32767).astype('<i2').tobytes()


class SpeechDetector:
    """VAD por usuario, con frames válidos de 10 ms a 48 kHz."""
    FRAME_BYTES = 960

    def __init__(self):
        self.vad = webrtcvad.Vad(2)
        self.pending = b''

    def is_speech(self, pcm):
        self.pending += pcm
        frames = []
        while len(self.pending) >= self.FRAME_BYTES:
            frame, self.pending = self.pending[:self.FRAME_BYTES], self.pending[self.FRAME_BYTES:]
            # Solo normalizar la entrada al VAD; nunca inflar el volumen medido.
            values = np.frombuffer(frame, dtype='<i2').astype(np.float64)
            level = float(np.sqrt(np.mean(values * values)))
            if level < 5.0:
                frames.append(False)
                continue
            if 40 <= level < 600:
                values *= min(4.0, 600.0 / max(level, 1.0))
                frame = np.clip(values, -32768, 32767).astype('<i2').tobytes()
            frames.append(self.vad.is_speech(frame, 48000))
        # Ningún frame completo: no asumir que es ruido y castigar.
        return any(frames) if frames else None


def score_audio(rms, speech, points, continuous, config):
    if rms < config['UMBRAL_RMS']:
        return max(0, points - 5), 0, 'SILENCIO'
    if speech is None:
        return points, continuous, 'ANALIZANDO'
    if speech:
        return max(0, points - 1), 0, 'VOZ'
    continuous += 1
    if continuous > config['TIEMPO_TOLERANCIA']:
        return points + 5, continuous, 'RUIDO CONTINUO'
    return points + 4, continuous, 'RUIDO'


class Telemetry:
    def __init__(self, database=None):
        self.database = database if database is not None else SQLiteDatabase()
        self._owns_database = database is None
        self.lock = RLock()
        self.started = time.time()
        self.users = {}
        self.events = collections.deque(self.database.recent_events(), maxlen=200)
        self.sequence = self.events[-1]['id'] if self.events else 0
        self.frames = 0
        self.calibration = {'active': False, 'progress': 0, 'samples': 0}

    def close(self):
        if self._owns_database:
            self.database.close()

    def audio(self, user, rms, status, points, limit, **diagnostics):
        with self.lock:
            self.frames += 1
            self.users[str(user.id)] = {
                'id': str(user.id), 'user': getattr(user, 'display_name', user.name),
                'rms': int(rms), 'status': status, 'violation': int(points),
                'limit': limit, 'last_seen': time.time(),
                **diagnostics,
            }

    def event(self, message, level='info'):
        with self.lock:
            record = self.database.insert_event(message, level)
            self.sequence = record['id']
            self.events.append(record)
            return record

    def snapshot(self, members=None):
        now = time.time()
        with self.lock:
            if members is not None:
                active = {str(member.id) for member in members if not member.bot}
                self.users = {key: value for key, value in self.users.items() if key in active}
                for member in members:
                    if not member.bot and str(member.id) not in self.users:
                        self.users[str(member.id)] = {'id': str(member.id), 'user': member.display_name,
                            'rms': 0, 'status': 'SILENCIO', 'violation': 0, 'limit': 50, 'last_seen': 0}
            users = copy.deepcopy(list(self.users.values()))
            for user in users:
                if now - user['last_seen'] > 1.5:
                    user['rms'] = 0
                    user.update(band_rms=0, peak=0, clipping=0, speech=False,
                                continuous_seconds=0, violation=0,
                                reason='Discord no ha enviado audio reciente.')
                    if user['status'] != 'INMUNE':
                        user['status'] = 'SILENCIO'
            return {'users': users, 'frames': self.frames, 'uptime': int(now - self.started),
                    'events': list(self.events), 'calibration': copy.deepcopy(self.calibration)}
