from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx

from va_cli import cli
from va_cli.automate import (
    VA_MARKER_PREFIX,
    _cronline_to_dict,
    _do_book_attempt,
    build_cron_entry,
    cmd_list,
    cmd_remove,
    compute_cron_times,
    interactive_add,
    worker_book_recurring,
)
from va_cli.client import VAError
from va_cli.config import Config
from va_cli.models import CalendarClass


def make_config(state_dir: Path) -> Config:
    return Config(
        username="test@example.com",
        password="secret",
        login_page_url="https://shop.virginactive.it/account/login",
        login_submit_url="https://shop.virginactive.it/account/login",
        login_status_url="https://www.virginactive.it/rest-api/login-status",
        calendar_page_url="https://www.virginactive.it/calendario-corsi",
        calendar_filter_url="https://www.virginactive.it/calendario-corsi/JFilter",
        integration_base_url="https://www.virginactive.it/VirginIntegrations/IntegrationPlatform",
        state_dir=state_dir,
        timeout_seconds=5,
        queue_full_threshold=999,
        booking_open_hours=48,
    )


# =====================================================================
# 1. Cron computation tests (5 tests)
# =====================================================================


class CronComputationTests(unittest.TestCase):

    def test_book_time_is_same_time_minus_48h(self) -> None:
        cron = compute_cron_times(0, "18:00")  # Monday
        self.assertEqual(cron["book_hour"], 18)
        self.assertEqual(cron["book_minute"], 0)

    def test_login_time_is_5min_before_book(self) -> None:
        cron = compute_cron_times(1, "17:15")  # Tuesday
        self.assertEqual(cron["login_minute"], 10)  # 15-5=10
        self.assertEqual(cron["book_minute"], 15)

    def test_all_dow_wrap_correctly(self) -> None:
        # Monday → book Saturday, login Friday
        cron = compute_cron_times(0, "18:00")
        self.assertEqual(cron["book_dow"], 6)   # Sat
        self.assertEqual(cron["login_dow"], 5)  # Fri

    def test_hour_wrap_past_midnight(self) -> None:
        cron = compute_cron_times(0, "02:00")
        self.assertEqual(cron["book_hour"], 2)
        self.assertEqual(cron["login_hour"], 1)
        self.assertEqual(cron["login_minute"], 55)

    def test_cron_entry_format_with_markers(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", course="Yoga")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("#"))
        self.assertIn("va login", lines[1])
        self.assertIn("va book --recurring", lines[2])
        self.assertIn("--club 'Roma EUR'", lines[2])
        self.assertIn("--course 'Yoga'", lines[2])


# =====================================================================
# 2. Cron entry generation (3 tests)
# =====================================================================


class CronEntryTests(unittest.TestCase):

    def test_no_course_omits_it(self) -> None:
        lines = build_cron_entry("Roma EUR", 1, "09:00")
        self.assertNotIn("--course", lines[2])

    def test_includes_retry_flags(self) -> None:
        lines = build_cron_entry(
            "Test", 3, "10:00", course="Spin",
            max_retries=5, retry_interval=120,
        )
        self.assertIn("--retry 5", lines[2])
        self.assertIn("--retry-interval 120", lines[2])

    def test_comment_line_has_details(self) -> None:
        lines = build_cron_entry("Test", 4, "12:00")
        self.assertIn("Test", lines[0])
        self.assertIn("Friday", lines[0])
        self.assertIn("12:00", lines[0])


# =====================================================================
# 3. worker_book_recurring tests (6 tests)
# =====================================================================


class WorkerBookTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_success_first_attempt(self) -> None:

        class SuccessClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="100c220", booking_id="100",
                    booking_center="220", title="Yoga",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200, "StatusMessage": "ok"}

        with patch("va_cli.automate.VirginActiveClient", SuccessClient):
            with patch("va_cli.automate.time.sleep"):
                result = worker_book_recurring(
                    state_dir=self.state_dir,
                    club="Roma EUR",
                    course="Yoga",
                    day_of_week=0,
                    time_str="18:00",
                    max_retries=3,
                    retry_interval=1,
                )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 1)

    def test_retry_on_error_then_succeeds(self) -> None:
        attempts = [0]

        class RetryClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="100c220", booking_id="100",
                    booking_center="220", title="Yoga",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                attempts[0] += 1
                if attempts[0] >= 2:
                    return {"StatusCode": 200, "StatusMessage": "ok"}
                raise VAError("temporarily unavailable")

        with patch("va_cli.automate.VirginActiveClient", RetryClient):
            with patch("va_cli.automate.time.sleep"):
                result = worker_book_recurring(
                    state_dir=self.state_dir,
                    club="Roma EUR", course="Yoga",
                    day_of_week=0, time_str="18:00",
                    max_retries=5, retry_interval=1,
                )
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["attempts"], 2)

    def test_exhausts_retries_raises(self) -> None:

        class AlwaysFailClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="100c220", booking_id="100",
                    booking_center="220", title="Yoga",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                raise VAError("server error")

        with patch("va_cli.automate.VirginActiveClient", AlwaysFailClient):
            with patch("va_cli.automate.time.sleep"):
                with self.assertRaises(VAError):
                    worker_book_recurring(
                        state_dir=self.state_dir,
                        club="Roma EUR", course="Yoga",
                        day_of_week=0, time_str="18:00",
                        max_retries=2, retry_interval=1,
                    )

    def test_missing_params_raises(self) -> None:
        with self.assertRaises(VAError):
            worker_book_recurring(
                state_dir=self.state_dir,
                club=None,
                course=None,
                day_of_week=0,
                time_str="18:00",
                max_retries=3,
                retry_interval=1,
            )

    def test_fallback_to_course_name_match(self) -> None:

        class FallbackClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="200c220", booking_id="200",
                    booking_center="220", title="Yoga Calm Advanced",
                    date="2026-05-15", start_time="19:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", FallbackClient):
            with patch("va_cli.automate.time.sleep"):
                result = worker_book_recurring(
                    state_dir=self.state_dir,
                    club="Roma EUR", course="Yoga Calm",
                    day_of_week=0, time_str="18:00",
                    max_retries=1, retry_interval=1,
                )
        self.assertEqual(result["status"], "success")

    def test_relogins_on_stale_session(self) -> None:
        login_called = [False]

        class StaleClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return False
            def login(self):
                login_called[0] = True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="300c220", booking_id="300",
                    booking_center="220", title="Yoga",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", StaleClient):
            with patch("va_cli.automate.time.sleep"):
                result = worker_book_recurring(
                    state_dir=self.state_dir,
                    club="Roma EUR", course="Yoga",
                    day_of_week=0, time_str="18:00",
                    max_retries=1, retry_interval=1,
                )
        self.assertTrue(login_called[0])
        self.assertEqual(result["status"], "success")


