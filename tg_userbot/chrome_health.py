"""Chrome Agent V2 Health Monitoring System."""

import asyncio
import os
import subprocess
import time
from datetime import datetime
from typing import Dict, Any, Optional, Callable
from enum import Enum


class HealthStatus(Enum):
    HEALTHY = "healthy"
    CHROME_DOWN = "chrome_down"
    CDP_DOWN = "cdp_down"
    AGENT_DOWN = "agent_down"
    RECOVERING = "recovering"


class HealthMonitor:
    """Monitors Chrome Agent health and triggers recovery."""

    def __init__(self, chrome_binary: str, profile_dir: str,
                 cdp_host: str, cdp_port: int,
                 check_interval: float = 5.0):
        self.chrome_binary = chrome_binary
        self.profile_dir = profile_dir
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.check_interval = check_interval

        self._status = HealthStatus.HEALTHY
        self._callbacks: Dict[HealthStatus, Callable] = {}
        self._monitoring_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def set_status_callback(self, status: HealthStatus, callback: Callable) -> None:
        """Set callback for specific status change."""
        self._callbacks[status] = callback

    async def start_monitoring(self) -> None:
        """Start continuous health monitoring."""
        self._stop_event.clear()
        self._monitoring_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitoring(self) -> None:
        """Stop health monitoring."""
        self._stop_event.set()
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None

    async def _monitor_loop(self) -> None:
        """Continuous monitoring loop."""
        while not self._stop_event.is_set():
            await self.check_health()
            await asyncio.sleep(self.check_interval)

    async def check_health(self) -> HealthStatus:
        """Comprehensive health check."""
        new_status = await self._check_components()

        if new_status != self._status:
            old_status = self._status
            self._status = new_status

            # Trigger callback if registered
            callback = self._callbacks.get(new_status)
            if callback:
                try:
                    await callback(old_status, new_status)
                except Exception:
                    pass

        return self._status

    async def _check_components(self) -> HealthStatus:
        """Check individual components."""
        # Check Chrome instance
        if not self._check_chrome_process():
            return HealthStatus.CHROME_DOWN

        # Check CDP connection
        if not await self._check_cdp_connectivity():
            return HealthStatus.CDP_DOWN

        return HealthStatus.HEALTHY

    def _check_chrome_process(self) -> bool:
        """Check if Chrome process is running."""
        try:
            out = subprocess.run(
                ["pgrep", "-f", "google chrome.*remote-debugging"],
                capture_output=True, timeout=5
            )
            return out.returncode == 0
        except Exception:
            return False

    async def _check_cdp_connectivity(self) -> bool:
        """Check if CDP endpoint is responsive."""
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=3) as session:
                url = f"http://{self.cdp_host}:{self.cdp_port}/json/version"
                async with session.get(url) as response:
                    return response.status == 200
        except Exception:
            return False

    def get_status(self) -> HealthStatus:
        """Get current health status."""
        return self._status

    def get_metrics(self) -> Dict[str, Any]:
        """Get health metrics."""
        return {
            "status": self._status.value,
            "last_check": time.time(),
            "chrome_binary": self.chrome_binary,
            "cdp_endpoint": f"{self.cdp_host}:{self.cdp_port}"
        }