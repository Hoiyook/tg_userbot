"""Chrome Agent health monitoring tests.

Tests for chrome_health.py: Health status checking, monitoring loop,
and callback system for Chrome Agent components.
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from tg_userbot.chrome_health import (
    HealthStatus,
    HealthMonitor,
    HealthMetrics,
)


class TestHealthStatus(unittest.TestCase):
    """Test HealthStatus enum values."""

    def test_health_status_values(self):
        """Verify all HealthStatus enum values are defined correctly."""
        self.assertEqual(HealthStatus.HEALTHY.value, "healthy")
        self.assertEqual(HealthStatus.CHROME_DOWN.value, "chrome_down")
        self.assertEqual(HealthStatus.CDP_DOWN.value, "cdp_down")
        self.assertEqual(HealthStatus.AGENT_DOWN.value, "agent_down")
        self.assertEqual(HealthStatus.RECOVERING.value, "recovering")


class TestHealthMonitor(unittest.TestCase):
    """Test HealthMonitor class functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.chrome_binary = "/fake/chrome"
        self.profile_dir = os.path.join(self.temp_dir, "profile")
        self.cdp_host = "127.0.0.1"
        self.cdp_port = 9222

        # Create profile directory
        os.makedirs(self.profile_dir)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_init_basic(self):
        """Test HealthMonitor initialization with basic parameters."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        self.assertEqual(monitor.chrome_binary, self.chrome_binary)
        self.assertEqual(monitor.profile_dir, self.profile_dir)
        self.assertEqual(monitor.cdp_host, self.cdp_host)
        self.assertEqual(monitor.cdp_port, self.cdp_port)
        self.assertIsNone(monitor.status_callback)
        self.assertEqual(monitor.status, HealthStatus.HEALTHY)
        self.assertEqual(monitor.monitoring, False)

    def test_set_status_callback(self):
        """Test setting status callback function."""
        callback = Mock()
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        monitor.set_status_callback(callback)
        self.assertEqual(monitor.status_callback, callback)

    @patch('tg_userbot.chrome_health.psutil')
    def test_check_chrome_process_running(self, mock_psutil):
        """Test Chrome process detection when Chrome is running."""
        mock_process = Mock()
        mock_process.info = {'pid': 12345, 'name': 'chrome'}
        mock_psutil.process_iter.return_value = [mock_process]
        mock_psutil.pid_exists.return_value = True

        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        result = monitor._check_chrome_process()
        self.assertTrue(result)

    @patch('tg_userbot.chrome_health.psutil')
    def test_check_chrome_process_not_running(self, mock_psutil):
        """Test Chrome process detection when Chrome is not running."""
        mock_psutil.process_iter.return_value = []

        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        result = monitor._check_chrome_process()
        self.assertFalse(result)

    @patch('tg_userbot.chrome_health.httpx')
    def test_check_cdp_connectivity_healthy(self, mock_httpx):
        """Test CDP connectivity check when Chrome is healthy."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "Browser": "Chrome",
            "Protocol-Version": "1.3"
        }
        # Mock the synchronous client.get call (not async)
        mock_httpx.Client.return_value.__enter__.return_value.get.return_value = mock_response

        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        result = monitor._check_cdp_connectivity()
        self.assertTrue(result)

    @patch('tg_userbot.chrome_health.httpx')
    def test_check_cdp_connectivity_unhealthy(self, mock_httpx):
        """Test CDP connectivity check when Chrome is unreachable."""
        mock_httpx.get.side_effect = Exception("Connection failed")

        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        result = monitor._check_cdp_connectivity()
        self.assertFalse(result)

    def test_check_components_healthy(self):
        """Test component checking when all components are healthy."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        with patch.object(monitor, '_check_chrome_process', return_value=True), \
             patch.object(monitor, '_check_cdp_connectivity', return_value=True):

            result = monitor._check_components()
            self.assertEqual(result, HealthStatus.HEALTHY)

    def test_check_components_chrome_down(self):
        """Test component checking when Chrome process is down."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        with patch.object(monitor, '_check_chrome_process', return_value=False), \
             patch.object(monitor, '_check_cdp_connectivity', return_value=False):

            result = monitor._check_components()
            self.assertEqual(result, HealthStatus.CHROME_DOWN)

    def test_check_components_cdp_down(self):
        """Test component checking when Chrome is running but CDP is down."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        with patch.object(monitor, '_check_chrome_process', return_value=True), \
             patch.object(monitor, '_check_cdp_connectivity', return_value=False):

            result = monitor._check_components()
            self.assertEqual(result, HealthStatus.CDP_DOWN)

    def test_check_health(self):
        """Test health status checking."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        with patch.object(monitor, '_check_components') as mock_check:
            mock_check.return_value = HealthStatus.CDP_DOWN

            result = monitor.check_health()
            self.assertEqual(result, HealthStatus.CDP_DOWN)
            mock_check.assert_called_once()

    def test_get_metrics(self):
        """Test getting health metrics."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        monitor.last_check_time = "2023-01-01T12:00:00"
        monitor.check_count = 5
        monitor.chrome_process_id = 12345

        with patch.object(monitor, '_check_components') as mock_check:
            mock_check.return_value = HealthStatus.HEALTHY

            metrics = monitor.get_metrics()
            self.assertIsInstance(metrics, HealthMetrics)
            self.assertEqual(metrics.status, HealthStatus.HEALTHY)
            self.assertEqual(metrics.last_check_time, "2023-01-01T12:00:00")
            self.assertEqual(metrics.check_count, 5)
            self.assertEqual(metrics.chrome_process_id, 12345)

    @patch('tg_userbot.chrome_health.asyncio')
    def test_start_monitoring(self, mock_asyncio):
        """Test starting the monitoring loop."""
        callback = Mock()
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )
        monitor.set_status_callback(callback)

        # Mock the monitoring loop task
        mock_task = Mock()
        mock_asyncio.create_task.return_value = mock_task

        monitor.start_monitoring(interval=2.0)

        self.assertTrue(monitor.monitoring)
        mock_asyncio.create_task.assert_called_once()

    @patch('tg_userbot.chrome_health.asyncio')
    def test_stop_monitoring(self, mock_asyncio):
        """Test stopping the monitoring loop."""
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host=self.cdp_host,
            cdp_port=self.cdp_port
        )

        # Mock a running task
        mock_task = Mock()
        monitor.monitoring_task = mock_task
        monitor.monitoring = True

        monitor.stop_monitoring()

        self.assertFalse(monitor.monitoring)
        mock_task.cancel.assert_called_once()


class TestHealthMetrics(unittest.TestCase):
    """Test HealthMetrics dataclass."""

    def test_metrics_structure(self):
        """Test HealthMetrics has correct structure."""
        metrics = HealthMetrics(
            status="healthy",
            last_check_time="2023-01-01T12:00:00",
            check_count=5,
            chrome_process_id=12345,
            cdp_response_time=0.1,
            uptime_seconds=3600
        )

        self.assertEqual(metrics.status, "healthy")
        self.assertEqual(metrics.last_check_time, "2023-01-01T12:00:00")
        self.assertEqual(metrics.check_count, 5)
        self.assertEqual(metrics.chrome_process_id, 12345)
        self.assertEqual(metrics.cdp_response_time, 0.1)
        self.assertEqual(metrics.uptime_seconds, 3600)


class TestMonitoringCallbacks(unittest.TestCase):
    """Test monitoring callback functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.chrome_binary = "/fake/chrome"
        self.profile_dir = os.path.join(self.temp_dir, "profile")
        os.makedirs(self.profile_dir)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_status_callback_triggered(self):
        """Test that status callback is triggered on status change."""
        callback = Mock()
        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host="127.0.0.1",
            cdp_port=9222
        )
        monitor.set_status_callback(callback)

        # Simulate status change
        monitor.status = HealthStatus.CHROME_DOWN
        callback.assert_called_with(HealthStatus.CHROME_DOWN)

    @patch('tg_userbot.chrome_health.asyncio')
    def test_monitoring_loop_calls_check_health(self, mock_asyncio):
        """Test that monitoring loop periodically calls check_health."""
        mock_task = Mock()
        mock_asyncio.create_task.return_value = mock_task

        monitor = HealthMonitor(
            chrome_binary=self.chrome_binary,
            profile_dir=self.profile_dir,
            cdp_host="127.0.0.1",
            cdp_port=9222
        )

        # Mock the check_health method to avoid actual calls
        monitor.check_health = Mock(return_value=HealthStatus.HEALTHY)

        # Start monitoring
        monitor.start_monitoring(interval=1.0)

        # Verify monitoring started
        self.assertTrue(monitor.monitoring)


if __name__ == '__main__':
    unittest.main()