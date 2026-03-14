from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from va_bot.config import BotConfig, BotRule
from va_bot.runtime import BotService
from va_bot.state import BotOccurrenceState, BotStateStore
from va_cli.client import VAError
from va_cli.config import Config
from va_cli.credentials import SavedCredentials
from va_cli.models import CalendarClass, CalendarDateOption


class FakeCredentialStore:
    def __init__(self, username: str = "user@example.com", password: str = "secret") -> None:
        self.saved = SavedCredentials(username=username, password=password)

    def load(self):
        return self.saved


class FakeClient:
    def __init__(self) -> None:
        self.saved_session = True
        self.login_calls = 0
        self.ensure_calls = 0
        self.book_calls: list[str] = []
        self.book_responses: list[object] = [{"status": "booked"}]
        self.config = Config(
            username="user@example.com",
            password="secret",
            login_page_url="https://example.com/login",
            login_submit_url="https://example.com/login",
            login_status_url="https://example.com/status",
            calendar_page_url="https://example.com/calendar",
            calendar_filter_url="https://example.com/filter",
            integration_base_url="https://example.com/integration",
            state_dir=Path("."),
            timeout_seconds=20,
        )

    def has_saved_session(self) -> bool:
        return self.saved_session

    def _ensure_site_session(self) -> None:
        self.ensure_calls += 1

    def login(self) -> None:
        self.login_calls += 1
        self.saved_session = True

    def get_calendar_dates(self, _filters):
        return [
            CalendarDateOption(date="2026-03-18", weekday="MERCOLEDI", day_number="18", selected=True),
            CalendarDateOption(date="2026-03-19", weekday="GIOVEDI", day_number="19", selected=False),
        ]

    def list_classes(self, filters, **_kwargs):
        if filters["date"] == "2026-03-19":
            return [
                CalendarClass(
                    index=1,
                    token="355132c220",
                    booking_id="355132",
                    booking_center="220",
                    title="Reformer Pilates Align",
                    date="2026-03-19",
                    start_time="18:15",
                    end_time="19:00",
                    club="Roma EUR",
                    trainer="Alice",
                    status="bookable",
                )
            ]
        if filters["date"] == "2026-03-18":
            return [
                CalendarClass(
                    index=1,
                    token="355132c220",
                    booking_id="355132",
                    booking_center="220",
                    title="Reformer Pilates Align",
                    date="2026-03-18",
                    start_time="18:00",
                    end_time="19:00",
                    club="Roma EUR",
                    trainer="Alice",
                    status="bookable",
                )
            ]
        return []

    def book(self, token):
        self.book_calls.append(token)
        response = self.book_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BotRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.app_config = Config(
            username="user@example.com",
            password="secret",
            login_page_url="https://example.com/login",
            login_submit_url="https://example.com/login",
            login_status_url="https://example.com/status",
            calendar_page_url="https://example.com/calendar",
            calendar_filter_url="https://example.com/filter",
            integration_base_url="https://example.com/integration",
            state_dir=Path(self.temp_dir.name),
            timeout_seconds=20,
        )
        self.rule = BotRule(
            name="eur-wed",
            club="Roma EUR",
            course="Reformer Pilates Align",
            weekday="wednesday",
            time="18:00",
        )

    def _make_service(self, client: FakeClient, now_box: dict[str, datetime]) -> BotService:
        return self._make_service_with_log(client, now_box, None)

    def _make_service_with_log(
        self,
        client: FakeClient,
        now_box: dict[str, datetime],
        log_messages: list[str] | None,
    ) -> BotService:
        def now_fn() -> datetime:
            return now_box["value"]

        def sleep_fn(seconds: float) -> None:
            now_box["value"] += timedelta(seconds=seconds)

        return BotService(
            client,
            BotConfig(timezone="UTC", rules=[self.rule]),
            app_config=self.app_config,
            credential_store=FakeCredentialStore(),
            state_store=BotStateStore(Path(self.temp_dir.name) / "bot-state.json"),
            now_fn=now_fn,
            sleep_fn=sleep_fn,
            log_fn=log_messages.append if log_messages is not None else None,
        )

    def test_plan_returns_exact_booking_window(self) -> None:
        service = self._make_service(FakeClient(), {"value": datetime(2026, 3, 16, 12, 0, tzinfo=UTC)})
        plan = service.plan()
        self.assertEqual(plan[0].class_start.isoformat(), "2026-03-18T18:00:00+00:00")
        self.assertEqual(plan[0].booking_opens.isoformat(), "2026-03-16T18:00:00+00:00")
        self.assertEqual(plan[0].preflight_at.isoformat(), "2026-03-16T17:58:00+00:00")

    def test_validate_requires_exactly_one_match(self) -> None:
        client = FakeClient()
        service = self._make_service(client, {"value": datetime(2026, 3, 16, 12, 0, tzinfo=UTC)})
        result = service.validate()[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.token, "355132c220")

    def test_preflight_and_book_occurrence_persist_state(self) -> None:
        client = FakeClient()
        now_box = {"value": datetime(2026, 3, 16, 17, 57, tzinfo=UTC)}
        service = self._make_service(client, now_box)
        action = service._next_action()
        self.assertEqual(action["type"], "preflight")
        service.sleep_fn((action["at"] - now_box["value"]).total_seconds())
        service._preflight(action["rule"], action["occurrence"])
        action = service._next_action()
        self.assertEqual(action["type"], "book")
        service.sleep_fn((action["at"] - now_box["value"]).total_seconds())
        service._book_occurrence(action["rule"], action["occurrence"])
        self.assertEqual(client.book_calls, ["355132c220"])
        saved = service.state_store.load()
        self.assertEqual(saved[action["occurrence"].key].status, "booked")

    def test_book_retries_on_too_early_payload(self) -> None:
        client = FakeClient()
        client.book_responses = [{"message": "too early"}, {"status": "booked"}]
        now_box = {"value": datetime(2026, 3, 16, 18, 0, tzinfo=UTC)}
        service = self._make_service(client, now_box)
        occurrence = service.plan()[0]
        service.state[occurrence.key] = BotOccurrenceState(
            status="preflight_ok",
            token="355132c220",
            class_start=occurrence.class_start.isoformat(),
            booking_opens=occurrence.booking_opens.isoformat(),
        )
        service._book_occurrence(self.rule, occurrence)
        self.assertEqual(client.book_calls, ["355132c220", "355132c220"])

    def test_book_does_not_retry_on_queue_full_payload(self) -> None:
        client = FakeClient()
        client.book_responses = [{"message": "Prenota in lista di attesa piena"}]
        now_box = {"value": datetime(2026, 3, 16, 18, 0, tzinfo=UTC)}
        service = self._make_service(client, now_box)
        occurrence = service.plan()[0]
        service.state[occurrence.key] = BotOccurrenceState(
            status="preflight_ok",
            token="355132c220",
            class_start=occurrence.class_start.isoformat(),
            booking_opens=occurrence.booking_opens.isoformat(),
        )
        with self.assertRaises(VAError):
            service._book_occurrence(self.rule, occurrence)
        self.assertEqual(client.book_calls, ["355132c220"])

    def test_ensure_authenticated_logs_in_when_session_missing(self) -> None:
        client = FakeClient()
        client.saved_session = False
        service = self._make_service(client, {"value": datetime(2026, 3, 16, 12, 0, tzinfo=UTC)})
        service._ensure_authenticated()
        self.assertEqual(client.login_calls, 1)

    def test_book_failure_is_recorded(self) -> None:
        client = FakeClient()
        client.book_responses = [VAError("broken")]
        now_box = {"value": datetime(2026, 3, 16, 18, 0, tzinfo=UTC)}
        service = self._make_service(client, now_box)
        occurrence = service.plan()[0]

        service.state[occurrence.key] = BotOccurrenceState(
            status="preflight_ok",
            token="355132c220",
            class_start=occurrence.class_start.isoformat(),
            booking_opens=occurrence.booking_opens.isoformat(),
        )
        with self.assertRaises(VAError):
            service._book_occurrence(self.rule, occurrence)
        saved = service.state_store.load()
        self.assertEqual(saved[occurrence.key].status, "failed")

    def test_debug_log_emits_runner_events(self) -> None:
        client = FakeClient()
        now_box = {"value": datetime(2026, 3, 16, 17, 57, tzinfo=UTC)}
        logs: list[str] = []
        service = self._make_service_with_log(client, now_box, logs)
        action = service._next_action()
        self.assertEqual(action["type"], "preflight")
        service.sleep_fn((action["at"] - now_box["value"]).total_seconds())
        service._preflight(action["rule"], action["occurrence"])
        action = service._next_action()
        service.sleep_fn((action["at"] - now_box["value"]).total_seconds())
        service._book_occurrence(action["rule"], action["occurrence"])
        joined = "\n".join(logs)
        self.assertIn("[va-bot debug] preflight start rule=eur-wed", joined)
        self.assertIn("[va-bot debug] auth checking saved session", joined)
        self.assertIn("[va-bot debug] book attempt=1 rule=eur-wed token=355132c220", joined)
        self.assertIn("[va-bot debug] book success rule=eur-wed token=355132c220", joined)

    def test_wait_until_chunks_long_sleep(self) -> None:
        client = FakeClient()
        now_box = {"value": datetime(2026, 3, 16, 12, 0, tzinfo=UTC)}
        sleeps: list[float] = []

        def now_fn() -> datetime:
            return now_box["value"]

        def sleep_fn(seconds: float) -> None:
            sleeps.append(seconds)
            now_box["value"] += timedelta(seconds=seconds)

        service = BotService(
            client,
            BotConfig(timezone="UTC", rules=[self.rule]),
            app_config=self.app_config,
            credential_store=FakeCredentialStore(),
            state_store=BotStateStore(Path(self.temp_dir.name) / "bot-state.json"),
            now_fn=now_fn,
            sleep_fn=sleep_fn,
        )
        service._wait_until(now_box["value"] + timedelta(seconds=125))
        self.assertEqual(sleeps, [60.0, 60.0, 1.0, 1.0, 1.0, 1.0, 1.0])

    def test_wait_until_logs_near_deadline_chunks(self) -> None:
        client = FakeClient()
        now_box = {"value": datetime(2026, 3, 16, 12, 0, tzinfo=UTC)}
        logs: list[str] = []
        service = self._make_service_with_log(client, now_box, logs)
        service._wait_until(now_box["value"] + timedelta(seconds=3))
        joined = "\n".join(logs)
        self.assertIn("[va-bot debug] wait chunk=1", joined)
        self.assertIn("[va-bot debug] wait complete", joined)


if __name__ == "__main__":
    unittest.main()
