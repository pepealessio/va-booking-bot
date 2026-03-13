from __future__ import annotations

import json
from pathlib import Path

from .models import SessionState


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SessionState:
        if not self.path.exists():
            return SessionState()
        with self.path.open("r", encoding="utf-8") as handle:
            return SessionState.from_dict(json.load(handle))

    def save(self, state: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
