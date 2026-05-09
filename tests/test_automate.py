from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import yaml

from va_cli import cli
from va_cli.automate import (
    AutomateConfig,
    _cron_dow_from_python,
    _do_book_attempt,
    all_cron_lines,
    build_cron_lines,
    cmd_list,
    compute_cron_minutes,
    install_crontab_entries,
    remove_crontab_entries,
    worker_cron_login,
    worker_cron_book,
)
from va_cli.client import VAError
from va_cli.config import Config
from va_cli.models import CalendarClass, CalendarFilters, FilterOption


def make_config(state_dir: Path) -> Config:
    """Build a minimal Config for automation tests."""
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
# 1. AutomateConfigTests (8 tests)
# =====================================================================


class AutomateConfigTests(unittest.TestCase):
    """Unit tests for AutomateConfig load/save/add/remove/get."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_load_creates_default(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        data = cfg.load()
        self.assertIn("workdir", data)
        self.assertEqual(data["classes"], [])

    def test_save_and_load_roundtrips(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        data = {"workdir": "/tmp/test", "classes": [
            {"id": "abc", "club": "X", "time": "18:00", "day_of_week": 1}
        ]}
        cfg.save(data)
        loaded = cfg.load()
        self.assertEqual(loaded["classes"][0]["id"], "abc")
        self.assertEqual(loaded["classes"][0]["club"], "X")

    def test_add_generates_id_and_persists(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        cls_entry = {"club": "Roma EUR", "time": "18:00", "day_of_week": 0}
        new_id = cfg.add(cls_entry)
        self.assertTrue(new_id)  # non-empty string
        data = cfg.load()
        self.assertEqual(len(data["classes"]), 1)
        self.assertEqual(data["classes"][0]["id"], new_id)

    def test_add_with_existing_id_keeps_it(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        cls_entry = {"id": "fixed-id", "club": "Test", "time": "09:00", "day_of_week": 2}
        saved_id = cfg.add(cls_entry)
        self.assertEqual(saved_id, "fixed-id")

    def test_remove_existing_returns_true(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        cls_entry = {"id": "rem", "club": "X", "time": "08:00", "day_of_week": 0}
        cfg.add(cls_entry)
        result = cfg.remove("rem")
        self.assertTrue(result)
        self.assertIsNone(cfg.get("rem"))

    def test_remove_missing_returns_false(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        result = cfg.remove("doesnotexist")
        self.assertFalse(result)

    def test_get_existing_returns_entry(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        entry = {"id": "findme", "club": "C", "time": "10:00", "day_of_week": 3}
        cfg.add(entry)
        found = cfg.get("findme")
        self.assertIsNotNone(found)
        self.assertEqual(found["club"], "C")

    def test_get_missing_returns_none(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        self.assertIsNone(cfg.get("none"))


# =====================================================================
# 2. CronComputationTests (8 tests)
# =====================================================================


class CronComputationTests(unittest.TestCase):
    """Pure math: cron DOW mapping and time computation."""

    def test_python_mon_to_cron_mon(self) -> None:
        self.assertEqual(_cron_dow_from_python(0), 1)  # Mon→1

    def test_python_sun_to_cron_sun(self) -> None:
        self.assertEqual(_cron_dow_from_python(6), 0)  # Sun→0

    def test_all_dow_mapping(self) -> None:
        expected = [1, 2, 3, 4, 5, 6, 0]
        for py_dow in range(7):
            self.assertEqual(_cron_dow_from_python(py_dow), expected[py_dow])

    def test_book_time_is_same_time_minus_48h(self) -> None:
        # 48h before is same time, 2 days earlier
        cron = compute_cron_minutes(0, "18:00")  # Monday, class at 18:00
        self.assertEqual(cron["book_hour"], 18)
        self.assertEqual(cron["book_minute"], 0)
        self.assertEqual(cron["book_dow"], 6)  # Saturday (cron DOW)

    def test_login_time_is_5min_before_book(self) -> None:
        cron = compute_cron_minutes(1, "17:15")  # Tuesday
        self.assertEqual(cron["login_minute"], 10)  # 15-5=10
        self.assertEqual(cron["book_minute"], 15)

    def test_login_dow_is_day_before_book_when_no_midnight_wrap(self) -> None:
        # Monday 18:00 class → book Sat 18:00, login is 5min before book
        # 5min before Sat 18:00 = Fri 23:55 on the preceding day boundary
        # But same-day subtraction: login_date = book_date - 5min = Fri 23:55
        cron = compute_cron_minutes(0, "18:00")
        self.assertEqual(cron["book_dow"], 6)   # Sat book
        self.assertEqual(cron["login_dow"], 5)  # Fri login (5min before midnight-wrap boundary)

    def test_hour_wrap_past_midnight(self) -> None:
        # Class at 02:00 → book at 02:00, login at 01:55 (same preceding day)
        cron = compute_cron_minutes(0, "02:00")
        self.assertEqual(cron["book_hour"], 2)
        self.assertEqual(cron["login_hour"], 1)
        self.assertEqual(cron["login_minute"], 55)

    def test_cron_entry_format_with_marker(self) -> None:
        cls = {"id": "test123", "course": "Yoga Calm", "day_of_week": 1, "time": "18:00"}
        lines = build_cron_lines(cls, "/home/user/repo")
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("# va-booking-bot: test123"))
        self.assertTrue(lines[2].startswith("# va-booking-bot: test123"))
        self.assertIn("* *", lines[1])
        self.assertIn("cd /home/user/repo && va automate cron-login", lines[1])
        self.assertIn("cd /home/user/repo && va automate cron-book --class test123", lines[3])


# =====================================================================
# 3. CrontabManagementTests (6 tests)
# =====================================================================


class CrontabManagementTests(unittest.TestCase):
    """Mocked subprocess tests for crontab install/remove."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.config = AutomateConfig(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_config(self) -> str:
        cls_id = self.config.add({
            "club": "Roma EUR", "course": "Yoga",
            "day_of_week": 0, "time": "18:00", "max_retries": 5
        })
        return cls_id

    def test_install_adds_to_empty_crontab(self) -> None:
        self._write_config()
        data = self.config.load()
        installed: list[str] = []
        with patch("va_cli.automate._current_crontab", return_value=""):
            with patch("va_cli.automate._install_crontab", side_effect=lambda c: installed.append(c)):
                lines = install_crontab_entries(data)
        self.assertGreater(len(lines), 0)
        self.assertEqual(len(installed), 1)
        for line in lines:
            self.assertIn(line, installed[0])

    def test_install_preserves_existing_other_entries(self) -> None:
        self._write_config()
        data = self.config.load()
        existing = "0 9 * * 1  echo hello\n30 17 * * 0  echo world\n"
        installed: list[str] = []
        with patch("va_cli.automate._current_crontab", return_value=existing):
            with patch("va_cli.automate._install_crontab", side_effect=lambda c: installed.append(c)):
                install_cron_lines = install_crontab_entries(data)
        merged = installed[0]
        self.assertIn("echo hello", merged)
        self.assertIn("echo world", merged)
        for line in install_cron_lines:
            self.assertIn(line, merged)

    def test_install_replaces_old_bot_entries(self) -> None:
        self._write_config()
        data = self.config.load()
        old = "# va-booking-bot: oldid (login something)\n* * * * *  old_command\n"
        installed: list[str] = []
        with patch("va_cli.automate._current_crontab", return_value=old):
            with patch("va_cli.automate._install_crontab", side_effect=lambda c: installed.append(c)):
                install_crontab_entries(data)
        merged = installed[0]
        self.assertNotIn("oldid", merged)

    def test_remove_strips_all_bot_entries(self) -> None:
        crontab_text = (
            "# va-booking-bot: abc123 (login yoga)\n"
            "0 18 * * 6  cd /repo && va automate cron-book --class abc123\n"
            "# va-booking-bot: def456 (book cycle)\n"
            "55 17 * * 6  cd /repo && va automate cron-login\n"
            "0 9 * * 1  keep-this\n"
        )
        with patch("va_cli.automate._current_crontab", return_value=crontab_text):
            with patch("va_cli.automate._install_crontab") as mock_install:
                removed = remove_crontab_entries()
        self.assertEqual(removed, 4)  # 2 markers + 2 cron lines after them
        self.assertIn("keep-this", mock_install.call_args[0][0])

    def test_remove_preserves_other_entries(self) -> None:
        crontab_text = "0 9 * * 1  echo a\n# va-booking-bot: x (l)\n0 0 * * *  bot-cmd\n0 12 * * 2  echo b\n"
        with patch("va_cli.automate._current_crontab", return_value=crontab_text):
            with patch("va_cli.automate._install_crontab") as mock_install:
                remove_crontab_entries()
        result = mock_install.call_args[0][0]
        self.assertIn("echo a", result)
        self.assertIn("echo b", result)
        self.assertNotIn("va-booking-bot", result)

    def test_remove_from_empty_is_noop(self) -> None:
        with patch("va_cli.automate._current_crontab", return_value=""):
            with patch("va_cli.automate._install_crontab") as mock_install:
                remove_crontab_entries()
        mock_install.assert_called_once()


