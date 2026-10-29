import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, get_ident
import unittest
from unittest.mock import patch

import main
from sentinel_core import ConfigStore, DEFAULT_CONFIG, Telemetry


class SQLitePersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'sentinel.db'

    def store(self, path=None, **kwargs):
        store = ConfigStore(path or self.path, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_required_schema_and_pragmas(self):
        store = self.store()
        connection = store.database.connection
        self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(connection.execute('PRAGMA synchronous').fetchone()[0], 1)
        self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({'config', 'whitelist', 'stats', 'pending_mutes', 'events', 'user_sensitivity'} <= tables)
        self.assertEqual(store.snapshot(), DEFAULT_CONFIG)

    def test_legacy_files_migrate_config_whitelist_stats_and_pending_mutes(self):
        legacy = self.root / 'sentinel_data.json'
        journal = self.root / 'pending_mutes.json'
        legacy.write_text(json.dumps({'MUTE_TIME': 25, 'AUTO_CASTIGOS': False,
            'WHITELIST': ['900000000000000001'], 'STATS': {'900000000000000001': 7}}), encoding='utf-8')
        record = {'guild_id': 1, 'user_id': 2, 'unmute_at': 100.0, 'jail_id': 11, 'orig_id': 10}
        journal.write_text(json.dumps({'1:2': record}), encoding='utf-8')
        store = self.store()
        self.assertEqual(store.snapshot()['MUTE_TIME'], 25)
        self.assertFalse(store.snapshot()['AUTO_CASTIGOS'])
        self.assertEqual(store.snapshot()['WHITELIST'], ['900000000000000001'])
        self.assertEqual(store.snapshot()['STATS'], {'900000000000000001': 7})
        self.assertEqual(store.database.pending_mutes(), [record])
        for source in (legacy, journal):
            self.assertFalse(source.exists())
            self.assertTrue(source.with_suffix('.json.migrated').is_file())
        reloaded = self.store()
        self.assertEqual(reloaded.snapshot(), store.snapshot())
        self.assertEqual(reloaded.database.pending_mutes(), [record])

    def test_migration_commit_before_rename_is_not_reimported(self):
        legacy = self.root / 'sentinel_data.json'
        legacy.write_text(json.dumps({'MUTE_TIME': 25, 'STATS': {'123': 7}}), encoding='utf-8')
        with patch.object(Path, 'rename', side_effect=OSError('rename unavailable')):
            store = self.store()
        self.assertIsNotNone(store.warning)
        self.assertTrue(legacy.exists())
        store.update('MUTE_TIME', 30)
        store.increment_stat('123')
        reloaded = self.store()
        self.assertEqual(reloaded.snapshot()['MUTE_TIME'], 30)
        self.assertEqual(reloaded.snapshot()['STATS']['123'], 8)
        self.assertFalse(legacy.exists())
        self.assertTrue(legacy.with_suffix('.json.migrated').exists())

    def test_invalid_migration_preserves_source_and_imports_no_partial_data(self):
        legacy = self.root / 'sentinel_data.json'
        source = json.dumps({'MUTE_TIME': 25, 'WHITELIST': ['not-an-id']})
        legacy.write_text(source, encoding='utf-8')
        store = self.store()
        self.assertIsNotNone(store.warning)
        self.assertEqual(store.snapshot(), DEFAULT_CONFIG)
        self.assertEqual(legacy.read_text(encoding='utf-8'), source)
        self.assertFalse(legacy.with_suffix('.json.migrated').exists())

    def test_invalid_journal_migration_imports_no_valid_subset(self):
        legacy = self.root / 'pending_mutes.json'
        legacy.write_text(json.dumps({'1:2': {'guild_id': 1, 'user_id': 2, 'unmute_at': 100},
                                      '1:3': {'guild_id': 1, 'user_id': 3, 'unmute_at': 'bad'}}), encoding='utf-8')
        store = self.store()
        self.assertIsNotNone(store.warning)
        self.assertEqual(store.database.pending_mutes(), [])
        self.assertTrue(legacy.exists())

    def test_repaired_legacy_config_can_be_migrated_after_invalid_first_start(self):
        legacy = self.root / 'sentinel_data.json'
        legacy.write_text('broken', encoding='utf-8')
        initial = self.store()
        self.assertEqual(initial.snapshot(), DEFAULT_CONFIG)
        legacy.write_text(json.dumps({'MUTE_TIME': 25}), encoding='utf-8')
        repaired = self.store()
        self.assertEqual(repaired.snapshot()['MUTE_TIME'], 25)
        self.assertFalse(legacy.exists())

    def test_sqlite_failure_keeps_configuration_cache_and_disk_unchanged(self):
        store = self.store()
        with store.database.transaction() as connection:
            connection.execute("""CREATE TRIGGER reject_config_update BEFORE UPDATE ON config
                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            store.update('MUTE_TIME', 25)
        self.assertEqual(store.snapshot()['MUTE_TIME'], 10)
        self.assertEqual(self.store().snapshot()['MUTE_TIME'], 10)
        self.assertFalse(store.database.connection.in_transaction)

    def test_transaction_rolls_back_all_pending_changes(self):
        store = self.store()
        with self.assertRaises(sqlite3.IntegrityError):
            with store.database.transaction() as connection:
                connection.execute('INSERT INTO pending_mutes (guild_id, user_id, unmute_at) VALUES (1, 2, 100)')
                connection.execute('INSERT INTO pending_mutes (guild_id, user_id, unmute_at) VALUES (1, 2, 200)')
        self.assertEqual(store.database.pending_mutes(), [])

    def test_increment_stat_is_atomic_across_threads_and_connections(self):
        stores = [self.store(), self.store()]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: stores[index % 2].increment_stat('123'), range(100)))
        self.assertEqual(sorted(results), list(range(1, 101)))
        self.assertEqual(self.store().snapshot()['STATS']['123'], 100)

    def test_user_sensitivity_persists_by_discord_id_and_validates_range(self):
        store = self.store()
        self.assertEqual(store.sensitivity('123'), 50)
        self.assertEqual(store.set_sensitivity('123', 82), 82)
        self.assertEqual(self.store().sensitivity('123'), 82)
        for value in (-1, 101, True, 5.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.set_sensitivity('123', value)

    def test_legacy_data_save_api_persists_to_sqlite(self):
        store = self.store()
        with store.lock:
            store.data['STATS']['123'] = 3
            store.save()
        self.assertEqual(self.store().snapshot()['STATS']['123'], 3)
        self.assertFalse((self.root / 'sentinel_data.json').exists())

    def test_wal_reader_does_not_block_on_uncommitted_writer(self):
        store = self.store()
        reader = sqlite3.connect(self.path, timeout=5)
        try:
            with store.database.transaction() as writer:
                writer.execute("UPDATE config SET value = '25' WHERE key = 'MUTE_TIME'")
                self.assertEqual(reader.execute("SELECT value FROM config WHERE key = 'MUTE_TIME'").fetchone()[0], '10')
            self.assertEqual(reader.execute("SELECT value FROM config WHERE key = 'MUTE_TIME'").fetchone()[0], '25')
        finally:
            reader.close()

    def test_event_history_reload_keeps_last_two_hundred_and_monotonic_ids(self):
        store = self.store()
        telemetry = Telemetry(store.database)
        for index in range(205):
            telemetry.event(f'event {index}')
        reopened = self.store()
        recovered = Telemetry(reopened.database)
        events = recovered.snapshot()['events']
        self.assertEqual(len(events), 200)
        self.assertEqual(events[0]['msg'], 'event 5')
        self.assertEqual(events[-1]['msg'], 'event 204')
        self.assertEqual(recovered.event('after restart', 'warning')['id'], events[-1]['id'] + 1)
        self.assertEqual(reopened.database.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 206)

    def test_concurrent_events_and_stats_have_no_lock_errors(self):
        store = self.store()
        telemetry = Telemetry(store.database)
        def write(index):
            store.increment_stat('123')
            return telemetry.event(f'event {index}')['id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            identities = list(pool.map(write, range(100)))
        self.assertEqual(len(set(identities)), 100)
        self.assertEqual(store.snapshot()['STATS']['123'], 100)
        self.assertEqual(len(telemetry.snapshot()['events']), 100)

    def test_failed_event_is_not_added_to_memory_cache(self):
        store = self.store()
        telemetry = Telemetry(store.database)
        with patch.object(store.database, 'insert_event', side_effect=sqlite3.OperationalError('disk')):
            with self.assertRaises(sqlite3.OperationalError):
                telemetry.event('not committed')
        self.assertEqual(telemetry.snapshot()['events'], [])
        self.assertEqual(store.database.recent_events(), [])

    def test_memory_demo_and_file_demo_are_isolated(self):
        real = self.store()
        demo = self.store(self.root / 'backups' / 'demo_sentinel.db')
        memory = self.store(':memory:')
        for store in (demo, memory):
            store.update('MUTE_TIME', 30)
            store.database.put_pending_mute({'guild_id': 1, 'user_id': 2, 'unmute_at': 100})
            Telemetry(store.database).event('demo only')
        self.assertEqual(real.snapshot()['MUTE_TIME'], 10)
        self.assertEqual(real.database.pending_mutes(), [])
        self.assertEqual(real.database.recent_events(), [])


class AsyncSQLiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ConfigStore(Path(self.directory.name) / 'sentinel.db')
        self.patches = [patch.object(main, 'STORE', self.store),
                        patch.object(main, 'MUTE_JOURNAL_LOCK', asyncio.Lock())]
        for replacement in self.patches:
            replacement.start()

    async def asyncTearDown(self):
        for replacement in reversed(self.patches):
            replacement.stop()
        self.store.close()
        self.directory.cleanup()

    async def test_journal_runs_off_event_loop_and_remains_responsive(self):
        loop = asyncio.get_running_loop()
        loop_thread = get_ident()
        entered = asyncio.Event()
        gate = Event()
        worker_threads = []
        original = self.store.database.put_pending_mute
        def blocked(record):
            worker_threads.append(get_ident())
            loop.call_soon_threadsafe(entered.set)
            if not gate.wait(2):
                raise TimeoutError('test gate')
            original(record)
        with patch.object(self.store.database, 'put_pending_mute', side_effect=blocked):
            task = asyncio.create_task(main.registrar_mute(1, 2, 10))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertNotEqual(worker_threads, [loop_thread])
                self.assertFalse(task.done())
            finally:
                gate.set()
                await task
        self.assertEqual(len(self.store.database.pending_mutes()), 1)

    async def test_cancelled_writer_finishes_before_releasing_async_lock(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        gate = Event()
        original = self.store.database.put_pending_mute
        def blocked(record):
            loop.call_soon_threadsafe(entered.set)
            if not gate.wait(2):
                raise TimeoutError('test gate')
            original(record)
        with patch.object(self.store.database, 'put_pending_mute', side_effect=blocked):
            writer = asyncio.create_task(main.registrar_mute(1, 2, 10))
            deleter = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                writer.cancel()
                deleter = asyncio.create_task(main.liberar_mute_journal(1, 2))
                await asyncio.sleep(0)
                writer.cancel()  # Una segunda cancelación tampoco suelta el lock.
                await asyncio.sleep(0)
                self.assertTrue(main.MUTE_JOURNAL_LOCK.locked())
                self.assertFalse(writer.done())
                self.assertFalse(deleter.done())
            finally:
                gate.set()
                results = await asyncio.gather(writer, *([deleter] if deleter else []), return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(self.store.database.pending_mutes(), [])
        self.assertFalse(main.MUTE_JOURNAL_LOCK.locked())

    async def test_sync_log_callback_in_event_loop_persists_off_thread(self):
        telemetry = Telemetry(self.store.database)
        worker_threads = []
        loop_thread = get_ident()
        original = self.store.database.insert_event
        def record_event(*args):
            worker_threads.append(get_ident())
            return original(*args)
        with patch.object(main, 'TELEMETRY', telemetry), patch.object(main, 'EVENT_TASKS', set()), \
             patch.object(main.logger, 'info'), patch.object(main.socketio, 'emit') as emit, \
             patch.object(self.store.database, 'insert_event', side_effect=record_event):
            main.log_event('persisted callback')
            await asyncio.gather(*main.EVENT_TASKS)
            self.assertEqual(len(worker_threads), 1)
            self.assertNotEqual(worker_threads[0], loop_thread)
            self.assertEqual(telemetry.snapshot()['events'][0]['msg'], 'persisted callback')
            emit.assert_called_once_with('log_event', telemetry.snapshot()['events'][0])
            self.assertEqual(main.EVENT_TASKS, set())


if __name__ == '__main__':
    unittest.main()
