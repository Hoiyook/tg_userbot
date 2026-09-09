import os
import json
import glob
import tempfile
from pathlib import Path
from datetime import datetime


def atomic_save_with_backup(data, file_path, max_backups=3):
    """
    Save data atomically with backup functionality.

    Args:
        data: The data to save (must be JSON serializable)
        file_path: Path to the primary file
        max_backups: Maximum number of backup files to keep
    """
    # Create backup if primary file already exists
    if os.path.exists(file_path):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{file_path}.bak.{timestamp}"

        try:
            with open(file_path, 'r') as f:
                backup_data = json.load(f)
            with open(backup_path, 'w') as f:
                json.dump(backup_data, f, indent=2)
        except (json.JSONDecodeError, IOError):
            # If backup fails, continue with atomic save
            pass

    # Perform atomic save using temp file
    temp_path = f"{file_path}.tmp"
    try:
        with open(temp_path, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, file_path)
    except Exception:
        # Clean up temp file if save fails
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    # Clean up old backups
    cleanup_old_backups(file_path, max_backups)


def load_tasks_with_backup(file_path):
    """
    Load tasks from primary file, fallback to backup if corrupted.

    Args:
        file_path: Path to the primary file

    Returns:
        The loaded data

    Raises:
        ValueError: If both primary and backup files are corrupted
    """
    # Try to load from primary file
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            # Primary file is corrupted, try backup
            pass

    # Try to load from backup files (newest first)
    backup_files = glob.glob(f"{file_path}.bak.*")
    # Sort by modification time (newest first)
    backup_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

    for backup_file in backup_files:
        try:
            with open(backup_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            # This backup is corrupted, try next one
            continue

    # No valid file found
    raise ValueError("both primary and backup files are corrupted")


def cleanup_old_backups(file_path, max_backups=3):
    """
    Clean up old backup files, keeping only the most recent ones.

    Args:
        file_path: Path to the primary file
        max_backups: Maximum number of backup files to keep
    """
    backup_files = glob.glob(f"{file_path}.bak.*")

    if len(backup_files) > max_backups:
        # Sort by modification time (oldest first)
        backup_files.sort(key=lambda x: os.path.getmtime(x))

        # Remove oldest backups
        for backup_file in backup_files[:-max_backups]:
            try:
                os.unlink(backup_file)
            except OSError:
                pass  # Ignore if file doesn't exist or can't be deleted