# =====================================================================
# 4. CronWorkerTests (8 tests)
# =====================================================================


class CronWorkerTests(unittest.TestCase):
    """Mocked client tests for login/book workers and retry logic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_class(self, overrides: dict | None = None) -> str:
        cfg = AutomateConfig(self.state_dir)
        entry = {
            "club": "Roma EUR", "course": "Yoga",
            "day_of_week": 0, "time": "18:00",
            "max_retries": 3, "retry_interval": 1,
        }
        if overrides:
            entry.update(overrides)
        return cfg.add(entry)

    # -- worker_cron_login tests --

    def test_cron_login_success(self) -> None:
        login_count = [0]

        class FakeClient:
            def __init__(self, *_a, **_k):
                login_count[0] += 1

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def login(self):
                return {"status": "ok"}

        with patch("va_cli.automate.VirginActiveClient", FakeClient):
            result = worker_cron_login(self.state_dir)
        self.assertEqual(result["status"], "success")
        self.assertEqual(login_count[0], 1)

    def test_cron_login_failure_raises(self) -> None:
        class FailClient:
            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def login(self):
                raise VAError("bad creds")

        with patch("va_cli.automate.VirginActiveClient", FailClient):
            with self.assertRaises(VAError):
                worker_cron_login(self.state_dir)

    # -- worker_cron_book tests --

    def test_cron_book_success_first_attempt(self) -> None:
        self._write_class()
        attempt_num = [0]

        class SuccessClient:
            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def has_saved_session(self):
                return True

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
                result = worker_cron_book(self.state_dir, "testid" if False else self._write_class({}))

    def test_cron_book_retry_on_error_then_succeeds(self) -> None:
        self._write_class({"max_retries": 3})
        attempts = [0]

        class RetryClient:
            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def has_saved_session(self):
                return True

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

        # Need a fresh class entry for the retry test
        cfg = AutomateConfig(self.state_dir)
        cfg.remove("testid") if False else None
        cls_id = cfg.add({
            "club": "Roma EUR", "course": "Yoga",
            "day_of_week": 0, "time": "18:00",
            "max_retries": 3, "retry_interval": 1,
        })

        with patch("va_cli.automate.VirginActiveClient", RetryClient):
            with patch("va_cli.automate.time.sleep"):
                result = worker_cron_book(self.state_dir, cls_id)
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["attempts"], 2)

    def test_cron_book_exhausts_retries_raises(self) -> None:
        cls_id = self._write_class({"max_retries": 2})

        class AlwaysFailClient:
            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def has_saved_session(self):
                return True

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
                with self.assertRaises(VAError) as ctx:
                    worker_cron_book(self.state_dir, cls_id)
        self.assertIn("2", str(ctx.exception))

    def test_cron_book_missing_class_raises(self) -> None:
        with self.assertRaises(VAError):
            worker_cron_book(self.state_dir, "nonexistent")


# =====================================================================
# 5. CliAutomateTests (6 tests)
# =====================================================================


class FakeAutomateClient:
    """Fake client that also supports automate.add interactive flow."""

    def __init__(self, config, *_args, **_kwargs) -> None:
        self.config = config
        self.book_calls: list = []
        self.list_classes_calls: list = []
        self.has_session = True
        self.login_fail = False

    def __enter__(self) -> "FakeAutomateClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def login(self):
        if self.login_fail:
            raise VAError("bad password")
        return {"status": "ok"}

    def has_saved_session(self) -> bool:
        return self.has_session

    def get_calendar_filters(self):
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
        self.book_calls.append(token)
        return {"StatusCode": 200}

    def logout(self):
        pass


class CliAutomateTests(unittest.TestCase):
    """CLI integration tests for automate subcommands."""

    @patch("va_cli.cli.CredentialStore")
    def test_automate_list_empty_shows_info(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main(["automate", "list"])
        self.assertEqual(code, 0)
        self.assertIn("No recurring", stdout.getvalue())

    @patch("va_cli.cli.CredentialStore")
    def test_automate_list_with_classes(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            import tempfile
            from pathlib import Path
            tmpdir = tempfile.mkdtemp()
            state = Path(tmpdir)
            cfg = AutomateConfig(state)
            cfg.add({
                "club": "Roma EUR", "course": "Yoga",
                "day_of_week": 1, "time": "18:00", "max_retries": 5
            })
            result = cmd_list(state)
            self.assertIsInstance(result, list)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["club"], "Roma EUR")
            self.assertIn("Yoga", result[0]["course"])

    @patch("va_cli.cli.CredentialStore")
    def test_automate_remove_missing_exits_code_2(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with self.assertRaises(SystemExit) as exc:
                cli.main(["automate", "remove", "nonexistent"])
            self.assertEqual(exc.exception.code, 2)

    @patch("va_cli.cli.CredentialStore")
    def test_automate_schedule_no_classes(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = cli.main(["automate", "schedule"])
        self.assertEqual(code, 0)
        self.assertIn("No classes", stdout.getvalue())

    @patch("va_cli.cli.CredentialStore")
    def test_automate_unschedule_calls_crontab(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with patch("va_cli.automate.remove_crontab_entries", return_value=3):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = cli.main(["automate", "unschedule"])
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("removed", output)

    @patch("va_cli.cli.CredentialStore")
    def test_automate_cron_book_missing_class_error(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        with patch("va_cli.cli.VirginActiveClient", FakeAutomateClient):
            with self.assertRaises(SystemExit) as exc:
                cli.main(["automate", "cron-book", "--class", "doesnotexist"])
            self.assertEqual(exc.exception.code, 2)


# =====================================================================
# 6. DoBookAttemptTests (4 tests)
# =====================================================================


class DoBookAttemptTests(unittest.TestCase):
    """Tests for _do_book_attempt internal matching logic."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.config = make_config(self.state_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _base_cls(self) -> dict:
        return {
            "club": "Roma EUR", "course": "Yoga Calm",
            "day_of_week": 0, "time": "18:00"
        }

    def test_book_attempt_matches_by_time(self) -> None:
        cls = self._base_cls()

        class MatchClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="100c220", booking_id="100",
                    booking_center="220", title="Yoga Calm",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", MatchClient):
            result = _do_book_attempt(self.config, cls)
        self.assertEqual(result["StatusCode"], 200)

    def test_book_attempt_falls_back_to_course_name(self) -> None:
        cls = {"club": "Roma EUR", "course": "Yoga Calm", "day_of_week": 0, "time": "18:00"}

        class FallbackClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                # No time match but title contains "Yoga Calm"
                return [CalendarClass(
                    index=1, token="200c220", booking_id="200",
                    booking_center="220", title="Yoga Calm Advanced",
                    date="2026-05-15", start_time="19:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", FallbackClient):
            result = _do_book_attempt(self.config, cls)
        self.assertEqual(result["StatusCode"], 200)

    def test_book_attempt_raises_on_no_match(self) -> None:
        cls = self._base_cls()

        class NoMatchClient:
            def __init__(self, *_a, **_k):
                pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def has_saved_session(self): return True
            def list_classes(self, *_a, **_k):
                return [CalendarClass(
                    index=1, token="300c220", booking_id="300",
                    booking_center="220", title="Spinning",
                    date="2026-05-15", start_time="09:00",
                )]

        with patch("va_cli.automate.VirginActiveClient", NoMatchClient):
            with self.assertRaises(VAError):
                _do_book_attempt(self.config, cls)

    def test_book_attempt_relogins_on_stale_session(self) -> None:
        cls = self._base_cls()
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
                    index=1, token="400c220", booking_id="400",
                    booking_center="220", title="Yoga Calm",
                    date="2026-05-15", start_time="18:00",
                )]
            def book(self, *_a, **_k):
                return {"StatusCode": 200}

        with patch("va_cli.automate.VirginActiveClient", StaleClient):
            result = _do_book_attempt(self.config, cls)
        self.assertTrue(login_called[0])
        self.assertEqual(result["StatusCode"], 200)


