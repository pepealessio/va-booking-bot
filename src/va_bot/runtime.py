from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from va_cli.client import VAError, VirginActiveClient
from va_cli.config import Config
from va_cli.credentials import CredentialStore
from va_cli.models import CalendarClass, CalendarDateOption

from .config import BotConfig, BotRule
from .state import BotOccurrenceState, BotStateStore


NowFn = Callable[[], datetime]
SleepFn = Callable[[float], None]
LogFn = Callable[[str], None]


@dataclass(slots=True)
class PlannedOccurrence:
    rule_name: str
    class_start: datetime
    booking_opens: datetime
    preflight_at: datetime
    key: str


@dataclass(slots=True)
class ValidationResult:
    rule_name: str
    date: str | None
    count: int
    ok: bool
    token: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "date": self.date,
            "count": self.count,
            "ok": self.ok,
            "token": self.token,
            "message": self.message,
        }


class BotService:
    FAR_SLEEP_SECONDS = 60.0
    NEAR_SLEEP_SECONDS = 1.0
    NEAR_WINDOW_SECONDS = 60.0

    def __init__(
        self,
        client: VirginActiveClient,
        config: BotConfig,
        *,
        app_config: Config,
        credential_store: CredentialStore,
        state_store: BotStateStore,
        now_fn: NowFn | None = None,
        sleep_fn: SleepFn | None = None,
        log_fn: LogFn | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.app_config = app_config
        self.credential_store = credential_store
        self.state_store = state_store
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.sleep_fn = sleep_fn or time_module.sleep
        self.log_fn = log_fn
        self.zone = ZoneInfo(config.timezone)
        self.state = self.state_store.load()

    def plan(self, *, now: datetime | None = None) -> list[PlannedOccurrence]:
        reference = now or self.now_fn()
        items = []
        for rule in self.config.rules:
            if not rule.enabled:
                continue
            items.append(self._next_occurrence(rule, reference))
        items.sort(key=lambda item: item.booking_opens)
        return items

    def validate(self) -> list[ValidationResult]:
        use_auth = self.client.has_saved_session()
        results = []
        for rule in self.config.rules:
            if not rule.enabled:
                results.append(
                    ValidationResult(
                        rule_name=rule.name,
                        date=None,
                        count=0,
                        ok=True,
                        message="rule disabled",
                    )
                )
                continue
            results.append(self._validate_rule(rule, use_auth=use_auth))
        return results

    def run_forever(self) -> None:
        while True:
            action = self._next_action()
            now = self.now_fn()
            wait_seconds = max(0.0, (action["at"] - now).total_seconds())
            self._log(
                "next action=%s rule=%s at=%s wait_seconds=%.3f"
                % (
                    action["type"],
                    action["rule"].name,
                    action["at"].isoformat(),
                    wait_seconds,
                )
            )
            if wait_seconds > 0:
                self._wait_until(action["at"])
            if action["type"] == "preflight":
                self._preflight(action["rule"], action["occurrence"])
            else:
                self._book_occurrence(action["rule"], action["occurrence"])

    def _next_action(self) -> dict[str, Any]:
        now = self.now_fn()
        candidates: list[dict[str, Any]] = []
        for rule in self.config.rules:
            if not rule.enabled:
                continue
            occurrence = self._next_occurrence(rule, now)
            entry = self.state.get(occurrence.key)
            if entry and entry.status == "booked":
                occurrence = self._next_occurrence(rule, occurrence.class_start + timedelta(seconds=1))
                entry = self.state.get(occurrence.key)
            if entry and entry.status == "preflight_ok":
                candidates.append({"type": "book", "at": max(now, occurrence.booking_opens), "rule": rule, "occurrence": occurrence})
                continue
            if now >= occurrence.booking_opens:
                candidates.append({"type": "book", "at": now, "rule": rule, "occurrence": occurrence})
                continue
            if now >= occurrence.preflight_at:
                candidates.append({"type": "preflight", "at": now, "rule": rule, "occurrence": occurrence})
                continue
            candidates.append({"type": "preflight", "at": occurrence.preflight_at, "rule": rule, "occurrence": occurrence})
        if not candidates:
            raise VAError("No enabled booking rules found.")
        candidates.sort(key=lambda item: item["at"])
        return candidates[0]

    def _validate_rule(self, rule: BotRule, *, use_auth: bool) -> ValidationResult:
        date_option = self._resolve_visible_date(rule, use_auth=use_auth)
        if date_option is None:
            return ValidationResult(
                rule_name=rule.name,
                date=None,
                count=0,
                ok=False,
                message="No visible calendar date matches the configured weekday.",
            )
        classes = self._load_matching_classes(rule, date_option.date, use_auth=use_auth)
        if len(classes) != 1:
            message = f"Expected exactly one class, found {len(classes)}."
            return ValidationResult(rule_name=rule.name, date=date_option.date, count=len(classes), ok=False, message=message)
        return ValidationResult(
            rule_name=rule.name,
            date=date_option.date,
            count=1,
            ok=True,
            token=classes[0].token,
            message="ok",
        )

    def _preflight(self, rule: BotRule, occurrence: PlannedOccurrence) -> None:
        self._log(
            f"preflight start rule={rule.name} class_start={occurrence.class_start.isoformat()} "
            f"booking_opens={occurrence.booking_opens.isoformat()}"
        )
        self._ensure_authenticated()
        classes = self._load_matching_classes(rule, occurrence.class_start.date().isoformat(), use_auth=True)
        if len(classes) != 1:
            raise VAError(f"Rule `{rule.name}` expected exactly one class during preflight, found {len(classes)}.")
        item = classes[0]
        self.state[occurrence.key] = BotOccurrenceState(
            status="preflight_ok",
            token=item.token,
            class_start=occurrence.class_start.isoformat(),
            booking_opens=occurrence.booking_opens.isoformat(),
            last_attempt_at=self.now_fn().isoformat(),
            result={"title": item.title, "club": item.club, "start_time": item.start_time},
        )
        self.state_store.save(self.state)
        self._log(
            f"preflight ok rule={rule.name} token={item.token} title={item.title!r} "
            f"start_time={item.start_time} club={item.club!r}"
        )

    def _book_occurrence(self, rule: BotRule, occurrence: PlannedOccurrence) -> None:
        self._log(
            f"book start rule={rule.name} class_start={occurrence.class_start.isoformat()} "
            f"booking_opens={occurrence.booking_opens.isoformat()}"
        )
        self._ensure_authenticated()
        entry = self.state.get(occurrence.key)
        if not entry or not entry.token:
            self._log(f"book missing token for rule={rule.name}; running preflight now")
            self._preflight(rule, occurrence)
            entry = self.state.get(occurrence.key)
        if not entry or not entry.token:
            raise VAError(f"Rule `{rule.name}` could not resolve a booking token.")
        deadline = self.now_fn() + timedelta(seconds=self.config.retry_window_seconds)
        last_error: str | None = None
        attempt = 0
        while self.now_fn() <= deadline:
            attempt += 1
            try:
                self._log(f"book attempt={attempt} rule={rule.name} token={entry.token}")
                payload = self.client.book(entry.token)
                if self._looks_like_success(payload):
                    self.state[occurrence.key] = BotOccurrenceState(
                        status="booked",
                        token=entry.token,
                        class_start=occurrence.class_start.isoformat(),
                        booking_opens=occurrence.booking_opens.isoformat(),
                        last_attempt_at=self.now_fn().isoformat(),
                        result=payload,
                    )
                    self.state_store.save(self.state)
                    self._log(f"book success rule={rule.name} token={entry.token} payload={payload!r}")
                    return
                if not self._should_retry_payload(payload):
                    self._log(f"book non-retry failure rule={rule.name} payload={payload!r}")
                    break
                last_error = self._payload_text(payload)
                self._log(f"book retryable response rule={rule.name} payload={payload!r}")
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self._log(f"book exception rule={rule.name} attempt={attempt} error={exc}")
            self.sleep_fn(self.config.retry_interval_seconds)
        self.state[occurrence.key] = BotOccurrenceState(
            status="failed",
            token=entry.token,
            class_start=occurrence.class_start.isoformat(),
            booking_opens=occurrence.booking_opens.isoformat(),
            last_attempt_at=self.now_fn().isoformat(),
            result={"error": last_error or "booking failed"},
        )
        self.state_store.save(self.state)
        self._log(f"book failed rule={rule.name} token={entry.token} error={last_error or 'unknown error'}")
        raise VAError(f"Booking failed for rule `{rule.name}`: {last_error or 'unknown error'}")

    def _resolve_visible_date(self, rule: BotRule, *, use_auth: bool) -> CalendarDateOption | None:
        filters = self._rule_filters(rule)
        dates = self.client.get_calendar_dates(filters)
        for item in dates:
            if datetime.strptime(item.date, "%Y-%m-%d").weekday() == rule.weekday_index:
                return item
        return None

    def _load_matching_classes(self, rule: BotRule, date: str, *, use_auth: bool) -> list[CalendarClass]:
        classes = self.client.list_classes(
            {**self._rule_filters(rule), "date": date},
            use_auth=use_auth,
            approve=(lambda _purpose: True) if use_auth else None,
        )
        return [item for item in classes if item.start_time == rule.time]

    def _rule_filters(self, rule: BotRule) -> dict[str, str | None]:
        return {
            "club": rule.club,
            "course": rule.course,
            "trainer": rule.trainer,
            "target": rule.target,
            "date": None,
        }

    def _next_occurrence(self, rule: BotRule, reference: datetime) -> PlannedOccurrence:
        local_reference = reference.astimezone(self.zone)
        hour, minute = (int(part) for part in rule.time.split(":"))
        days_ahead = (rule.weekday_index - local_reference.weekday()) % 7
        candidate_date = local_reference.date() + timedelta(days=days_ahead)
        candidate = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            hour,
            minute,
            tzinfo=self.zone,
        )
        if candidate <= local_reference:
            candidate += timedelta(days=7)
        booking_opens = candidate - timedelta(hours=48)
        preflight_at = booking_opens - timedelta(minutes=self.config.preflight_minutes)
        key = f"{rule.name}:{candidate.isoformat()}"
        return PlannedOccurrence(
            rule_name=rule.name,
            class_start=candidate,
            booking_opens=booking_opens,
            preflight_at=preflight_at,
            key=key,
        )

    def _ensure_authenticated(self) -> None:
        if self.client.has_saved_session():
            try:
                self._log("auth checking saved session")
                self.client._ensure_site_session()
                self._log("auth saved session is usable")
                return
            except Exception as exc:  # noqa: BLE001
                self._log(f"auth saved session refresh failed: {exc}")
        creds = self.credential_store.load()
        self._log(f"auth attempting login with {'saved credentials' if creds else 'configured credentials'}")
        if creds and (self.app_config.username != creds.username or self.app_config.password != creds.password):
            self.client.config = self.client.config.with_credentials(creds.username, creds.password)
        self.client.login()
        self._log("auth login completed")

    def _looks_like_success(self, payload: Any) -> bool:
        text = self._payload_text(payload)
        lowered = text.casefold()
        return any(token in lowered for token in ("ok", "success", "prenot", "booked", "confermat"))

    def _should_retry_payload(self, payload: Any) -> bool:
        text = self._payload_text(payload).casefold()
        retry_markers = ("troppo presto", "too early", "attendere", "riprov", "tempor", "timeout")
        return any(marker in text for marker in retry_markers)

    def _payload_text(self, payload: Any) -> str:
        if isinstance(payload, dict):
            return " ".join(self._payload_text(value) for value in payload.values())
        if isinstance(payload, list):
            return " ".join(self._payload_text(value) for value in payload)
        return str(payload or "")

    def _wait_until(self, target: datetime) -> None:
        chunk_count = 0
        while True:
            now = self.now_fn()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                if chunk_count:
                    self._log(f"wait complete target={target.isoformat()} chunks={chunk_count}")
                return
            sleep_seconds = self._next_sleep_chunk(remaining)
            chunk_count += 1
            self._log(
                "wait chunk=%d target=%s remaining=%.3f sleep_seconds=%.3f"
                % (chunk_count, target.isoformat(), remaining, sleep_seconds)
            )
            self.sleep_fn(sleep_seconds)

    def _next_sleep_chunk(self, remaining: float) -> float:
        if remaining <= self.NEAR_WINDOW_SECONDS:
            return min(self.NEAR_SLEEP_SECONDS, remaining)
        return min(self.FAR_SLEEP_SECONDS, remaining)

    def _log(self, message: str) -> None:
        if self.log_fn is not None:
            self.log_fn(f"[va-bot debug] {message}")