# =====================================================================
# 4. _do_book_attempt tests (3 tests)
# =====================================================================


class DoBookAttemptTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.config = make_config(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_matches_by_time(self) -> None:

        class MatchClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="100c220", booking_id="100",
                    booking_center="220", title="Yoga",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", MatchClient):
            result = _do_book_attempt(
                self.config, "Roma EUR", "Yoga", 0, "18:00"
            )
        self.assertEqual(result["StatusCode"], 200)

    def test_raises_on_no_match(self) -> None:

        class NoMatchClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="100c220", booking_id="100",
                    booking_center="220", title="Spinning",
                    date="2026-05-15", start_time="09:00",
                )]

        with patch("va_cli.automate.VirginActiveClient", NoMatchClient):
            with self.assertRaises(VAError):
                _do_book_attempt(self.config, "Roma EUR", "Yoga", 0, "18:00")

    def test_fallback_course_name(self) -> None:

        class FallbackClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="200c220", booking_id="200",
                    booking_center="220", title="Yoga Calm",
                    date="2026-05-15", start_time="19:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", FallbackClient):
            result = _do_book_attempt(self.config, "Roma EUR", "Yoga Calm", 0, "18:00")
        self.assertEqual(result["StatusCode"], 200)


# =====================================================================
# 5. CLI integration (5 tests)
# =====================================================================


class FakeAutomateClient:
    def __init__(self, config, *_args, **_kwargs) -> None:
        self.config = config
        self.list_classes_calls: list = []
        self.has_session = True

    def __enter__(self) -> "FakeAutomateClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def login(self):
        return {"status": "ok"}

    def has_saved_session(self) -> bool:
        return self.has_session

    def get_calendar_filters(self):
        from va_cli.models import CalendarFilters, FilterOption
        return CalendarFilters(
            courses=[FilterOption(label="Yoga Calm", value="yoga")],
            trainers=[],
            clubs=[FilterOption(label="Roma EUR", value="eur")],
            targets=[],
        )

    def list_classes(self, filters: dict, **_kw) -> list:
        self.list_classes_calls.append(filters)
        return [CalendarClass(
            index=1, token="555c220", booking_id="555",
            booking_center="220", title="Yoga Calm",
            date="2026-05-13", start_time="18:00",
            club="Roma EUR", status="bookable",
        )]

    def book(self, token: str, **_kw):
        return {"StatusCode": 200}


