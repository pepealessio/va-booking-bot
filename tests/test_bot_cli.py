from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from va_bot import cli
from va_bot.state import BotOccurrenceState
from va_cli.config import Config
from va_cli.credentials import SavedCredentials
from va_cli.models import CalendarClass, CalendarDateOption, CalendarFilters, FilterOption


class FakeCredentialStore:
    def load(self):
        return SavedCredentials(username="user@example.com", password="secret")


class FakeClient:
    def __init__(self, config, *_args, **_kwargs) -> None:
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def has_saved_session(self) -> bool:
        return True

    def _ensure_site_session(self) -> None:
        return None

    def get_calendar_filters(self):
        return CalendarFilters(
            courses=[FilterOption(label="Reformer Pilates Align", value="Reformer Pilates Align")],
            trainers=[FilterOption(label="Alice", value="Alice")],
            clubs=[FilterOption(label="Roma EUR", value="Roma EUR")],
            targets=[FilterOption(label="Forza", value="force-id")],
        )

    def get_calendar_dates(self, _filters):
        return [CalendarDateOption(date="2026-03-18", weekday="MERCOLEDI", day_number="18", selected=True)]

    def list_classes(self, filters, **_kwargs):
        return [
            CalendarClass(
                index=1,
                token="355132c220",
                booking_id="355132",
                booking_center="220",
                title="Reformer Pilates Align",
                date=filters["date"],
                start_time="18:00",
                end_time="19:00",
                club="Roma EUR",
                trainer="Alice",
                status="bookable",
            )
        ]

    def book(self, _token):
        return {"status": "booked"}

    def login(self) -> None:
        return None


class BotCliTests(unittest.TestCase):
    @patch("va_bot.cli.CredentialStore", FakeCredentialStore)
    @patch("va_bot.cli.VirginActiveClient", FakeClient)
    def test_validate_outputs_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "va-bot.yml"
            config_path.write_text(
                """
timezone: UTC
rules:
  - name: eur-wed
    club: Roma EUR
    course: Reformer Pilates Align
    weekday: wednesday
    time: "18:00"
""".strip(),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = cli.main(["validate", "--config", str(config_path)])
            self.assertEqual(exit_code, 0)
            self.assertIn("eur-wed", stdout.getvalue())
            self.assertIn("355132c220", stdout.getvalue())

    @patch("va_bot.cli.CredentialStore", FakeCredentialStore)
    @patch("va_bot.cli.VirginActiveClient", FakeClient)
    def test_init_writes_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "va-bot.yml"
            answers = iter(
                [
                    "UTC",
                    "2",
                    "15",
                    "1",
                    "1",
                    "3",
                    "1",
                    "1",
                    "eur-wed",
                    "y",
                    "",
                    "4",
                ]
            )
            stdout = io.StringIO()
            with patch("builtins.input", side_effect=lambda _prompt="": next(answers)):
                with redirect_stdout(stdout):
                    exit_code = cli.main(["init", "--config", str(config_path)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(config_path.exists())
            contents = config_path.read_text(encoding="utf-8")
            self.assertIn("eur-wed", contents)
            self.assertIn("timezone: UTC", contents)
            self.assertIn("trainer: Alice", contents)

    @patch("va_bot.cli.CredentialStore", FakeCredentialStore)
    @patch("va_bot.cli.VirginActiveClient", FakeClient)
    def test_init_can_edit_existing_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "va-bot.yml"
            config_path.write_text(
                """
timezone: UTC
preflight_minutes: 2
retry_window_seconds: 15
retry_interval_seconds: 1
rules:
  - name: eur-wed
    club: Roma EUR
    course: Reformer Pilates Align
    weekday: wednesday
    time: "18:00"
""".strip(),
                encoding="utf-8",
            )
            answers = iter(
                [
                    "",
                    "",
                    "",
                    "",
                    "2",
                    "1",
                    "1",
                    "3",
                    "1",
                    "1",
                    "eur-wed-updated",
                    "n",
                    "",
                    "4",
                ]
            )
            stdout = io.StringIO()
            with patch("builtins.input", side_effect=lambda _prompt="": next(answers)):
                with redirect_stdout(stdout):
                    exit_code = cli.main(["init", "--config", str(config_path)])
            self.assertEqual(exit_code, 0)
            contents = config_path.read_text(encoding="utf-8")
            self.assertIn("eur-wed-updated", contents)
            self.assertNotIn("trainer:", contents)

    @patch("va_bot.cli.CredentialStore", FakeCredentialStore)
    @patch("va_bot.cli.VirginActiveClient", FakeClient)
    def test_plan_outputs_next_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "va-bot.yml"
            config_path.write_text(
                """
timezone: UTC
rules:
  - name: eur-wed
    club: Roma EUR
    course: Reformer Pilates Align
    weekday: wednesday
    time: "18:00"
""".strip(),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = cli.main(["plan", "--config", str(config_path)])
            self.assertEqual(exit_code, 0)
            self.assertIn("booking_opens", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
