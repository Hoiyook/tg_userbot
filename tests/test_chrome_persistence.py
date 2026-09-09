import os
import json
import tempfile
import unittest
from unittest.mock import patch, mock_open
from pathlib import Path
import glob

from tg_userbot.chrome_persistence import (
    atomic_save_with_backup,
    load_tasks_with_backup,
    cleanup_old_backups
)


class TestChromePersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_data = [{"id": "task1", "status": "PENDING"}, {"id": "task2", "status": "RUNNING"}]
        self.test_file = os.path.join(self.temp_dir, "chrome_tasks.json")
        self.max_backups = 3

    def tearDown(self):
        # Clean up all files in temp directory
        for file in Path(self.temp_dir).glob("*"):
            file.unlink()
        os.rmdir(self.temp_dir)

    def test_atomic_save_with_backup_creates_backup(self):
        """Test that atomic save creates backup file when primary file exists"""
        # First create the primary file
        with open(self.test_file, 'w') as f:
            json.dump(self.test_data, f)

        # Verify primary file exists
        self.assertTrue(os.path.exists(self.test_file))

        # Perform atomic save with backup
        atomic_save_with_backup(self.test_data, self.test_file, self.max_backups)

        # Verify primary file exists and is updated
        self.assertTrue(os.path.exists(self.test_file))
        with open(self.test_file, 'r') as f:
            data = json.load(f)
        self.assertEqual(data, self.test_data)

        # Verify backup file was created
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 1)
        self.assertTrue(".bak." in backup_files[0])

    def test_atomic_save_without_backup_when_primary_missing(self):
        """Test that atomic save doesn't create backup when primary file doesn't exist"""
        # Ensure primary file doesn't exist
        self.assertFalse(os.path.exists(self.test_file))

        # Perform atomic save with backup
        atomic_save_with_backup(self.test_data, self.test_file, self.max_backups)

        # Verify primary file exists and is updated
        self.assertTrue(os.path.exists(self.test_file))
        with open(self.test_file, 'r') as f:
            data = json.load(f)
        self.assertEqual(data, self.test_data)

        # Verify no backup files created
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 0)

    def test_load_tasks_from_primary_file(self):
        """Test that load_tasks loads from primary file when it's valid"""
        # Create primary file
        with open(self.test_file, 'w') as f:
            json.dump(self.test_data, f)

        # Load tasks
        loaded_data = load_tasks_with_backup(self.test_file)

        # Verify data matches
        self.assertEqual(loaded_data, self.test_data)

    def test_load_tasks_from_backup_when_primary_corrupted(self):
        """Test that load_tasks falls back to backup when primary is corrupted"""
        # Create primary file with invalid JSON
        with open(self.test_file, 'w') as f:
            f.write("invalid json content")

        # Create backup file with valid data
        backup_file = f"{self.test_file}.bak.20240101_120000"
        with open(backup_file, 'w') as f:
            json.dump(self.test_data, f)

        # Load tasks
        loaded_data = load_tasks_with_backup(self.test_file)

        # Verify data matches backup
        self.assertEqual(loaded_data, self.test_data)

    def test_load_tasks_raises_error_when_both_corrupted(self):
        """Test that load_tasks raises ValueError when both primary and backup are corrupted"""
        # Create primary file with invalid JSON
        with open(self.test_file, 'w') as f:
            f.write("invalid json content")

        # Create backup file with invalid JSON
        backup_file = f"{self.test_file}.bak.20240101_120000"
        with open(backup_file, 'w') as f:
            f.write("also invalid json")

        # Should raise ValueError
        with self.assertRaises(ValueError) as cm:
            load_tasks_with_backup(self.test_file)

        self.assertIn("both primary and backup files are corrupted", str(cm.exception))

    def test_cleanup_old_backups_maintains_count(self):
        """Test that cleanup_old_backups maintains only the specified number of backups"""
        # Create multiple backup files with different timestamps
        timestamps = ["20240101_100000", "20240101_110000", "20240101_120000", "20240101_130000"]

        for timestamp in timestamps:
            backup_file = f"{self.test_file}.bak.{timestamp}"
            with open(backup_file, 'w') as f:
                json.dump([{"id": f"task_{timestamp}"}], f)

        # Initially we have 4 backup files
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 4)

        # Cleanup to keep only 3 backups
        cleanup_old_backups(self.test_file, self.max_backups)

        # Verify only 3 backups remain
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 3)

        # Verify the oldest backup was removed
        remaining_timestamps = [f.split(".bak.")[-1] for f in backup_files]
        self.assertNotIn("20240101_100000", remaining_timestamps)
        self.assertIn("20240101_110000", remaining_timestamps)
        self.assertIn("20240101_120000", remaining_timestamps)
        self.assertIn("20240101_130000", remaining_timestamps)

    def test_cleanup_old_backups_when_count_exceeded(self):
        """Test that cleanup_old_backups removes oldest backups when count is exceeded"""
        # Create 5 backup files
        for i in range(5):
            timestamp = f"20240101_{100000 + i * 10000}"
            backup_file = f"{self.test_file}.bak.{timestamp}"
            with open(backup_file, 'w') as f:
                json.dump([{"id": f"task_{i}"}], f)

        # Verify all 5 backups exist
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 5)

        # Cleanup to keep only 3
        cleanup_old_backups(self.test_file, 3)

        # Verify only 3 remain
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 3)

    @patch('os.replace')
    def test_atomic_save_uses_temp_file_pattern(self, mock_replace):
        """Test that atomic_save uses temp file + os.replace pattern"""
        # Create primary file first
        with open(self.test_file, 'w') as f:
            json.dump([{"id": "old_task"}], f)

        # Perform atomic save
        new_data = [{"id": "new_task"}]
        atomic_save_with_backup(new_data, self.test_file, self.max_backups)

        # Verify os.replace was called with temp file path
        mock_replace.assert_called_once()
        call_args = mock_replace.call_args[0]
        self.assertTrue(call_args[0].endswith('.tmp'))
        self.assertEqual(call_args[1], self.test_file)

    def test_atomic_save_creates_backup_when_primary_exists(self):
        """Test that atomic_save creates backup before replacing primary"""
        # Create primary file
        with open(self.test_file, 'w') as f:
            json.dump([{"id": "original"}], f)

        # Perform atomic save
        new_data = [{"id": "updated"}]
        atomic_save_with_backup(new_data, self.test_file, self.max_backups)

        # Verify primary file is updated
        with open(self.test_file, 'r') as f:
            data = json.load(f)
        self.assertEqual(data, new_data)

        # Verify backup was created
        backup_files = glob.glob(f"{self.test_file}.bak.*")
        self.assertEqual(len(backup_files), 1)

        # Verify backup contains original data
        with open(backup_files[0], 'r') as f:
            backup_data = json.load(f)
        self.assertEqual(backup_data, [{"id": "original"}])


if __name__ == '__main__':
    unittest.main()