class CliRecurringTests(unittest.TestCase):

    @patch("va_cli.cli.CredentialStore")
    def test_book_recurring_missing_club_exits(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with self.assertRaises(SystemExit) as exc:
            cli.main(["book", "--recurring", "--day", "0", "--time", "18:00"])
        self.assertEqual(exc.exception.code, 2)

    @patch("va_cli.cli.CredentialStore")
    def test_book_recurring_missing_day_exits(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with self.assertRaises(SystemExit) as exc:
            cli.main(["book", "--recurring", "--club", "X", "--time", "18:00"])
        self.assertEqual(exc.exception.code, 2)

    @patch("va_cli.cli.CredentialStore")
    def test_book_recurring_with_all_flags(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with patch("va_cli.cli.worker_book_recurring") as mock_worker:
                mock_worker.return_value = {"status": "success", "attempts": 1}
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = cli.main([
                        "book", "--recurring",
                        "--club", "Roma EUR", "--course", "Yoga",
                        "--day", "0", "--time", "18:00",
                        "--retry", "5", "--retry-interval", "30",
                    ])
        self.assertEqual(code, 0)
        mock_worker.assert_called_once()
        kwargs = mock_worker.call_args[1]
        self.assertEqual(kwargs["club"], "Roma EUR")
        self.assertEqual(kwargs["course"], "Yoga")
        self.assertEqual(kwargs["day_of_week"], 0)
        self.assertEqual(kwargs["time_str"], "18:00")
        self.assertEqual(kwargs["max_retries"], 5)
        self.assertEqual(kwargs["retry_interval"], 30)

    @patch("va_cli.cli.CredentialStore")
    def test_book_without_recurring_requires_token(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with self.assertRaises(SystemExit) as exc:
                cli.main(["book"])
        self.assertEqual(exc.exception.code, 2)

    @patch("va_cli.cli.CredentialStore")
    def test_automate_cron_login_works(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main(["automate", "cron-login"])
        self.assertEqual(code, 0)


# =====================================================================
# 11. Crontab entry ID marker (3 tests)
# =====================================================================


class CronEntryMarkerTests(unittest.TestCase):

    def test_marker_in_login_and_book(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", entry_id="abc12345")
        self.assertIn(VA_MARKER_PREFIX + "abc12345", lines[1])
        self.assertIn(VA_MARKER_PREFIX + "abc12345", lines[2])

    def test_marker_not_in_comment(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", entry_id="abc12345")
        self.assertNotIn(VA_MARKER_PREFIX, lines[0])

    def test_auto_generates_id_when_none(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00")
        for i in (1, 2):
            self.assertIn(VA_MARKER_PREFIX, lines[i])


# =====================================================================
# 12. Cronline parser (3 tests)
# =====================================================================


class CronlineParserTests(unittest.TestCase):

    def test_parses_book_line(self) -> None:
        line = "00 18 * * 6 va book --recurring --club 'Roma EUR' --course 'Yoga' --day 0 --time '18:00' --retry 10 --retry-interval 60 # va-automate:abc12345"
        result = _cronline_to_dict(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "abc12345")
        self.assertEqual(result["club"], "Roma EUR")
        self.assertEqual(result["course"], "Yoga")
        self.assertEqual(result["day"], "Monday")
        self.assertEqual(result["time"], "18:00")

    def test_skips_login_line(self) -> None:
        line = "55 17 * * 5 va login # va-automate:abc12345"
        result = _cronline_to_dict(line)
        self.assertIsNone(result)

    def test_skips_comment_line(self) -> None:
        result = _cronline_to_dict("# Roma EUR — Yoga — Monday 18:00")
        self.assertIsNone(result)


# =====================================================================
# 13. List command (2 tests)
# =====================================================================


class CmdListTests(unittest.TestCase):

    def test_empty_crontab(self) -> None:
        with patch("va_cli.automate._read_crontab", return_value=""):
            result = cmd_list()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["entries"], [])

    def test_returns_entries(self) -> None:
        crontab = (
            "# Roma EUR\n"
            "55 17 * * 5 va login # va-automate:abc12345\n"
            "00 18 * * 6 va book --recurring --club 'Roma EUR' --day 0 --time '18:00' --retry 10 --retry-interval 60 # va-automate:abc12345\n"
        )
        with patch("va_cli.automate._read_crontab", return_value=crontab):
            result = cmd_list()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["entries"][0]["id"], "abc12345")


# =====================================================================
# 14. Remove command (2 tests)
# =====================================================================


class CmdRemoveTests(unittest.TestCase):

    def test_found_and_removed(self) -> None:
        crontab = (
            "# Yoga\n"
            "55 17 * * 5 va login # va-automate:abc12345\n"
            "00 18 * * 6 va book --recurring --club 'Roma EUR' --day 0 --time '18:00' --retry 10 --retry-interval 60 # va-automate:abc12345\n"
        )
        written: list[str] = []

        def _fake_write(content: str) -> None:
            written.append(content)

        with patch("va_cli.automate._read_crontab", return_value=crontab):
            with patch("va_cli.automate._write_crontab", side_effect=_fake_write):
                result = cmd_remove("abc12345")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["removed"], "abc12345")
        self.assertEqual(len(written), 1)
        self.assertNotIn("abc12345", written[0])

    def test_not_found_raises(self) -> None:
        with patch("va_cli.automate._read_crontab", return_value="00 00 * * * echo hello"):
            with self.assertRaises(VAError) as exc:
                cmd_remove("nonexistent")
        self.assertIn("nonexistent", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
