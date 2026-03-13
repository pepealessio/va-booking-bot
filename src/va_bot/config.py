from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


class BotConfigError(RuntimeError):
    """Raised when the bot configuration is invalid."""


WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(slots=True)
class BotRule:
    name: str
    club: str
    course: str
    weekday: str
    time: str
    enabled: bool = True
    trainer: str | None = None
    target: str | None = None

    @property
    def weekday_index(self) -> int:
        return WEEKDAY_NAMES[self.weekday]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class BotConfig:
    timezone: str = "Europe/Rome"
    preflight_minutes: int = 2
    retry_window_seconds: int = 15
    retry_interval_seconds: float = 1.0
    rules: list[BotRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone,
            "preflight_minutes": self.preflight_minutes,
            "retry_window_seconds": self.retry_window_seconds,
            "retry_interval_seconds": self.retry_interval_seconds,
            "rules": [rule.to_dict() for rule in self.rules],
        }


def load_config(path: Path) -> BotConfig:
    if not path.exists():
        raise BotConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise BotConfigError("Config file must contain a top-level mapping.")
    return parse_config(payload)


def parse_config(payload: dict[str, Any]) -> BotConfig:
    timezone = _as_string(payload.get("timezone"), "timezone", default="Europe/Rome")
    preflight_minutes = _as_int(payload.get("preflight_minutes", 2), "preflight_minutes", minimum=0)
    retry_window_seconds = _as_int(payload.get("retry_window_seconds", 15), "retry_window_seconds", minimum=1)
    retry_interval_seconds = _as_float(payload.get("retry_interval_seconds", 1), "retry_interval_seconds", minimum=0.1)
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise BotConfigError("Config must define a non-empty `rules` list.")
    rules: list[BotRule] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            raise BotConfigError(f"Rule #{index} must be a mapping.")
        rule = BotRule(
            name=_as_string(item.get("name"), f"rules[{index}].name"),
            enabled=_as_bool(item.get("enabled", True), f"rules[{index}].enabled"),
            club=_as_string(item.get("club"), f"rules[{index}].club"),
            course=_as_string(item.get("course"), f"rules[{index}].course"),
            weekday=_parse_weekday(item.get("weekday"), f"rules[{index}].weekday"),
            time=_parse_time(item.get("time"), f"rules[{index}].time"),
            trainer=_as_optional_string(item.get("trainer"), f"rules[{index}].trainer"),
            target=_as_optional_string(item.get("target"), f"rules[{index}].target"),
        )
        if rule.name in seen_names:
            raise BotConfigError(f"Duplicate rule name: {rule.name}")
        seen_names.add(rule.name)
        rules.append(rule)
    return BotConfig(
        timezone=timezone,
        preflight_minutes=preflight_minutes,
        retry_window_seconds=retry_window_seconds,
        retry_interval_seconds=retry_interval_seconds,
        rules=rules,
    )


def dump_config(config: BotConfig) -> str:
    return yaml.safe_dump(config.to_dict(), sort_keys=False, allow_unicode=False)


def write_config(path: Path, config: BotConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(dump_config(config))


def _as_string(value: Any, label: str, *, default: str | None = None) -> str:
    if value is None:
        if default is not None:
            return default
        raise BotConfigError(f"Missing required field `{label}`.")
    if not isinstance(value, str):
        raise BotConfigError(f"`{label}` must be a string.")
    normalized = value.strip()
    if not normalized:
        raise BotConfigError(f"`{label}` must not be empty.")
    return normalized


def _as_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BotConfigError(f"`{label}` must be a string or null.")
    normalized = value.strip()
    return normalized or None


def _as_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BotConfigError(f"`{label}` must be an integer.")
    if value < minimum:
        raise BotConfigError(f"`{label}` must be >= {minimum}.")
    return value


def _as_float(value: Any, label: str, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BotConfigError(f"`{label}` must be a number.")
    numeric = float(value)
    if numeric < minimum:
        raise BotConfigError(f"`{label}` must be >= {minimum}.")
    return numeric


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BotConfigError(f"`{label}` must be a boolean.")
    return value


def _parse_weekday(value: Any, label: str) -> str:
    weekday = _as_string(value, label).casefold()
    if weekday not in WEEKDAY_NAMES:
        allowed = ", ".join(WEEKDAY_NAMES)
        raise BotConfigError(f"`{label}` must be one of: {allowed}.")
    return weekday


def _parse_time(value: Any, label: str) -> str:
    raw = _as_string(value, label)
    parts = raw.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise BotConfigError(f"`{label}` must be in HH:MM format.")
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise BotConfigError(f"`{label}` must be in HH:MM format.")
    return f"{hour:02d}:{minute:02d}"
