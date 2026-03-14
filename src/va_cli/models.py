from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SessionState:
    cookies: list[dict[str, Any]] = field(default_factory=list)
    last_login_at: str | None = None
    last_login_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionState":
        return cls(
            cookies=list(payload.get("cookies", [])),
            last_login_at=payload.get("last_login_at"),
            last_login_url=payload.get("last_login_url"),
        )


@dataclass(slots=True)
class ClassSummary:
    class_id: str
    title: str
    club: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    instructor: str | None = None
    available: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BookingSummary:
    booking_id: str
    class_id: str | None = None
    title: str | None = None
    club: str | None = None
    start_time: str | None = None
    cancellable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FilterOption:
    label: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CalendarDateOption:
    date: str
    weekday: str
    day_number: str
    selected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CalendarFilters:
    courses: list[FilterOption] = field(default_factory=list)
    trainers: list[FilterOption] = field(default_factory=list)
    clubs: list[FilterOption] = field(default_factory=list)
    targets: list[FilterOption] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "courses": [item.to_dict() for item in self.courses],
            "trainers": [item.to_dict() for item in self.trainers],
            "clubs": [item.to_dict() for item in self.clubs],
            "targets": [item.to_dict() for item in self.targets],
        }


@dataclass(slots=True)
class CalendarClass:
    index: int
    token: str
    booking_id: str
    booking_center: str
    title: str
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    duration: str | None = None
    trainer: str | None = None
    club: str | None = None
    room: str | None = None
    status: str | None = None
    queue_length: int | None = None
    available_places: int | None = None
    button_label: str | None = None
    button_href: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
