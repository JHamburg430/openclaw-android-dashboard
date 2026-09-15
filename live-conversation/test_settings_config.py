"""Pure settings validation tests: no audio/model dependencies required."""
import unittest
from settings_config import GLOBAL_KEYS, SETTINGS_SCHEMA, defaults, validate_settings


class SettingsValidationTest(unittest.TestCase):
    def test_factory_defaults_are_valid_and_independent(self):
        expected = defaults()
        self.assertEqual(validate_settings({}), expected)
        self.assertEqual(len(expected), len(SETTINGS_SCHEMA))
        expected['speech_model'] = 'changed'
        self.assertEqual(defaults()['speech_model'], 'openclaw-live-conversation:4b')
        self.assertTrue(set(GLOBAL_KEYS) <= set(defaults()))

    def test_partial_merge_preserves_deployment_overrides(self):
        current = {'tts_backend': 'qwen', 'tts_speed': 1.2, 'audio_capture': True}
        patch = {'browser_speech_threshold': .007}
        merged = validate_settings(patch, current)
        self.assertEqual(merged['tts_backend'], 'qwen')
        self.assertEqual(merged['tts_speed'], 1.2)
        self.assertTrue(merged['audio_capture'])
        self.assertEqual(patch, {'browser_speech_threshold': .007})
        self.assertEqual(len(current), 3)

    def test_type_finite_unknown_and_range_rejections(self):
        for patch in [None, [], {'unknown': 1}, {'tts_speed': True},
                      {'tts_speed': float('nan')}, {'tts_speed': float('inf')},
                      {'tts_threads': 1.5}, {'tts_threads': '8'},
                      {'tts_threads': 0}, {'tts_threads': 33},
                      {'audio_capture': 1}, {'tts_speed': 10 ** 10000},
                      {'qwen_tts_instructions': 'x' * 2001},
                      {'qwen_tts_voice': '  '}, {'qwen_tts_voice': 'a\x00b'},
                      {'speech_model': 'model; shell'}, {'tts_backend': 'shell'},
                      {'qwen_tts_url': 'https://example.com'}, {'asr_model': '/tmp/model'}]:
            with self.subTest(patch=str(patch)[:100] if not isinstance(patch, dict) or 'tts_speed' not in patch else 'speed'):
                with self.assertRaises(ValueError):
                    validate_settings(patch)
        with self.assertRaises(ValueError):
            validate_settings({}, {'stale_unknown_key': 3})

    def test_cross_field_validation_and_atomic_multi_key_changes(self):
        for patch in [
            {'online_asr_overlap_seconds': 14.0},
            {'speech_num_predict': 700},
            {'speech_retry_num_predict': 8192, 'speech_model_context': 4096},
            {'max_history_messages': 8, 'prompt_history_messages': 9},
            {'prompt_history_chars': 80000},
            {'browser_semantic_check_ms': 5000},
            {'browser_speech_threshold': .05},
            {'asr_device': 'cpu'},
        ]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_settings(patch)
        self.assertEqual(validate_settings({'asr_device': 'cpu', 'asr_compute_type': 'int8'})['asr_device'], 'cpu')
        merged = validate_settings({'speech_num_predict': 800, 'speech_retry_num_predict': 900})
        self.assertEqual(merged['speech_num_predict'], 800)

    def test_schema_complete_metadata_and_application_modes(self):
        for entry in SETTINGS_SCHEMA:
            self.assertEqual(set(entry), {'key', 'label', 'group', 'type', 'default', 'min', 'max', 'options', 'description', 'apply'})
            expected_apply = ('reconnect' if entry['key'].startswith('browser_') else
                              'immediate' if entry['key'] in ('action_confirmation', 'audio_capture') else 'restart')
            self.assertEqual(entry['apply'], expected_apply)
            self.assertNotIn('path', entry['key'])
            self.assertNotIn('url', entry['key'])


if __name__ == '__main__':
    unittest.main()
