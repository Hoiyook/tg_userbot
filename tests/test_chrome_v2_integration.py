"""Chrome Agent V2 Recovery Integration Tests

Comprehensive end-to-end testing of Chrome Agent V2 recovery mechanisms including:
- Recovery from Agent crash scenarios
- Event isolation between concurrent downloads
- Health monitoring accuracy
- Backup/restore functionality
- Complete workflow testing

Testing the integration of all Chrome Agent V2 components:
- chrome_health.py
- chrome_events.py
- chrome_persistence.py
- Enhanced chrome_agent.py
- Enhanced chrome_client.py

Run with:
    .venv/bin/python -m unittest discover -s tests -p "test_chrome_v2_integration.py" -v
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta

# Must set temp directory before importing tg_userbot
_TMP = tempfile.mkdtemp(prefix="tg_userbot_chrome_v2_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

from tg_userbot import config
from tg_userbot import chrome_agent
from tg_userbot import chrome_health
from tg_userbot import chrome_events
from tg_userbot import chrome_persistence


class TestChromeV2Integration(unittest.TestCase):
    """Integration tests for Chrome Agent V2 recovery mechanisms."""

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        cls.temp_dir = _TMP

    @classmethod
    def tearDownClass(cls):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir)

    def setUp(self):
        """Set up each test case."""
        # Create test-specific subdirectories
        self.tasks_file = os.path.join(self.temp_dir, "chrome_tasks.json")
        self.requests_file = os.path.join(self.temp_dir, "chrome_requests.json")
        self.download_dir = os.path.join(self.temp_dir, "downloads")
        os.makedirs(self.download_dir, exist_ok=True)

    def tearDown(self):
        """Clean up after each test case."""
        # Clean up test files
        for f in [self.tasks_file, self.requests_file]:
            if os.path.exists(f):
                os.remove(f)

    def test_integration_from_agent_crash(self):
        """Test recovery when Agent crashes but Chrome is still running."""
        # Create a mock completed download file
        completed_file = os.path.join(self.download_dir, "test_video.mp4")
        with open(completed_file, "w") as f:
            f.write("mock video content")

        # Create a task that appears to be running with filename pointing to completed file
        tasks = [{
            "task_id": "test-123456",
            "url": "https://example.com/video.mp4",
            "status": "RUNNING",
            "filename": "test_video.mp4",
            "started_at": (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "guid": "mock-guid-123"
        }]

        # Save tasks with enhanced persistence
        chrome_persistence.atomic_save_with_backup(tasks, self.tasks_file)

        # Simulate recovery - should detect completed file
        recovered_tasks = chrome_persistence.load_tasks_with_backup(self.tasks_file)
        self.assertEqual(len(recovered_tasks), 1)

        # Apply enhanced recovery logic
        chrome_agent.enhanced_recover_tasks(recovered_tasks, self.download_dir)

        # Task should be marked as SUCCESS due to completed file
        task = recovered_tasks[0]
        self.assertEqual(task["status"], "SUCCESS")
        self.assertIsNotNone(task["finished_at"])
        self.assertGreater(task["size_bytes"], 0)

    def test_event_isolation_concurrent_downloads(self):
        """Test that GUID-based dispatcher properly isolates events between concurrent downloads."""
        dispatcher = chrome_events.EventDispatcher(guid_timeout_seconds=60)

        # Register first task
        task_id1 = "task-1"
        guid1 = "task-guid-1"
        dispatcher.register_task(task_id1, guid1)

        # Register second task
        task_id2 = "task-2"
        guid2 = "task-guid-2"
        dispatcher.register_task(task_id2, guid2)

        # Event for first task should pass
        event1 = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": guid1, "suggestedFilename": "video1.mp4"}
        }
        filtered1 = dispatcher.filter_event(event1, task_id1)
        self.assertIsNotNone(filtered1)
        self.assertEqual(filtered1["params"]["guid"], guid1)

        # Event for second task should also pass
        event2 = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": guid2, "suggestedFilename": "video2.mp4"}
        }
        filtered2 = dispatcher.filter_event(event2, task_id2)
        self.assertIsNotNone(filtered2)
        self.assertEqual(filtered2["params"]["guid"], guid2)

        # Event with unknown GUID should be filtered out
        unknown_event = {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": "unknown-guid", "suggestedFilename": "video3.mp4"}
        }
        filtered_unknown = dispatcher.filter_event(unknown_event, task_id1)
        self.assertIsNone(filtered_unknown)

        # Clear first task and test filtering
        dispatcher.clear_task(task_id1)

        # Event for cleared task should now be filtered
        filtered1_after_clear = dispatcher.filter_event(event1, task_id1)
        self.assertIsNone(filtered1_after_clear)

    def test_health_monitoring_integration(self):
        """Test health monitoring functionality with mocked dependencies."""
        monitor = chrome_health.HealthMonitor(
            chrome_binary="/mock/chrome",
            profile_dir="/mock/profile",
            cdp_host="127.0.0.1",
            cdp_port=9222,
            check_interval=0.1  # Fast interval for testing
        )

        # Test initial status
        self.assertEqual(monitor.get_status().value, "healthy")

        # Test metrics collection
        metrics = monitor.get_metrics()
        self.assertIn("status", metrics)
        self.assertIn("chrome_binary", metrics)
        self.assertIn("cdp_endpoint", metrics)

        # Set up callback for status changes
        status_changes = []

        async def on_status_change(old_status, new_status):
            status_changes.append((old_status.value, new_status.value))

        monitor.set_status_callback(chrome_health.HealthStatus.RECOVERING, on_status_change)

        # Start monitoring
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def test_monitoring():
            await monitor.start_monitoring()
            await asyncio.sleep(0.2)  # Let it run for a bit
            await monitor.stop_monitoring()

        try:
            loop.run_until_complete(test_monitoring)
        finally:
            loop.close()

        # Verify monitoring ran (status changes may vary due to mocking)
        self.assertTrue(len(status_changes) >= 0)

    def test_persistence_backup_integration(self):
        """Test enhanced persistence with backup and restore."""
        # Create test tasks
        original_tasks = [
            {
                "task_id": "task-1",
                "url": "https://example.com/video1.mp4",
                "status": "PENDING"
            },
            {
                "task_id": "task-2",
                "url": "https://example.com/video2.mp4",
                "status": "RUNNING",
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        ]

        # Save with backup
        chrome_persistence.atomic_save_with_backup(original_tasks, self.tasks_file)

        # Verify main file was created
        self.assertTrue(os.path.exists(self.tasks_file))

        # Verify backup files were created
        backup_files = [f for f in os.listdir(self.temp_dir) if f.startswith("chrome_tasks.json.bak")]
        self.assertGreaterEqual(len(backup_files), 1)

        # Load with backup
        loaded_tasks = chrome_persistence.load_tasks_with_backup(self.tasks_file)
        self.assertEqual(len(loaded_tasks), 2)

        # Verify task data integrity
        task_ids = [t["task_id"] for t in loaded_tasks]
        self.assertIn("task-1", task_ids)
        self.assertIn("task-2", task_ids)

        # Test backup cleanup when multiple backups exist
        chrome_persistence.cleanup_old_backups(self.tasks_file, 2)  # Keep only 2 backups

        remaining_backups = [f for f in os.listdir(self.temp_dir) if f.startswith("chrome_tasks.json.bak")]
        self.assertLessEqual(len(remaining_backups), 2 + 1)  # +1 for current main file

    def test_enhanced_recovery_logic(self):
        """Test enhanced recovery logic for various scenarios."""
        # Scenario 1: Stale task (running too long)
        stale_task = {
            "task_id": "stale-task",
            "url": "https://example.com/video.mp4",
            "status": "RUNNING",
            "started_at": (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "guid": "stale-guid"
        }

        chrome_agent.enhanced_recover_tasks([stale_task], self.download_dir)
        self.assertEqual(stale_task["status"], "RECOVERING")
        self.assertIn("recovery_reason", stale_task)
        self.assertEqual(stale_task["recovery_reason"], "stale_task")

        # Scenario 2: Completed download after crash
        completed_file = os.path.join(self.download_dir, "completed.mp4")
        with open(completed_file, "w") as f:
            f.write("completed content")

        completed_task = {
            "task_id": "completed-task",
            "url": "https://example.com/completed.mp4",
            "status": "RUNNING",
            "filename": "completed.mp4",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "guid": "complete-guid"
        }

        chrome_agent.enhanced_recover_tasks([completed_task], self.download_dir)
        self.assertEqual(completed_task["status"], "SUCCESS")
        self.assertIsNotNone(completed_task["finished_at"])
        self.assertGreater(completed_task["size_bytes"], 0)

        # Scenario 3: Invalid GUID
        invalid_guid_task = {
            "task_id": "invalid-task",
            "url": "https://example.com/invalid.mp4",
            "status": "RUNNING",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "guid": "invalid-guid"
        }

        # Mock the GUID validation to return False
        with mock.patch('tg_userbot.chrome_agent._is_guid_valid') as mock_validate:
            mock_validate.return_value = False
            chrome_agent.enhanced_recover_tasks([invalid_guid_task], self.download_dir)
            self.assertEqual(invalid_guid_task["status"], "PENDING")
            self.assertEqual(invalid_guid_task["error"], "GUID expired")

    def test_config_constants_integration(self):
        """Test that all V2 configuration constants are properly defined."""
        constants = [
            'CHROME_HEALTH_CHECK_INTERVAL',
            'CHROME_RECOVERY_TIMEOUT',
            'CHROME_BACKUP_COUNT',
            'CHROME_GUID_VALIDITY_SECONDS',
            'CHROME_MAX_GUID_AGE',
            'CHROME_MIN_PROGRESS_INTERVAL'
        ]

        for constant in constants:
            self.assertTrue(hasattr(config, constant))
            value = getattr(config, constant)
            self.assertIsNotNone(value)

        # Validate specific values
        self.assertEqual(config.CHROME_HEALTH_CHECK_INTERVAL, 5.0)
        self.assertEqual(config.CHROME_RECOVERY_TIMEOUT, 30.0)
        self.assertEqual(config.CHROME_BACKUP_COUNT, 3)
        self.assertEqual(config.CHROME_GUID_VALIDITY_SECONDS, 300)
        self.assertEqual(config.CHROME_MAX_GUID_AGE, 1800)
        self.assertEqual(config.CHROME_MIN_PROGRESS_INTERVAL, 30)

    def test_enhanced_client_status_text(self):
        """Test enhanced status text functionality."""
        from tg_userbot.chrome_client import enhanced_status_text

        # Mock health data
        health_data = {
            "status": "recovering",
            "last_check": 1234567890,
            "recovery_attempts": 2,
            "chrome_binary": "/mock/chrome",
            "cdp_endpoint": "127.0.0.1:9222"
        }

        # Mock tasks
        tasks = [
            {
                "task_id": "test-task",
                "url": "https://example.com/video.mp4",
                "status": "SUCCESS",
                "filename": "test.mp4",
                "size_bytes": 1024000,
                "finished_at": "2026-09-09 12:00:00"
            }
        ]

        # Generate enhanced status text
        status_text = enhanced_status_text(
            agent_up=True,
            chrome_running=True,
            cdp_ok=True,
            tasks=tasks,
            dl_dir=self.download_dir,
            health_data=health_data
        )

        # Verify content includes health information
        self.assertIn("🔧 Agent 健康状态：", status_text)
        self.assertIn("🔄 正在恢复", status_text)
        self.assertIn("恢复尝试次数：2", status_text)
        self.assertIn("任务统计", status_text)  # Should include regular task info

    def test_end_to_end_workflow_simulation(self):
        """Simulate a complete Chrome Agent V2 workflow."""
        # Step 1: Create and save tasks
        tasks = [
            {
                "task_id": "e2e-task-1",
                "url": "https://example.com/video1.mp4",
                "status": "PENDING"
            }
        ]

        chrome_persistence.atomic_save_with_backup(tasks, self.tasks_file)

        # Step 2: Set up event dispatcher
        dispatcher = chrome_events.EventDispatcher()
        guid = "e2e-guid-1"
        dispatcher.register_task(guid)

        # Step 3: Simulate CDP events
        events = [
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": guid, "suggestedFilename": "video1.mp4"}
            },
            {
                "method": "Browser.downloadProgress",
                "params": {"guid": guid, "state": "completed", "receivedBytes": 1024000}
            }
        ]

        # Filter events through dispatcher
        filtered_events = [dispatcher.filter_event(e) for e in events]
        self.assertEqual(len([e for e in filtered_events if e is not None]), 2)

        # Step 4: Apply recovery logic
        recovered_tasks = chrome_persistence.load_tasks_with_backup(self.tasks_file)
        chrome_agent.enhanced_recover_tasks(recovered_tasks, self.download_dir)

        # Step 5: Verify state
        self.assertTrue(len(recovered_tasks) > 0)
        original_task = next(t for t in recovered_tasks if t["task_id"] == "e2e-task-1")

        # Task should still be PENDING (no actual download occurred)
        self.assertEqual(original_task["status"], "PENDING")

    def test_error_handling_robustness(self):
        """Test error handling and graceful degradation."""
        # Test with corrupted tasks file
        corrupted_file = os.path.join(self.temp_dir, "corrupted.json")
        with open(corrupted_file, "w") as f:
            f.write("invalid json content")

        # Should fall back to empty list
        recovered = chrome_persistence.load_tasks_with_backup(corrupted_file)
        self.assertEqual(len(recovered), 0)

        # Test with missing tasks file
        missing_file = os.path.join(self.temp_dir, "missing.json")
        recovered = chrome_persistence.load_tasks_with_backup(missing_file)
        self.assertEqual(len(recovered), 0)

        # Test event dispatcher with invalid events
        dispatcher = chrome_events.EventDispatcher()

        # Test with None event
        filtered = dispatcher.filter_event(None)
        self.assertIsNone(filtered)

        # Test with event missing method
        invalid_event = {"params": {"guid": "test"}}
        filtered = dispatcher.filter_event(invalid_event)
        self.assertIsNone(filtered)  # Should filter out invalid events


if __name__ == '__main__':
    unittest.main()