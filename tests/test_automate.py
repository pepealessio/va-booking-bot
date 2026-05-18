from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

from va_cli import cli
from va_cli.automate import (
    VA_MARKER_PREFIX,
    _cronline_to_dict,
    _do_remove,
    build_cron_entry,
    cmd_list,
    compute_cron_times,
    worker_book,
)
from va_cli.client import VAError
from va_cli.models import CalendarClass


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
        # Monday → book Saturday, login also Saturday (5min offset can't cross day boundary)
        cron = compute_cron_times(0, "18:00")
        self.assertEqual(cron["book_dow"], 6)    # Sat
        self.assertEqual(cron["login_dow"], 6)   # Sat (same day as book)

    def test_hour_wrap_past_midnight(self) -> None:
        cron = compute_cron_times(0, "02:00")
        self.assertEqual(cron["book_hour"], 2)
        self.assertEqual(cron["login_hour"], 1)
        self.assertEqual(cron["login_minute"], 55)

    def test_cron_entry_format_with_markers(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", course="Yoga")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("#"))
        self.assertIn("va login --notify f &&", lines[1])
        self.assertIn("--dangerously-approve-token --json classes", lines[1])
        self.assertIn("--club 'Roma EUR'", lines[1])
        self.assertIn("--course 'Yoga'", lines[1])
        self.assertIn("$(cat /tmp/va_booking_", lines[2])


# =====================================================================
# 2. Cron entry generation (3 tests)
# =====================================================================


class CronEntryTests(unittest.TestCase):

    def test_no_course_omits_it(self) -> None:
        lines = build_cron_entry("Roma EUR", 1, "09:00")
        self.assertNotIn("--course", lines[1])

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

    def test_cron_entry_includes_notify_flags(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", course="Yoga")
        self.assertIn("login --notify f", lines[1])
        self.assertIn("--json classes --notify f", lines[1])
        self.assertIn("book --notify sf", lines[2])


# =====================================================================
# 3. Worker book with retry (3 tests)
# =====================================================================


class WorkerBookTests(unittest.TestCase):

    def test_success_first_attempt(self) -> None:
        mock_client = MagicMock()
        mock_client.book.return_value = {"StatusCode": 200, "StatusMessage": "ok"}

        with patch("va_cli.automate.time.sleep"):
            result = worker_book(
                client=mock_client, token="100c220",
                approve=lambda _: True,
                max_retries=3, retry_interval=1,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 1)

    def test_retry_on_error_then_succeeds(self) -> None:
        mock_client = MagicMock()
        mock_client.book.side_effect = [VAError("temporarily unavailable"), {"StatusCode": 200}]

        with patch("va_cli.automate.time.sleep"):
            result = worker_book(
                client=mock_client, token="100c220",
                approve=lambda _: True,
                max_retries=5, retry_interval=1,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 2)

    def test_exhausts_retries_raises(self) -> None:
        mock_client = MagicMock()
        mock_client.book.side_effect = VAError("server error")

        with patch("va_cli.automate.time.sleep"):
            with self.assertRaises(VAError):
                worker_book(
                    client=mock_client, token="100c220",
                    approve=lambda _: True,
                    max_retries=2, retry_interval=1,
                )

    def test_notify_f_skips_success_send(self) -> None:
        mock_client = MagicMock()
        mock_client.book.return_value = {"StatusCode": 200}
        mock_notifier = MagicMock()

        with patch("va_cli.automate.time.sleep"):
            with patch("va_cli.automate.from_config", return_value=mock_notifier):
                result = worker_book(
                    client=mock_client, token="100c220",
                    approve=lambda _: True,
                    max_retries=1, retry_interval=1,
                    notify="f",
                )
        self.assertEqual(result["status"], "success")
        mock_notifier.send.assert_not_called()

    def test_notify_f_sends_error_on_failure(self) -> None:
        mock_client = MagicMock()
        mock_client.book.side_effect = VAError("server error")
        mock_notifier = MagicMock()

        with patch("va_cli.automate.time.sleep"):
            with patch("va_cli.automate.from_config", return_value=mock_notifier):
                with self.assertRaises(VAError):
                    worker_book(
                        client=mock_client, token="100c220",
                        approve=lambda _: True,
                        max_retries=2, retry_interval=1,
                        notify="f",
                    )
        mock_notifier.send.assert_called_once_with("error", "Booking failed for token **100c220** after 2 attempts — server error")


# =====================================================================
# 4. CLI integration (3 tests)
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


class CliBookTests(unittest.TestCase):

    @patch("va_cli.cli.CredentialStore")
    def test_book_missing_token_exits(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with self.assertRaises(SystemExit) as exc:
                cli.main(["book"])
        self.assertEqual(exc.exception.code, 2)

    @patch("va_cli.cli.CredentialStore")
    def test_book_direct_with_token(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main([
                    "--dangerously-approve-token", "book", "100c220",
                ])
        self.assertEqual(code, 0)

    @patch("va_cli.cli.CredentialStore")
    def test_book_with_retry(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with patch("va_cli.cli.worker_book") as mock_worker:
                mock_worker.return_value = {"status": "success", "attempts": 1}
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = cli.main([
                        "--dangerously-approve-token", "book",
                        "100c220", "--retry", "5", "--retry-interval", "30",
                    ])
        self.assertEqual(code, 0)
        mock_worker.assert_called_once()
        kwargs = mock_worker.call_args[1]
        self.assertEqual(kwargs["token"], "100c220")
        self.assertEqual(kwargs["max_retries"], 5)
        self.assertEqual(kwargs["retry_interval"], 30)

# =====================================================================
# 5. Crontab entry ID marker (3 tests)
# =====================================================================


class CronEntryMarkerTests(unittest.TestCase):

    def test_marker_in_all_lines(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", entry_id="abc12345")
        for i in range(3):
            self.assertIn(VA_MARKER_PREFIX + "abc12345", lines[i])

    def test_marker_in_comment(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00", entry_id="abc12345")
        self.assertIn(VA_MARKER_PREFIX, lines[0])
        self.assertIn("abc12345", lines[0])

    def test_auto_generates_id_when_none(self) -> None:
        lines = build_cron_entry("Roma EUR", 0, "18:00")
        self.assertIn(VA_MARKER_PREFIX, lines[0])
        self.assertIn(VA_MARKER_PREFIX, lines[1])


# =====================================================================
# 6. Cronline parser (3 tests)
# =====================================================================


class CronlineParserTests(unittest.TestCase):

    def test_parses_find_line(self) -> None:
        line = (
            "55 17 * * 6 va login && va --dangerously-approve-token --json classes"
            " --club 'Roma EUR' --course 'Yoga' --day 0 --time '18:00'"
            " | python3 -c \"import sys,json;print(json.load(sys.stdin)[0]['id'])\""
            " > /tmp/va_booking_abc12345 # va-automate:abc12345"
        )
        result = _cronline_to_dict(line)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "abc12345")
        self.assertEqual(result["club"], "Roma EUR")
        self.assertEqual(result["course"], "Yoga")
        self.assertEqual(result["day"], "Monday")
        self.assertEqual(result["time"], "18:00")

    def test_skips_comment_line(self) -> None:
        result = _cronline_to_dict("# Roma EUR — Yoga — Monday 18:00")
        self.assertIsNone(result)

    def test_skips_book_line(self) -> None:
        line = (
            "00 18 * * 6 va --dangerously-approve-token book $(cat /tmp/va_booking_abc12345)"
            " --retry 10 --retry-interval 60 # va-automate:abc12345"
        )
        result = _cronline_to_dict(line)
        self.assertIsNone(result)


# =====================================================================
# 7. List command (2 tests)
# =====================================================================


class CmdListTests(unittest.TestCase):

    def test_empty_crontab(self) -> None:
        result = cmd_list("")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["entries"], [])

    def test_returns_entries(self) -> None:
        crontab = (
            "# Roma EUR — any class — Monday 18:00 # va-automate:abc12345\n"
            "55 17 * * 6 va login && va --dangerously-approve-token --json classes"
            " --club 'Roma EUR' --day 0 --time '18:00'"
            " | python3 -c \"import sys,json;print(json.load(sys.stdin)[0]['id'])\""
            " > /tmp/va_booking_abc12345 # va-automate:abc12345\n"
            "00 18 * * 6 va --dangerously-approve-token book $(cat /tmp/va_booking_abc12345)"
            " --retry 10 --retry-interval 60 # va-automate:abc12345\n"
        )
        result = cmd_list(crontab)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["entries"][0]["id"], "abc12345")


# =====================================================================
# 8. Remove command (3 tests)
# =====================================================================


class CmdRemoveTests(unittest.TestCase):

    def test_found_and_removed(self) -> None:
        crontab = (
            "# Yoga # va-automate:abc12345\n"
            "55 17 * * 6 va login && va --dangerously-approve-token --json classes"
            " --club 'Roma EUR' --day 0 --time '18:00'"
            " | python3 -c \"import sys,json;print(json.load(sys.stdin)[0]['id'])\""
            " > /tmp/va_booking_abc12345 # va-automate:abc12345\n"
            "00 18 * * 6 va --dangerously-approve-token book $(cat /tmp/va_booking_abc12345)"
            " --retry 10 --retry-interval 60 # va-automate:abc12345\n"
        )
        result = _do_remove("abc12345", crontab)
        self.assertIsNotNone(result)
        self.assertNotIn("abc12345", result)

    def test_not_found_returns_none(self) -> None:
        result = _do_remove("nonexistent", "00 00 * * * echo hello\n")
        self.assertIsNone(result)

    def test_empty_crontab_returns_none(self) -> None:
        result = _do_remove("nonexistent", "")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
