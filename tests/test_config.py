import unittest
from unittest.mock import patch, mock_open
import os
import tempfile
import json

from tg_userbot import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        # Reset any environment variables that might affect config loading
        self.env_vars_to_restore = {}

    def tearDown(self):
        # Restore environment variables
        for key, value in self.env_vars_to_restore.items():
            os.environ[key] = value
        # Remove any environment variables we set
        for key in os.environ:
            if key.startswith('TG_') and key not in self.env_vars_to_restore:
                del os.environ[key]

    def test_is_termux(self):
        # Test when TERMUX environment variable is not set
        with patch.dict(os.environ, {}, clear=False):
            self.assertFalse(config.is_termux())

        # Test when TERMUX environment variable is set to "true"
        os.environ['TERMUX'] = 'true'
        self.assertTrue(config.is_termux())

        # Test when TERMUX environment variable is set to "false"
        os.environ['TERMUX'] = 'false'
        self.assertFalse(config.is_termux())

        # Test when TERMUX environment variable is set to "1"
        os.environ['TERMUX'] = '1'
        self.assertTrue(config.is_termux())

    def test_is_mingw(self):
        # Test when MINGW environment variable is not set
        with patch.dict(os.environ, {}, clear=False):
            self.assertFalse(config.is_mingw())

        # Test when MINGW environment variable is set to "true"
        os.environ['MINGW'] = 'true'
        self.assertTrue(config.is_mingw())

        # Test when MINGW environment variable is set to "false"
        os.environ['MINGW'] = 'false'
        self.assertFalse(config.is_mingw())

    def test_secrets_loading(self):
        test_secrets = {
            'api_id': 12345,
            'api_hash': 'test_hash',
            'bot_token': 'test:token',
            'bot_username': 'test_bot',
            'tg_proxy': 'socks5://127.0.0.1:7890',
            'douyin_cookie': 'test_cookie'
        }

        with patch('builtins.open', mock_open(read_data=json.dumps(test_secrets))):
            with patch('os.path.exists', return_value=True):
                # Test with default secrets file
                config.load_secrets()
                self.assertEqual(config.API_ID, 12345)
                self.assertEqual(config.API_HASH, 'test_hash')
                self.assertEqual(config.BOT_TOKEN, 'test:token')
                self.assertEqual(config.BOT_USERNAME, 'test_bot')
                self.assertEqual(config.TG_PROXY, 'socks5://127.0.0.1:7890')
                self.assertEqual(config.DOUYIN_COOKIE, 'test_cookie')

                # Test with custom secrets file
                custom_path = '/path/to/custom/secrets.json'
                config.load_secrets(custom_path)
                # Should still have loaded the test data

    def test_secrets_loading_missing_file(self):
        with patch('os.path.exists', return_value=False):
            config.load_secrets()
            # Should not crash and should use defaults
            self.assertIsNone(config.API_ID)
            self.assertIsNone(config.API_HASH)
            self.assertIsNone(config.BOT_TOKEN)
            self.assertIsNone(config.BOT_USERNAME)
            self.assertIsNone(config.TG_PROXY)
            self.assertEqual(config.DOUYIN_COOKIE, '')

    def test_secrets_loading_partial_file(self):
        test_secrets = {
            'api_id': 12345,
            'bot_token': 'test:token'
        }

        with patch('builtins.open', mock_open(read_data=json.dumps(test_secrets))):
            with patch('os.path.exists', return_value=True):
                config.load_secrets()
                # Should have loaded available values and keep defaults for missing ones
                self.assertEqual(config.API_ID, 12345)
                self.assertEqual(config.BOT_TOKEN, 'test:token')
                self.assertIsNone(config.API_HASH)
                self.assertIsNone(config.BOT_USERNAME)
                self.assertIsNone(config.TG_PROXY)
                self.assertEqual(config.DOUYIN_COOKIE, '')

    def test_platform_links(self):
        # Test that PLATFORM_LINKS is correctly configured
        self.assertIn('douyin', config.PLATFORM_LINKS)
        self.assertIn('instagram', config.PLATFORM_LINKS)

        douyin_link = config.PLATFORM_LINKS['douyin']
        self.assertIn('bot_username', douyin_link)
        self.assertIn('log_label', douyin_link)

        instagram_link = config.PLATFORM_LINKS['instagram']
        self.assertIn('bot_username', instagram_link)
        self.assertIn('log_label', instagram_link)

    def test_task_events_constants(self):
        # Test task events configuration constants
        self.assertIsInstance(config.TASK_EVENTS_MAX_EVENTS, int)
        self.assertGreater(config.TASK_EVENTS_MAX_EVENTS, 0)

        # Test valid event types
        valid_events = [
            'RECEIVED', 'QUEUED', 'RUNNING', 'SUCCESS', 'FAILED',
            'RETRY', 'CANCELLED', 'REMOVED', 'DEDUP_HIT', 'DEDUP_SKIPPED'
        ]
        for event_type in valid_events:
            self.assertIn(event_type, config.TASK_EVENT_TYPES)

    def test_find_constants(self):
        # Test find functionality constants
        self.assertIsInstance(config.FIND_INPUT_WINDOW_SECONDS, int)
        self.assertGreater(config.FIND_INPUT_WINDOW_SECONDS, 0)

        self.assertIsInstance(config.LIST_PAGE_SIZE, int)
        self.assertGreater(config.LIST_PAGE_SIZE, 0)

        self.assertIsInstance(config.FIND_INPUT_UNTIL, type(None))

    def test_chrome_agent_constants(self):
        # Test Chrome Agent constants
        self.assertIsInstance(config.CHROME_AGENT_PID_FILE, str)
        self.assertTrue(config.CHROME_AGENT_PID_FILE.endswith('.pid'))

        # Test Chrome Agent constants that should exist
        self.assertIsInstance(config.CHROME_HEALTH_CHECK_INTERVAL, float)
        self.assertIsInstance(config.CHROME_RECOVERY_TIMEOUT, float)
        self.assertIsInstance(config.CHROME_BACKUP_COUNT, int)
        self.assertIsInstance(config.CHROME_GUID_VALIDITY_SECONDS, int)
        self.assertIsInstance(config.CHROME_MAX_GUID_AGE, int)
        self.assertIsInstance(config.CHROME_MIN_PROGRESS_INTERVAL, int)

        # Test reasonable values
        self.assertGreater(config.CHROME_HEALTH_CHECK_INTERVAL, 0)
        self.assertGreater(config.CHROME_RECOVERY_TIMEOUT, 0)
        self.assertGreater(config.CHROME_BACKUP_COUNT, 0)
        self.assertGreater(config.CHROME_GUID_VALIDITY_SECONDS, 0)
        self.assertGreater(config.CHROME_MAX_GUID_AGE, 0)
        self.assertGreater(config.CHROME_MIN_PROGRESS_INTERVAL, 0)


if __name__ == '__main__':
    unittest.main()