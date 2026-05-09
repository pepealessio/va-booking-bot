from __future__ import annotations

from pathlib import Path

_FIXTURES_DIR = Path(__file__).parent


def load(name: str) -> str:
    """Load a fixture file from the fixtures directory.

    Args:
        name: Filename relative to the fixtures directory.

    Returns:
        The full text contents of the fixture file.
    """
    path = _FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8")


def path(name: str) -> Path:
    """Return the absolute path to a fixture file."""
    return _FIXTURES_DIR / name
