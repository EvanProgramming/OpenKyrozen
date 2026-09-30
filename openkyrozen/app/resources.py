"""Locate shipped resources without depending on feature-module locations."""
from importlib import resources
from pathlib import Path

def repository_root():
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None

def plugin_directory():
    return Path(str(resources.files("plugins")))
