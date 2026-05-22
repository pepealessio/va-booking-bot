from __future__ import annotations

import json
import random
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def generate_id() -> str:
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "bk_" + "".join(random.choices(chars, k=8))


@dataclass
class BookingRecord:
    id: str
    token: str
    class_desc: str
    chat_id: int
    message_id: int | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BookingRecord":
        return cls(
            id=payload["id"],
            token=payload["token"],
            class_desc=payload["class_desc"],
            chat_id=payload["chat_id"],
            message_id=payload.get("message_id"),
            created_at=payload.get("created_at", ""),
        )


class BookingStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _save_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)

    def save(self, record: BookingRecord) -> None:
        all_records = self._load_all()
        all_records[record.id] = record.to_dict()
        self._save_all(all_records)

    def load(self, booking_id: str) -> BookingRecord | None:
        all_records = self._load_all()
        payload = all_records.get(booking_id)
        if payload is None:
            return None
        return BookingRecord.from_dict(payload)

    def delete(self, booking_id: str) -> None:
        all_records = self._load_all()
        all_records.pop(booking_id, None)
        self._save_all(all_records)
