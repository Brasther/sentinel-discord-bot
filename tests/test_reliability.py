import asyncio
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
import main
from sentinel_core import ConfigStore


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.journal = Path(self.directory.name) / 'sentinel.db'
        self.store = ConfigStore(self.journal)
        self.patches = [
            patch.object(main, 'STORE', self.store),
            patch.object(main, 'MUTE_JOURNAL_LOCK', asyncio.Lock()),
            patch.object(main, 'MUTE_TASKS', {}),
            patch.object(main, 'CASTIGOS_ACTIVOS', set()),
            patch.object(main, 'PREVIEW', False),
            patch.object(main, 'log_event'),
            patch.object(main, 'CONFIG', self.store.data),
        ]
        for replacement in self.patches:
            replacement.start()

    async def asyncTearDown(self):
        tasks = [task for task in main.MUTE_TASKS.values()
                 if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for replacement in reversed(self.patches):
            replacement.stop()
        self.store.close()
        self.directory.cleanup()

    def records(self):
        return {f"{record['guild_id']}:{record['user_id']}": record
                for record in self.store.database.pending_mutes()}

    def member_and_bot(self, channel_id=11):
        origin = SimpleNamespace(id=10)
        jail = SimpleNamespace(id=11, name='La Cárcel')
        guild = SimpleNamespace(id=1, voice_channels=[jail])
        member = SimpleNamespace(id=2, display_name='Test', mention='<@2>', guild=guild,
                                 voice=SimpleNamespace(mute=True, channel=SimpleNamespace(id=channel_id)),
                                 edit=AsyncMock(), move_to=AsyncMock())
        guild.get_member = Mock(return_value=member)
        guild.fetch_member = AsyncMock(return_value=member)
        guild.get_channel = Mock(side_effect=lambda identity: origin if identity == 10 else None)
        bot = SimpleNamespace(get_guild=Mock(return_value=guild),
                              get_channel=Mock(return_value=None), is_closed=Mock(return_value=False))
        return member, bot, origin

    async def seed_record(self, duration=-1):
        await main.registrar_mute(1, 2, duration, jail_id=11, orig_id=10)

    async def drain_recovery(self):
        await main.reconciliar_mutes_pendientes()
        tasks = list(main.MUTE_TASKS.values())
        await asyncio.gather(*tasks)

    async def test_registration_is_atomic_and_has_all_required_fields(self):
        with patch.object(main, 'time', SimpleNamespace(time=lambda: 100)):
            await main.registrar_mute(1, 2, 10, jail_id=11, orig_id=10)
        self.assertTrue(self.journal.is_file())
        record = self.records()['1:2']
        self.assertEqual(record, {'guild_id': 1, 'user_id': 2, 'unmute_at': 110,
                                  'jail_id': 11, 'orig_id': 10})
        self.assertFalse(self.store.database.connection.in_transaction)
        self.assertEqual(list(self.journal.parent.glob('*.tmp')), [])

    async def test_release_removes_only_requested_entry_and_is_idempotent(self):
        await self.seed_record()
        await main.registrar_mute(1, 3, 10)
        other = self.records()['1:3']
        await main.liberar_mute_journal(1, 2)
        self.assertEqual(self.records(), {'1:3': other})
        await main.liberar_mute_journal(1, 2)
        self.assertEqual(self.records(), {'1:3': other})
        self.assertEqual(list(self.journal.parent.glob('*.tmp')), [])

    async def test_on_ready_starts_journal_reconciliation(self):
        bot = SimpleNamespace(user=SimpleNamespace(name='Test'), voice_clients=[])
        watchdog = Mock(done=Mock(return_value=False))
        with patch.object(main, 'bot', bot), patch.object(main, 'WATCHDOG_TASK', watchdog), \
             patch.object(main, 'reconciliar_mutes_pendientes', new_callable=AsyncMock) as reconcile, \
             patch.dict(main.os.environ, {'SENTINEL_RESUME_CHANNEL': ''}), patch('builtins.print'):
            await main.on_ready()
        reconcile.assert_awaited_once_with()

    async def test_audio_engine_hot_update_keeps_voice_client_and_migrates_scores(self):
        class UpdatedEngine:
            def __init__(self):
                self.points = 0
                self.floor = 0

            def migrate_state_from(self, previous):
                self.points = previous.points
                self.floor = previous.floor

        sink = main.VigilanteRuido(None, 0)
        previous = main.AudioEngine()
        previous.points = 37
        previous.floor = 24.5
        sink.engines[123] = previous
        reader = SimpleNamespace(sink=sink)
        client = SimpleNamespace(_reader=reader, disconnect=AsyncMock(), is_connected=lambda: True)
        bot = SimpleNamespace(voice_clients=[client])
        signature = (main.AUDIO_SOURCE_SIGNATURE[0] + 1, main.AUDIO_SOURCE_SIGNATURE[1])
        module = SimpleNamespace(AudioEngine=UpdatedEngine, calibrated_threshold=lambda values: 10)
        with patch.object(main, 'bot', bot), patch.object(main, '_load_audio_detector', return_value=module), \
             patch.object(main, '_log_event_async', new_callable=AsyncMock), \
             patch.object(main, 'AUDIO_SOURCE_SIGNATURE', main.AUDIO_SOURCE_SIGNATURE), \
             patch.object(main, 'AudioEngine', main.AudioEngine), \
             patch.object(main, 'calibrated_threshold', main.calibrated_threshold):
            await main._apply_audio_detector_update(signature)
            current = sink.engines[123]
            self.assertIsInstance(current, UpdatedEngine)
            self.assertEqual((current.points, current.floor), (37, 24.5))
            self.assertIs(client._reader.sink, sink)
            self.assertTrue(client.is_connected())
            client.disconnect.assert_not_awaited()

    def test_audio_detector_source_loads_as_an_isolated_module(self):
        signature = main._audio_source_signature()
        module = main._load_audio_detector(signature)
        updated = module.AudioEngine()
        previous = main.AudioEngine()
        previous.points = 19
        previous.floor = 12.5
        updated.migrate_state_from(previous)
        self.assertEqual((updated.points, updated.floor), (19, 12.5))

    async def test_unexpected_voice_disconnect_rejoins_without_process_restart(self):
        channel = SimpleNamespace(name='General', members=[SimpleNamespace(bot=False)])
        guild = SimpleNamespace(id=55, voice_client=None)
        member = SimpleNamespace(id=99, guild=guild, edit=AsyncMock())
        before = SimpleNamespace(channel=channel)
        after = SimpleNamespace(channel=None, mute=False)
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        with patch.object(main, 'bot', bot), patch.object(main, 'SALIDA_AUTORIZADA', False), \
             patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_RESTARTING', set()), \
             patch.object(main, 'VOICE_REJOINING', set()), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock), \
             patch.object(main.os, 'execv') as restart:
            await main.on_voice_state_update(member, before, after)
        connect.assert_awaited_once_with(channel)
        restart.assert_not_called()

    async def test_unexpected_voice_disconnect_is_respected_when_auto_rejoin_is_disabled(self):
        channel = SimpleNamespace(name='General', members=[SimpleNamespace(bot=False)])
        guild = SimpleNamespace(id=56, voice_client=None)
        member = SimpleNamespace(id=99, guild=guild, edit=AsyncMock())
        before = SimpleNamespace(channel=channel)
        after = SimpleNamespace(channel=None, mute=False)
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        with patch.object(main, 'bot', bot), patch.object(main, 'CONFIG', {'AUTO_REENTRAR_VOZ': False}), \
             patch.object(main, 'SALIDA_AUTORIZADA', False), \
             patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_RESTARTING', set()), \
             patch.object(main, 'VOICE_REJOINING', set()), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock) as log_event:
            await main.on_voice_state_update(member, before, after)
        connect.assert_not_awaited()
        self.assertIn('Autorreconexión desactivada', log_event.await_args.args[0])

    async def test_manual_dashboard_connect_joins_the_most_occupied_voice_channel(self):
        quiet = SimpleNamespace(name='Quieto', members=[])
        occupied = SimpleNamespace(name='General', members=[SimpleNamespace(bot=False)] * 2)
        guild = SimpleNamespace(id=58, voice_channels=[quiet, occupied], stage_channels=[], voice_client=None)
        quiet.guild = occupied.guild = guild
        bot = SimpleNamespace(guilds=[guild], voice_clients=[], is_ready=Mock(return_value=True))
        with patch.object(main, 'bot', bot), patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock), \
             patch.object(main, 'CONFIG', {'AUTO_UNIR_VOZ': False}):
            result = await main._connect_voice_to_occupied_channel()
        self.assertTrue(result['ok'])
        self.assertEqual(result['channel'], 'General')
        connect.assert_awaited_once_with(occupied)

    async def test_manual_dashboard_connect_explains_when_no_voice_channel_is_occupied(self):
        empty = SimpleNamespace(name='General', members=[])
        guild = SimpleNamespace(id=59, voice_channels=[empty], stage_channels=[], voice_client=None)
        bot = SimpleNamespace(guilds=[guild], voice_clients=[], is_ready=Mock(return_value=True))
        with patch.object(main, 'bot', bot):
            result = await main._connect_voice_to_occupied_channel()
        self.assertFalse(result['ok'])
        self.assertIn('nadie', result['error'])

    async def test_moving_bot_client_preserves_connection_and_audio_listener(self):
        source = SimpleNamespace(id=1, name='General')
        destination = SimpleNamespace(id=2, name='Sala nueva')
        guild = SimpleNamespace(id=57, me=object(), voice_client=None)
        async def move_to(channel, *, timeout):
            client.channel = channel
        client = SimpleNamespace(
            channel=source,
            is_connected=Mock(return_value=True),
            is_listening=Mock(return_value=True),
            move_to=AsyncMock(side_effect=move_to),
            disconnect=AsyncMock(),
        )
        guild.voice_client = client
        destination.guild = guild
        destination.id = 2
        destination.permissions_for = Mock(
            return_value=SimpleNamespace(view_channel=True, connect=True))
        with patch.object(main, 'VOICE_CONNECT_LOCK', asyncio.Lock()), \
             patch.object(main, '_log_event_async', new_callable=AsyncMock):
            result = await main.connect_audio(destination)
        self.assertIs(result, client)
        client.move_to.assert_awaited_once_with(destination, timeout=main.VOICE_MOVE_TIMEOUT)
        client.disconnect.assert_not_awaited()

    async def test_voice_move_uses_same_client_when_latest_target_changes_in_flight(self):
        source = SimpleNamespace(id=1, name='General')
        intermediate = SimpleNamespace(id=2, name='Intermedio')
        human = SimpleNamespace(bot=False)
        destination = SimpleNamespace(id=3, name='Destino final', members=[human])
        guild = SimpleNamespace(id=5710, me=object(), voice_client=None)
        async def move_to(channel, *, timeout):
            if channel is intermediate:
                main.VOICE_PENDING_FOLLOWS[guild.id] = (guild, intermediate, destination)
                main.VOICE_RECENT_MOVE_TARGETS[guild.id] = (destination, main.time.monotonic())
            else:
                client.channel = destination
        client = SimpleNamespace(
            channel=source,
            is_connected=Mock(side_effect=[False, False, True, True, True]),
            is_listening=Mock(return_value=True),
            move_to=AsyncMock(side_effect=move_to),
            disconnect=AsyncMock(),
        )
        guild.voice_client = client
        intermediate.guild = guild
        intermediate.permissions_for = Mock(
            return_value=SimpleNamespace(view_channel=True, connect=True))
        intermediate.connect = AsyncMock()
        destination.guild = guild
        destination.permissions_for = Mock(
            return_value=SimpleNamespace(view_channel=True, connect=True))
        destination.connect = AsyncMock()
        with patch.object(main, 'VOICE_CONNECT_LOCK', asyncio.Lock()), \
             patch.object(main, 'VOICE_FOLLOW_TASKS', {guild.id: asyncio.current_task()}), \
             patch.object(main, 'VOICE_PENDING_FOLLOWS', {}), \
             patch.object(main, 'VOICE_RECENT_MOVE_TARGETS', {}), \
             patch.object(main, 'VigilanteRuido'), \
             patch.object(main, '_log_event_async', new_callable=AsyncMock):
            result = await main.connect_audio(intermediate)
        self.assertIs(result, client)
        self.assertEqual([call.args[0] for call in client.move_to.await_args_list],
                         [intermediate, destination])
        intermediate.connect.assert_not_awaited()
        destination.connect.assert_not_awaited()
        client.disconnect.assert_not_awaited()

    async def test_group_follow_retries_when_voice_client_is_temporarily_disconnected(self):
        human = SimpleNamespace(bot=False)
        destination = SimpleNamespace(id=8102, name='Destino', members=[human])
        source = SimpleNamespace(id=8101, name='Origen', members=[])
        client = SimpleNamespace(channel=source, is_connected=Mock(side_effect=[False, True]),
                                 is_listening=Mock(return_value=True))
        guild = SimpleNamespace(id=810, voice_client=client,
                                voice_channels=[source, destination], stage_channels=[])
        with patch.object(main, 'VOICE_FOLLOW_TASKS', {}), \
             patch.object(main, 'VOICE_PENDING_FOLLOWS', {guild.id: (guild, source, destination)}), \
             patch.object(main, 'VOICE_RECENT_MOVE_TARGETS', {}), \
             patch.object(main, 'VOICE_REJOINING', set()), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect, \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            task = asyncio.create_task(main._follow_voice_moves(guild.id))
            main.VOICE_FOLLOW_TASKS[guild.id] = task
            await task
        connect.assert_awaited_once_with(destination)

    async def test_move_during_active_follow_updates_latest_destination(self):
        human = SimpleNamespace(id=8120, bot=False)
        bot_member = SimpleNamespace(id=8121, bot=True)
        source = SimpleNamespace(id=8122, name='Origen', members=[])
        intermediate = SimpleNamespace(id=8123, name='Intermedio', members=[human])
        destination = SimpleNamespace(id=8124, name='Final', members=[human])
        client = SimpleNamespace(channel=source,
                                 is_connected=Mock(return_value=True),
                                 is_listening=Mock(return_value=True))
        guild = SimpleNamespace(id=812, voice_client=client,
                                voice_channels=[source, intermediate, destination],
                                stage_channels=[])
        first_mover = SimpleNamespace(id=human.id, bot=False, guild=guild)
        second_mover = SimpleNamespace(id=human.id, bot=False, guild=guild)
        bot = SimpleNamespace(user=SimpleNamespace(id=bot_member.id))
        async def connect(channel):
            if channel is intermediate:
                await main.on_voice_state_update(
                    second_mover, SimpleNamespace(channel=intermediate),
                    SimpleNamespace(channel=destination))
            client.channel = channel
        with patch.object(main, 'bot', bot), \
             patch.object(main, 'VOICE_JOINING', set()), \
             patch.object(main, 'VOICE_RESTARTING', set()), \
             patch.object(main, 'VOICE_REJOINING', set()), \
             patch.object(main, 'VOICE_LEAVE_AUTHORIZED', set()), \
             patch.object(main, 'VOICE_FOLLOW_TASKS', {}), \
             patch.object(main, 'VOICE_PENDING_FOLLOWS', {}), \
             patch.object(main, 'VOICE_RECENT_MOVE_TARGETS', {}), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock, side_effect=connect) as follow, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            await main.on_voice_state_update(
                first_mover, SimpleNamespace(channel=source),
                SimpleNamespace(channel=intermediate))
            task = main.VOICE_FOLLOW_TASKS[guild.id]
            await task
        self.assertEqual([call.args[0] for call in follow.await_args_list],
                         [intermediate, destination])

    async def test_moving_bot_client_restarts_stopped_listener_without_disconnect(self):
        channel = SimpleNamespace(id=3, name='General')
        guild = SimpleNamespace(id=571, me=object(), voice_client=None)
        client = SimpleNamespace(
            channel=channel,
            is_connected=Mock(return_value=True),
            is_listening=Mock(return_value=False),
            move_to=AsyncMock(),
            listen=Mock(),
            disconnect=AsyncMock(),
        )
        guild.voice_client = client
        channel.guild = guild
        channel.permissions_for = Mock(
            return_value=SimpleNamespace(view_channel=True, connect=True))
        with patch.object(main, 'VOICE_CONNECT_LOCK', asyncio.Lock()), \
             patch.object(main, 'VigilanteRuido'), \
             patch.object(main, '_log_event_async', new_callable=AsyncMock):
            result = await main.connect_audio(channel)
        self.assertIs(result, client)
        client.listen.assert_called_once()
        client.disconnect.assert_not_awaited()

    async def test_bot_follows_direct_moderator_channel_move(self):
        source = SimpleNamespace(name='General')
        destination = SimpleNamespace(name='Sala nueva')
        guild = SimpleNamespace(id=58, voice_client=None)
        member = SimpleNamespace(id=99, guild=guild, bot=True, edit=AsyncMock())
        before = SimpleNamespace(channel=source)
        after = SimpleNamespace(channel=destination, mute=False)
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        with patch.object(main, 'bot', bot), patch.object(main, 'SALIDA_AUTORIZADA', False), \
             patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_RESTARTING', set()), \
             patch.object(main, 'VOICE_REJOINING', set()), patch.object(main, 'VOICE_LEAVE_AUTHORIZED', set()), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock):
            await main.on_voice_state_update(member, before, after)
        connect.assert_awaited_once_with(destination)

    async def test_auto_join_when_human_enters_voice_respects_web_toggle_and_manual_stop(self):
        for enabled, suppressed, should_connect in (
                (True, set(), True), (False, set(), False), (True, {592}, False)):
            with self.subTest(enabled=enabled, suppressed=suppressed):
                guild_id = 592
                channel = SimpleNamespace(name='General')
                guild = SimpleNamespace(id=guild_id, voice_client=None)
                channel.guild = guild
                member = SimpleNamespace(id=10, guild=guild, bot=False)
                before = SimpleNamespace(channel=None)
                after = SimpleNamespace(channel=channel, mute=False)
                bot = SimpleNamespace(user=SimpleNamespace(id=99))
                with patch.object(main, 'bot', bot), patch.object(main, 'CONFIG', {'AUTO_UNIR_VOZ': enabled}), \
                     patch.object(main, 'VOICE_AUTO_JOIN_SUPPRESSED', set(suppressed)), \
                     patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_REJOINING', set()), \
                     patch.object(main, 'VOICE_RESTARTING', set()), patch.object(main, 'VOICE_LEAVE_AUTHORIZED', set()), \
                     patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect, \
                     patch.object(main, '_log_event_async', new_callable=AsyncMock):
                    await main.on_voice_state_update(member, before, after)
                if should_connect:
                    connect.assert_awaited_once_with(channel)
                else:
                    connect.assert_not_awaited()

    async def test_follows_group_only_after_watched_channel_is_empty(self):
        for source_has_human in (False, True):
            with self.subTest(source_has_human=source_has_human):
                guild_id = 590 if source_has_human else 589
                human = SimpleNamespace(id=10, bot=False)
                bot_member = SimpleNamespace(id=99, bot=True)
                source_members = [bot_member] + ([human] if source_has_human else [])
                source = SimpleNamespace(id=1, name='General', members=source_members)
                destination = SimpleNamespace(id=2, name='Sala nueva', members=[human])
                client = SimpleNamespace(
                    channel=source,
                    is_connected=Mock(return_value=True),
                    is_listening=Mock(return_value=True),
                )
                async def follow(channel):
                    client.channel = channel
                guild = SimpleNamespace(id=guild_id, voice_client=client,
                                        voice_channels=[source, destination])
                mover = SimpleNamespace(id=11, bot=False, guild=guild)
                before = SimpleNamespace(channel=source)
                after = SimpleNamespace(channel=destination)
                bot = SimpleNamespace(user=SimpleNamespace(id=99))
                with patch.object(main, 'bot', bot), patch.object(main, 'SALIDA_AUTORIZADA', False), \
                     patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_RESTARTING', set()), \
                     patch.object(main, 'VOICE_REJOINING', set()), patch.object(main, 'VOICE_LEAVE_AUTHORIZED', set()), \
                     patch.object(main, 'VOICE_FOLLOW_TASKS', {}), \
                     patch.object(main, 'VOICE_PENDING_FOLLOWS', {}), \
                     patch.object(main, 'VOICE_RECENT_MOVE_TARGETS', {}), \
                     patch.object(main, 'connect_audio', new_callable=AsyncMock,
                                  side_effect=follow) as connect, \
                     patch.object(main, '_log_event_async', new_callable=AsyncMock), \
                     patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
                    await main.on_voice_state_update(mover, before, after)
                    task = main.VOICE_FOLLOW_TASKS[guild_id]
                    await task
                if source_has_human:
                    connect.assert_not_awaited()
                else:
                    connect.assert_awaited_once_with(destination)

    async def test_group_follow_uses_latest_destination_after_rapid_channel_changes(self):
        guild_id = 591
        human = SimpleNamespace(id=10, bot=False)
        source = SimpleNamespace(id=1, name='General', members=[])
        intermediate = SimpleNamespace(id=2, name='Intermedio', members=[])
        destination = SimpleNamespace(id=3, name='Destino final', members=[human])
        client = SimpleNamespace(
            channel=source,
            is_connected=Mock(return_value=True),
            is_listening=Mock(return_value=True),
        )
        async def follow(channel):
            client.channel = channel
        guild = SimpleNamespace(id=guild_id, voice_client=client,
                                voice_channels=[source, intermediate, destination])
        mover = SimpleNamespace(id=11, bot=False, guild=guild)
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        with patch.object(main, 'bot', bot), patch.object(main, 'SALIDA_AUTORIZADA', False), \
             patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_RESTARTING', set()), \
             patch.object(main, 'VOICE_REJOINING', set()), patch.object(main, 'VOICE_LEAVE_AUTHORIZED', set()), \
             patch.object(main, 'VOICE_FOLLOW_TASKS', {}), \
             patch.object(main, 'VOICE_PENDING_FOLLOWS', {}), \
             patch.object(main, 'VOICE_RECENT_MOVE_TARGETS', {}), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock, side_effect=follow) as connect, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            await main.on_voice_state_update(
                mover, SimpleNamespace(channel=source), SimpleNamespace(channel=intermediate))
            await main.on_voice_state_update(
                mover, SimpleNamespace(channel=intermediate), SimpleNamespace(channel=destination))
            task = main.VOICE_FOLLOW_TASKS[guild_id]
            await task
        connect.assert_awaited_once_with(destination)

    async def test_group_follow_retargets_when_members_move_during_voice_transition(self):
        guild_id = 5911
        human = SimpleNamespace(id=10, bot=False)
        source = SimpleNamespace(id=1, name='General', members=[])
        intermediate = SimpleNamespace(id=2, name='Intermedio', members=[human])
        destination = SimpleNamespace(id=3, name='Destino final', members=[human])
        client = SimpleNamespace(
            channel=source,
            is_connected=Mock(return_value=True),
            is_listening=Mock(return_value=True),
        )
        guild = SimpleNamespace(id=guild_id, voice_client=client,
                                voice_channels=[source, intermediate, destination])
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        async def follow(channel):
            client.channel = channel
            if channel is intermediate:
                intermediate.members.clear()
                main.VOICE_RECENT_MOVE_TARGETS[guild_id] = (destination, main.time.monotonic())
                main.VOICE_PENDING_FOLLOWS[guild_id] = (guild, source, destination)
        with patch.object(main, 'bot', bot), patch.object(main, 'VOICE_REJOINING', set()), \
             patch.object(main, 'VOICE_FOLLOW_TASKS', {}), \
             patch.object(main, 'VOICE_PENDING_FOLLOWS', {guild_id: (guild, source, intermediate)}), \
             patch.object(main, 'VOICE_RECENT_MOVE_TARGETS', {guild_id: (intermediate, main.time.monotonic())}), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock, side_effect=follow) as connect, \
             patch.object(main, '_log_event_async', new_callable=AsyncMock), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock):
            task = asyncio.create_task(main._follow_voice_moves(guild_id))
            main.VOICE_FOLLOW_TASKS[guild_id] = task
            await task
        self.assertEqual(connect.await_args_list[0].args, (intermediate,))
        self.assertEqual(connect.await_args_list[1].args, (destination,))

    async def test_authorized_voice_leave_does_not_trigger_automatic_rejoin(self):
        channel = SimpleNamespace(name='General')
        guild = SimpleNamespace(id=56)
        member = SimpleNamespace(id=99, guild=guild, edit=AsyncMock())
        before = SimpleNamespace(channel=channel)
        after = SimpleNamespace(channel=None, mute=False)
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        with patch.object(main, 'bot', bot), patch.object(main, 'SALIDA_AUTORIZADA', False), \
             patch.object(main, 'VOICE_JOINING', set()), patch.object(main, 'VOICE_RESTARTING', set()), \
             patch.object(main, 'VOICE_REJOINING', set()), patch.object(main, 'VOICE_LEAVE_AUTHORIZED', {56}), \
             patch.object(main, 'connect_audio', new_callable=AsyncMock) as connect:
            await main.on_voice_state_update(member, before, after)
        connect.assert_not_awaited()

    async def test_concurrent_journal_updates_survive_reload(self):
        await asyncio.gather(*(main.registrar_mute(1, identity, 10) for identity in range(1, 21)))
        records = self.records()
        self.assertEqual(len(records), 20)
        await asyncio.gather(*(main.liberar_mute_journal(1, identity) for identity in range(1, 11)))
        records = self.records()
        self.assertEqual({record['user_id'] for record in records.values()}, set(range(11, 21)))

    async def test_failed_sqlite_write_preserves_previous_journal(self):
        await self.seed_record()
        previous = self.records()
        with self.store.database.transaction() as connection:
            connection.execute("""CREATE TRIGGER reject_test_mute BEFORE INSERT ON pending_mutes
                WHEN NEW.user_id = 3 BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            await main.registrar_mute(1, 3, 10)
        self.assertEqual(self.records(), previous)
        self.assertFalse(self.store.database.connection.in_transaction)

    async def test_corrupt_journal_is_not_overwritten(self):
        legacy = self.journal.parent / 'pending_mutes.json'
        legacy.write_text('broken', encoding='utf-8')
        self.store.close()
        self.store = ConfigStore(self.journal)
        with patch.object(main, 'STORE', self.store):
            await main.reconciliar_mutes_pendientes()
        self.assertIsNotNone(self.store.warning)
        self.assertEqual(legacy.read_text(encoding='utf-8'), 'broken')
        self.assertEqual(self.records(), {})
        self.assertEqual(main.MUTE_TASKS, {})

    async def test_restart_releases_expired_mute_and_restores_original_channel(self):
        await self.seed_record()
        member, bot, origin = self.member_and_bot()
        with patch.object(main, 'bot', bot):
            await self.drain_recovery()
        member.edit.assert_awaited_once_with(mute=False, reason='Sentinel: recuperación de castigo pendiente')
        self.assertEqual(member.move_to.await_args.args, (origin,))
        self.assertEqual(self.records(), {})
        self.assertNotIn(2, main.CASTIGOS_ACTIVOS)

    async def test_future_deadline_and_repeated_ready_do_not_duplicate_release(self):
        clock = [100.0]
        member, bot, _ = self.member_and_bot()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def sleep(seconds):
            self.assertEqual(seconds, 15)
            member.edit.assert_not_awaited()
            entered.set()
            await release.wait()
            clock[0] += seconds
        with patch.object(main, 'time', SimpleNamespace(time=lambda: clock[0])):
            await self.seed_record(duration=15)
            with patch.object(main, 'bot', bot), patch.object(main.asyncio, 'sleep', side_effect=sleep):
                await main.reconciliar_mutes_pendientes()
                first = list(main.MUTE_TASKS.values())
                self.assertEqual(len(first), 1)
                await asyncio.wait_for(entered.wait(), timeout=1)
                member.edit.assert_not_awaited()
                member.move_to.assert_not_awaited()
                self.assertIn('1:2', self.records())
                self.assertFalse(first[0].done())
                await main.reconciliar_mutes_pendientes()
                self.assertEqual(list(main.MUTE_TASKS.values()), first)
                release.set()
                await asyncio.wait_for(asyncio.gather(*first), timeout=1)
        member.edit.assert_awaited_once()
        self.assertEqual(self.records(), {})

    async def test_reconciliation_does_not_duplicate_live_punishment(self):
        await self.seed_record()
        main.MUTE_TASKS[(1, 2)] = asyncio.current_task()
        await main.reconciliar_mutes_pendientes()
        self.assertIs(main.MUTE_TASKS[(1, 2)], asyncio.current_task())

    async def test_failed_unmute_keeps_journal_and_active_reservation(self):
        await self.seed_record()
        member, bot, _ = self.member_and_bot()
        member.edit.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason='error'), 'failure')
        with patch.object(main, 'bot', bot), patch.object(main.asyncio, 'sleep', side_effect=asyncio.CancelledError):
            await main.reconciliar_mutes_pendientes()
            results = await asyncio.gather(*main.MUTE_TASKS.values(), return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIn('1:2', self.records())
        self.assertIn(2, main.CASTIGOS_ACTIVOS)

    async def test_disconnected_member_is_released_after_rejoining(self):
        await self.seed_record()
        member, bot, _ = self.member_and_bot()
        voice = member.voice
        member.voice = None
        async def sleep(seconds):
            self.assertEqual(seconds, 10)
            self.assertIn('1:2', self.records())
            member.edit.assert_not_awaited()
            member.voice = voice
        with patch.object(main, 'bot', bot), patch.object(main.asyncio, 'sleep', side_effect=sleep):
            await self.drain_recovery()
        member.edit.assert_awaited_once()

    async def test_cache_miss_fetches_member(self):
        await self.seed_record()
        member, bot, _ = self.member_and_bot()
        member.guild.get_member.return_value = None
        with patch.object(main, 'bot', bot):
            await self.drain_recovery()
        member.guild.fetch_member.assert_awaited_once_with(2)
        member.edit.assert_awaited_once()

    async def test_manual_move_is_not_reversed(self):
        await self.seed_record()
        member, bot, _ = self.member_and_bot(channel_id=12)
        with patch.object(main, 'bot', bot):
            await self.drain_recovery()
        member.move_to.assert_not_awaited()
        member.edit.assert_awaited_once()

    async def test_failed_return_does_not_prevent_unmute(self):
        await self.seed_record()
        member, bot, _ = self.member_and_bot()
        member.move_to.side_effect = discord.HTTPException(SimpleNamespace(status=403, reason='error'), 'failure')
        with patch.object(main, 'bot', bot):
            await self.drain_recovery()
        member.edit.assert_awaited_once()
        self.assertEqual(self.records(), {})

    async def test_punishment_journals_before_other_side_effects_and_removes_on_success(self):
        member, bot, origin = self.member_and_bot()
        member.voice.mute = False
        member.voice.channel = origin
        async def check_journal(seconds):
            self.assertEqual(seconds, 10)
            record = self.records()['1:2']
            self.assertEqual((record['jail_id'], record['orig_id']), (11, 10))
        sink = main.VigilanteRuido(bot, main.CANAL_LOGS_ID)
        with patch.object(main.asyncio, 'sleep', side_effect=check_journal):
            await sink.castigar(member)
        self.assertEqual([call.kwargs['mute'] for call in member.edit.await_args_list], [True, False])
        self.assertEqual(self.records(), {})
        self.assertEqual(main.MUTE_TASKS, {})
        sink.cleanup()

    async def test_punishment_journal_failure_still_unmutes(self):
        member, bot, origin = self.member_and_bot()
        member.voice.mute = False
        member.voice.channel = origin
        sink = main.VigilanteRuido(bot, main.CANAL_LOGS_ID)
        with patch.object(main, 'registrar_mute', side_effect=OSError('disk')):
            await sink.castigar(member)
        self.assertEqual([call.kwargs['mute'] for call in member.edit.await_args_list], [True, False])
        sink.cleanup()

    async def test_failed_finally_unmute_is_journaled_and_retried(self):
        member, bot, origin = self.member_and_bot()
        member.voice.mute = False
        member.voice.channel = origin
        failure = discord.HTTPException(SimpleNamespace(status=500, reason='error'), 'failure')
        member.edit.side_effect = [None, failure, None]
        clock = [100.0]
        async def sleep(seconds):
            clock[0] += seconds
        sink = main.VigilanteRuido(bot, main.CANAL_LOGS_ID)
        with patch.object(main, 'bot', bot), \
             patch.object(main, 'time', SimpleNamespace(time=lambda: clock[0])), \
             patch.object(main.asyncio, 'sleep', side_effect=sleep):
            await sink.castigar(member)
            self.assertIn('1:2', self.records())
            self.assertIn(2, main.CASTIGOS_ACTIVOS)
            tasks = list(main.MUTE_TASKS.values())
            self.assertEqual(len(tasks), 1)
            await asyncio.gather(*tasks)
        self.assertEqual([call.kwargs['mute'] for call in member.edit.await_args_list], [True, False, False])
        self.assertEqual(self.records(), {})
        sink.cleanup()

    async def test_failed_journal_deletion_retains_record_until_next_reconciliation(self):
        member, bot, origin = self.member_and_bot()
        member.voice.mute = False
        member.voice.channel = origin
        sink = main.VigilanteRuido(bot, main.CANAL_LOGS_ID)
        with patch.object(main.asyncio, 'sleep', new_callable=AsyncMock), \
             patch.object(main, 'liberar_mute_journal', side_effect=OSError('disk')):
            await sink.castigar(member)
            self.assertIn('1:2', self.records())
            self.assertEqual(len(main.MUTE_TASKS), 1)
            for task in list(main.MUTE_TASKS.values()):
                task.cancel()
            await asyncio.gather(*list(main.MUTE_TASKS.values()), return_exceptions=True)
        sink.cleanup()

    async def test_watchdog_distinguishes_dtx_from_unresponsive_signalling(self):
        for connected, closed, ack_age, latency, expect_restart in [
            (True, False, 1, .05, False),
            (True, False, 31, .05, True),
            (True, False, 1, float('inf'), True),
            (True, True, 1, .05, True),
            (False, False, 1, .05, True),
        ]:
            with self.subTest(connected=connected, closed=closed, ack_age=ack_age, latency=latency):
                ws = SimpleNamespace(ws=SimpleNamespace(closed=closed), latency=latency,
                                     _keep_alive=SimpleNamespace(_last_ack=100 - ack_age))
                vc = SimpleNamespace(ws=ws, channel=SimpleNamespace(members=[1, 2]),
                                     is_connected=Mock(return_value=connected))
                bot = SimpleNamespace(voice_clients=[vc], wait_until_ready=AsyncMock(),
                                      is_closed=Mock(side_effect=[False, True]))
                with patch.object(main, 'bot', bot), patch.object(main, 'ULTIMO_LATIDO', 0), \
                     patch.object(main, 'time', SimpleNamespace(time=lambda: 100, perf_counter=lambda: 100)), \
                     patch.object(main.asyncio, 'sleep', new_callable=AsyncMock) as sleep, \
                     patch.object(main.os, 'execv') as reboot:
                    await main.watchdog()
                    self.assertEqual(reboot.called, expect_restart)
                    sleep.assert_awaited_once_with(10)
                    if not expect_restart:
                        self.assertEqual(main.ULTIMO_LATIDO, 100)

    async def test_watchdog_preserves_thirty_second_threshold(self):
        vc = SimpleNamespace(ws=None, channel=SimpleNamespace(members=[1, 2]),
                             is_connected=Mock(return_value=False))
        bot = SimpleNamespace(voice_clients=[vc], wait_until_ready=AsyncMock(),
                              is_closed=Mock(side_effect=[False, True]))
        with patch.object(main, 'bot', bot), patch.object(main, 'ULTIMO_LATIDO', 70), \
             patch.object(main, 'time', SimpleNamespace(time=lambda: 100)), \
             patch.object(main.asyncio, 'sleep', new_callable=AsyncMock), patch.object(main.os, 'execv') as reboot:
            await main.watchdog()
        reboot.assert_not_called()

    async def test_concealed_audio_does_not_renew_watchdog_heartbeat(self):
        sink = main.VigilanteRuido(main.bot, main.CANAL_LOGS_ID)
        with patch.object(main, 'ULTIMO_LATIDO', 123):
            sink.write(SimpleNamespace(id=2), SimpleNamespace(pcm=b'\0' * 3840, packet=False))
            self.assertEqual(main.ULTIMO_LATIDO, 123)
        sink.cleanup()


if __name__ == '__main__':
    unittest.main()
