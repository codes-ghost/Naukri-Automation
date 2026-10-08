"""Resolve persistent workflow data to the active user's data directory."""
import os
from pathlib import Path


def get_data_dir() -> Path:
    configured_dir = os.environ.get("NAUKRI_DATA_DIR")
    return Path(configured_dir).expanduser().resolve() if configured_dir else Path.cwd().resolve()


def data_path(filename: str | Path) -> Path:
    path = Path(filename)
    return path if path.is_absolute() else get_data_dir() / path
