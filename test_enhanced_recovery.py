#!/usr/bin/env python3
"""
Test for Chrome Agent V2 enhanced recovery features.
This test should fail initially, then pass after implementing the enhanced features.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

# Must set TG_SAVE_FOLDER before importing chrome_agent
_TMP = tempfile.mkdtemp(prefix="tg_userbot_chrome_test_")
os.environ["TG_SAVE_FOLDER"] = _TMP

# Import after setting environment
from tg_userbot import config
from tg_userbot import chrome_agent


def test_enhanced_task_recovery():
    """Test enhanced task recovery with GUID tracking and health checks."""

    # Test 1: Test _is_chrome_completed function
    task = {
        "task_id": "test-123",
        "status": "RUNNING",
        "filename": "completed.mp4"
    }

    # Create completed file
    download_dir = "/tmp/test_download"
    os.makedirs(download_dir, exist_ok=True)
    completed_file = os.path.join(download_dir, "completed.mp4")
    with open(completed_file, "wb") as f:
        f.write(b"test content")

    # Should detect completed file
    assert chrome_agent._is_chrome_completed(task, download_dir) == True

    # Create crdownload file (should not be considered completed)
    crdownload_file = completed_file + ".crdownload"
    with open(crdownload_file, "wb") as f:
        f.write(b"incomplete")

    # Should NOT detect as completed when .crdownload exists
    assert chrome_agent._is_chrome_completed(task, download_dir) == False

    # Clean up
    os.remove(completed_file)
    os.remove(crdownload_file)
    os.rmdir(download_dir)

    # Test 2: Test _is_task_stale function
    # Create a task that started more than 30 minutes ago
    old_time = datetime.now() - timedelta(minutes=35)
    task_stale = {
        "task_id": "stale-task",
        "status": "RUNNING",
        "started_at": old_time.strftime("%Y-%m-%d %H:%M:%S")
    }

    # Should detect stale task
    assert chrome_agent._is_task_stale(task_stale) == True

    # Test non-stale task
    task_fresh = {
        "task_id": "fresh-task",
        "status": "RUNNING",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # Just started
    }

    assert chrome_agent._is_task_stale(task_fresh) == False

    # Test 3: Test _is_guid_valid function
    task_with_guid = {
        "task_id": "guid-task",
        "guid": "valid-guid-123"
    }

    assert chrome_agent._is_guid_valid(task_with_guid) == True

    task_without_guid = {
        "task_id": "no-guid-task"
    }

    assert chrome_agent._is_guid_valid(task_without_guid) == False

    print("All enhanced recovery tests passed!")


def test_enhanced_recover_tasks():
    """Test the enhanced recover_tasks function."""

    download_dir = "/tmp/test_recovery"
    os.makedirs(download_dir, exist_ok=True)

    # Test 1: Completed download should be marked SUCCESS
    completed_task = {
        "task_id": "completed-123",
        "status": "RUNNING",
        "filename": "video.mp4"
    }

    # Create completed file
    completed_file = os.path.join(download_dir, "video.mp4")
    with open(completed_file, "wb") as f:
        f.write(b"completed content")

    # Call enhanced recovery
    chrome_agent.enhanced_recover_tasks([completed_task], download_dir)

    # Should be marked SUCCESS
    assert completed_task["status"] == "SUCCESS"
    assert completed_task["size_bytes"] == len(b"completed content")

    # Clean up
    os.remove(completed_file)

    # Test 2: Task without filename should not be affected
    no_file_task = {
        "task_id": "no-file-123",
        "status": "RUNNING"
        # No filename
    }

    chrome_agent.enhanced_recover_tasks([no_file_task], download_dir)
    assert no_file_task["status"] == "RUNNING"  # Should remain RUNNING

    # Test 3: Non-RUNNING tasks should not be affected
    pending_task = {
        "task_id": "pending-123",
        "status": "PENDING"
    }

    chrome_agent.enhanced_recover_tasks([pending_task], download_dir)
    assert pending_task["status"] == "PENDING"  # Should remain PENDING

    # Clean up
    os.rmdir(download_dir)

    print("Enhanced recover_tasks tests passed!")


if __name__ == "__main__":
    # This should fail initially because the functions don't exist
    try:
        test_enhanced_task_recovery()
        test_enhanced_recover_tasks()
        print("All tests passed!")
    except AttributeError as e:
        print(f"Expected failure: {e}")
        print("Functions need to be implemented in chrome_agent.py")