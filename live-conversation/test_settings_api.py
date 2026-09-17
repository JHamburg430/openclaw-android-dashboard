"""Settings transport/persistence and consumer integration, without model loading."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import settings_api
from server import LiveConversationService, _origin_matches_request, configured_tts_backend
from settings_config import validate_settings


class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'settings.json'
        self.service = LiveConversationService('test:settings', 1.08, settings_path=str(self.path))
        self.app = web.Application()
        self.app['service'] = self.service
        self.socket = SimpleNamespace(closed=False, send_json=AsyncMock())
        self.app['live_sockets'] = [self.socket]
        settings_api.install(self.app, _origin_matches_request)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def put(self, values, revision=None, **kwargs):
        return await self.client.put('/api/settings', json={'values': values, 'revision': self.service.settings_revision if revision is None else revision}, **kwargs)

    async def test_save_reload_broadcast_and_restart_pending(self):
        response = await self.client.get('/api/settings')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        initial = await response.json()
        self.assertEqual(initial['restart_required'], [])
        values = {'tts_speed': 1.25, 'audio_capture': True, 'action_confirmation': 'confirm', 'browser_start_confirm_ms': 400}
        response = await self.put(values)
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(saved['restart_required'], ['tts_speed'])
        self.assertEqual(self.service.tts.speed, 1.08)
        self.assertTrue(self.service.audio_capture_enabled)
        self.socket.send_json.assert_awaited_once()
        self.assertEqual(self.socket.send_json.call_args.args[0]['browser']['browser_start_confirm_ms'], 400)
        disk = json.loads(self.path.read_text())
        self.assertEqual(disk['configuration']['tts_speed'], 1.25)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        restarted = LiveConversationService('test:settings', 1.08, settings_path=str(self.path))
        self.assertEqual(restarted.tts.speed, 1.25)
        self.assertEqual(restarted.settings_revision, 1)
        self.assertTrue(restarted.confirmation_required)
        self.assertEqual(settings_api.payload(restarted)['restart_required'], [])

    async def test_invalid_requests_and_stale_revisions_do_not_write(self):
        for values, revision, expected in [({'tts_speed': 99}, 0, 400), ({'unknown': 1}, 0, 400),
                                           ({'tts_speed': float('nan')}, 0, 400), ({'tts_speed': True}, 0, 400),
                                           ({'tts_speed': 1.3}, 8, 409), ({}, False, 409),
                                           ({'browser_semantic_check_ms': 5000}, 0, 400)]:
            with self.subTest(values=values, revision=revision):
                response = await self.put(values, revision)
                self.assertEqual(response.status, expected)
                self.assertEqual(self.service.settings_revision, 0)
                self.assertFalse(self.path.exists())
        response = await self.client.put('/api/settings', data='{}')
        self.assertEqual(response.status, 415)
        response = await self.client.put('/api/settings', data='{', headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)

    async def test_cross_origin_get_put_and_restart_are_rejected(self):
        headers = {'Origin': 'https://untrusted.example'}
        response = await self.client.get('/api/settings', headers=headers)
        self.assertEqual(response.status, 403)
        response = await self.put({'audio_capture': True}, headers=headers)
        self.assertEqual(response.status, 403)
        with patch('settings_api.asyncio.create_subprocess_exec', new_callable=AsyncMock) as launch:
            response = await self.client.post('/api/settings/restart', json={'revision': 0}, headers=headers)
            self.assertEqual(response.status, 403)
            launch.assert_not_called()
        self.assertFalse(self.service.audio_capture_enabled)

    async def test_persistence_failure_rolls_back_every_live_field(self):
        self.service.pending_confirmation = ('keep me', None, 'agent')
        before = dict(self.service.configuration)
        with patch('server._secure_atomic_write', side_effect=OSError('disk full')):
            response = await self.put({'tts_speed': 1.4, 'audio_capture': True, 'action_confirmation': 'confirm'})
        self.assertEqual(response.status, 500)
        self.assertEqual(self.service.configuration, before)
        self.assertEqual(self.service.settings_revision, 0)
        self.assertFalse(self.service.audio_capture_enabled)
        self.assertFalse(self.service.confirmation_required)
        self.assertEqual(self.service.pending_confirmation[0], 'keep me')
        self.socket.send_json.assert_not_awaited()

    async def test_legacy_toggle_invalidates_open_settings_form(self):
        self.service.set_audio_capture_enabled(True)
        response = await self.put({'tts_speed': 1.4}, revision=0)
        self.assertEqual(response.status, 409)
        self.assertTrue((await (await self.client.get('/api/settings')).json())['values']['audio_capture'])

    async def test_restart_is_fixed_unit_and_rejects_stale_revision(self):
        process = SimpleNamespace(wait=AsyncMock(return_value=0))
        with patch('settings_api.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process) as launch:
            response = await self.client.post('/api/settings/restart', json={'revision': 1})
            self.assertEqual(response.status, 409)
            launch.assert_not_awaited()
            response = await self.client.post('/api/settings/restart', json={'revision': 0})
            self.assertEqual(response.status, 200)
            self.assertEqual(launch.call_args.args, ('systemctl', '--user', '--no-block', 'restart', 'openclaw-live-conversation.service'))
            process.wait.return_value = 1
            response = await self.client.post('/api/settings/restart', json={'revision': 0})
            self.assertEqual(response.status, 503)

    async def test_saved_history_capacity_is_applied_on_construction(self):
        response = await self.put({'max_history_messages': 200, 'max_conversation_events': 750})
        self.assertEqual(response.status, 200)
        history_path = Path(self.temp.name) / 'history.json'
        history_path.write_text(json.dumps({'messages': [{'role': 'user', 'content': str(i)} for i in range(150)], 'events': [{'type': 'message', 'id': str(i)} for i in range(600)]}))
        restarted = LiveConversationService('test:settings', 1.08, settings_path=str(self.path), history_path=str(history_path))
        self.assertEqual(len(restarted.history), 150)
        self.assertEqual(len(restarted.events), 600)
        self.assertEqual(restarted.history.maxlen, 200)
        self.assertEqual(restarted.events.maxlen, 750)

    async def test_transcriber_uses_active_not_pending_settings(self):
        self.service.active_configuration = validate_settings({
            'asr_beam_size': 7, 'asr_best_of': 8, 'asr_temperature': .2,
            'asr_hotwords': 'special term', 'asr_vad_final_threshold': .4,
            'asr_vad_wake_threshold': .55, 'asr_vad_partial_threshold': .65,
            'asr_min_speech_duration_ms': 400, 'asr_min_silence_duration_ms': 500,
            'asr_speech_pad_final_ms': 600, 'asr_speech_pad_partial_ms': 700,
        })
        self.service.configuration['asr_beam_size'] = 11
        model = MagicMock()
        model.transcribe.return_value = ([SimpleNamespace(text='hello', no_speech_prob=.1)], None)
        self.service.stt = SimpleNamespace(_model=model)
        for purpose, threshold, padding in [('final', .4, 600), ('wake', .55, 700), ('partial', .65, 700)]:
            self.assertEqual(await self.service.transcribe(bytes(320), purpose), 'hello')
            args = model.transcribe.call_args.kwargs
            self.assertEqual(args['beam_size'], 7)
            self.assertEqual(args['best_of'], 8)
            self.assertEqual(args['temperature'], .2)
            self.assertEqual(args['hotwords'], 'special term')
            self.assertEqual(args['vad_parameters'], dict(threshold=threshold, min_speech_duration_ms=400, min_silence_duration_ms=500, speech_pad_ms=padding))

    def test_tts_configuration_reaches_worker_and_http_client(self):
        config = validate_settings({'tts_speaker_id': 4, 'tts_threads': 3, 'tts_speed': 1.4})
        worker = configured_tts_backend(config['tts_speed'], config)
        self.assertEqual((worker.speaker_id, worker.threads, worker.speed), (4, 3, 1.4))
        config = validate_settings({'tts_backend': 'qwen', 'qwen_tts_voice': 'aiden', 'qwen_tts_instructions': 'Speak quietly', 'qwen_tts_initial_chunk_frames': 12, 'qwen_tts_startup_wait_seconds': 30.0, 'qwen_tts_temperature': .65, 'qwen_tts_top_p': .9, 'qwen_tts_top_k': 30, 'qwen_tts_repetition_penalty': 1.1, 'qwen_tts_seed': 7})
        client = configured_tts_backend(config['tts_speed'], config)
        self.assertEqual((client.voice, client.instructions, client.initial_chunk_frames, client.startup_wait_seconds), ('aiden', 'Speak quietly', 12, 30.0))
        self.assertEqual((client.temperature, client.top_p, client.top_k, client.repetition_penalty, client.seed), (.65, .9, 30, 1.1, 7))


if __name__ == '__main__':
    unittest.main()