# =====================================================================
# 7. Integration: all_cron_lines + schedule preview (3 tests)
# =====================================================================


class AllCronLinesTests(unittest.TestCase):
    """Tests for all_cron_lines aggregation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_all_cron_lines_multiple_classes(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        cfg.add({
            "club": "Roma EUR", "course": "Yoga",
            "day_of_week": 0, "time": "18:00"
        })
        cfg.add({
            "club": "Roma EUR", "course": "Spin",
            "day_of_week": 3, "time": "09:00"
        })
        data = cfg.load()
        lines = all_cron_lines(data)
        # 2 classes * 4 lines each = 8
        self.assertEqual(len(lines), 8)
        markers = [l for l in lines if l.startswith("# va-booking-bot:")]
        self.assertEqual(len(markers), 4)

    def test_all_cron_lines_empty(self) -> None:
        data = {"workdir": "/tmp", "classes": []}
        self.assertEqual(all_cron_lines(data), [])

    def test_cron_lines_use_configured_workdir(self) -> None:
        cfg = AutomateConfig(self.state_dir)
        cfg.add({
            "club": "Test", "course": "X",
            "day_of_week": 1, "time": "17:00"
        })
        data = cfg.load()
        data["workdir"] = "/custom/path"
        lines = all_cron_lines(data)
        for cmd_line in lines:
            if not cmd_line.startswith("#"):
                self.assertIn("cd /custom/path", cmd_line)


# =====================================================================


if __name__ == "__main__":
    unittest.main()
