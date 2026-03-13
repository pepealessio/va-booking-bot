from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class BotOccurrenceState:
    status: str
    token: str | None = None
    class_start: str | None = None
    booking_opens: str | None = None
    last_attempt_at: str | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "token": self.token,
            "class_start": self.class_start,
            "booking_opens": self.booking_opens,
            "last_attempt_at": self.last_attempt_at,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BotOccurrenceState":
        return cls(
            status=str(payload.get("status", "")),
            token=payload.get("token"),
            class_start=payload.get("class_start"),
            booking_opens=payload.get("booking_opens"),
            last_attempt_at=payload.get("last_attempt_at"),
            result=dict(payload.get("result", {})),
        )


class BotStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, BotOccurrenceState]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return {}
        items = payload.get("occurrences", {})
        if not isinstance(items, dict):
            return {}
        return {
            key: BotOccurrenceState.from_dict(value)
            for key, value in items.items()
            if isinstance(value, dict)
        }

    def save(self, occurrences: dict[str, BotOccurrenceState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "occurrences": {key: value.to_dict() for key, value in occurrences.items()},
        }
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
