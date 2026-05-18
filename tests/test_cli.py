from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from va_cli import cli
from va_cli.client import VAError
from va_cli.models import CalendarClass, CalendarDateOption, CalendarFilters, FilterOption


class FakeClient:
    def __init__(self, config, *_args, **_kwargs) -> None:
        self.config = config
        self.logged_out = False
        self.list_classes_calls: list[dict[str, object]] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def login(self):
        class Result:
            def to_dict(self) -> dict[str, str]:
                return {"status": "ok"}

        return Result()

    def has_saved_session(self) -> bool:
        return True

    def logout(self) -> None:
        self.logged_out = True

    def get_calendar_filters(self):
        return CalendarFilters(
            courses=[FilterOption(label="Yoga Calm", value="Yoga Calm")],
            trainers=[FilterOption(label="Francesco De Rose", value="Francesco De Rose")],
            clubs=[FilterOption(label="Roma Via Mantova", value="Roma Via Mantova")],
            targets=[FilterOption(label="Forza", value="force-id")],
        )

    def get_calendar_dates(self, _filters):
        return [CalendarDateOption(date="2026-03-13", weekday="VENERDI", day_number="13", selected=True)]

    def list_classes(self, _filters, **_kwargs):
        self.list_classes_calls.append(_kwargs)
        return [
            CalendarClass(
                index=1,
                token="208239c232",
                booking_id="208239",
                booking_center="232",
                title="Yoga Calm",
                date="2026-03-13",
                start_time="17:15",
                end_time="18:00",
                club="Roma Via Mantova",
                trainer="Francesco De Rose",
                room="Studio Active",
                status="bookable",
                button_label="Prenota",
            ),
            CalendarClass(
                index=2,
                token="208240c232",
                booking_id="208240",
                booking_center="232",
                title="Lift Club",
                date="2026-03-13",
                start_time="18:15",
                end_time="19:00",
                club="Roma Via Mantova",
                trainer="Francesco De Rose",
                status="queue",
                queue_length=15,
                button_label="15 utenti in attesa",
            ),
            CalendarClass(
                index=3,
                token="208241c232",
                booking_id="208241",
                booking_center="232",
                title="Reformer",
                date="2026-03-13",
                start_time="19:15",
                end_time="20:00",
                club="Roma Via Mantova",
                trainer="Francesco De Rose",
                status="queue_full",
                button_label="Prenota in lista di attesa piena",
            ),
            CalendarClass(
                index=4,
                token="208242c232",
                booking_id="208242",
                booking_center="232",
                title="Matwork",
                date="2026-03-13",
                start_time="20:15",
                end_time="21:00",
                club="Roma Via Mantova",
                trainer="Francesco De Rose",
                status="full",
                button_label="Prenotazioni non disponibili",
            ),
        ]

    def book(self, token: str, **_kwargs):
        return {"status": "booked", "token": token}

    def cancel(self, token: str, **_kwargs):
        return {"status": "cancelled", "token": token}

    def whoami(self, **_kwargs):
        return {"logged_in": True}


class CliTests(unittest.TestCase):
    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_debug_courses_output(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(["debug", "courses"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Yoga Calm", stdout.getvalue())

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_classes_plain_output(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(stdout):
                cli.main(["classes", "--club", "Roma Via Mantova", "--date", "2026-03-13"])
        output = stdout.getvalue()
        self.assertIn("ID", output)
        self.assertIn("208239c232", output)
        self.assertIn("bookable", output)
        self.assertIn("full", output)
        self.assertIn("queue_full", output)
        self.assertIn("15", output)
        self.assertNotIn("button_label", output)

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient")
    def test_classes_no_auth_forces_public_mode(self, client_cls, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        instance = FakeClient(None)
        client_cls.return_value.__enter__.return_value = instance
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli.main(["classes", "--no-auth", "--date", "2026-03-13"])
        self.assertEqual(instance.list_classes_calls[0]["use_auth"], False)

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_classes_exact_time_filter(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(stdout):
                cli.main(["classes", "--date", "2026-03-13", "--time", "17:15"])
        output = stdout.getvalue()
        self.assertIn("Yoga Calm", output)
        self.assertNotIn("Lift Club", output)

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_book_command_json_output(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(["--json", "--dangerously-approve-token", "book", "208239c232"])
        self.assertEqual(exit_code, 0)
        self.assertIn('"status": "booked"', stdout.getvalue())

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_login_with_save_uses_keyring(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(["login", "--user", "user@example.com", "--passwd", "secret", "--save"])
        self.assertEqual(exit_code, 0)
        store_cls.return_value.save.assert_called_once_with("user@example.com", "secret")
        self.assertEqual(stdout.getvalue().strip(), "success")

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_logout_clears_keyring(self, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(["logout"])
        self.assertEqual(exit_code, 0)
        store_cls.return_value.clear.assert_called_once()
        self.assertEqual(stdout.getvalue().strip(), "success")

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient")
    def test_va_error_exits_with_code_2(self, client_cls, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        instance = client_cls.return_value.__enter__.return_value
        instance.book.side_effect = VAError("broken")
        with self.assertRaises(SystemExit) as exc:
            cli.main(["--dangerously-approve-token", "book", "208239c232"])
        self.assertEqual(exc.exception.code, 2)

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_classes_json_no_null_values(self, store_cls) -> None:
        """JSON output must not contain null values."""
        import json as _json
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(stdout):
                cli.main(["--json", "classes", "--club", "Roma Via Mantova", "--date", "2026-03-13"])
        data = _json.loads(stdout.getvalue())
        for item in data:
            for key, value in item.items():
                self.assertIsNotNone(value, f"{key} is null in JSON output")

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_classes_json_includes_booking_fields(self, store_cls) -> None:
        """JSON output must include booking_id and booking_center."""
        import json as _json
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(stdout):
                cli.main(["--json", "classes", "--date", "2026-03-13"])
        data = _json.loads(stdout.getvalue())
        first = data[0]
        self.assertIn("booking_id", first)
        self.assertIn("booking_center", first)
        self.assertEqual(first["booking_id"], "208239")
        self.assertEqual(first["booking_center"], "232")

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_classes_json_includes_button_label(self, store_cls) -> None:
        """JSON output must include button_label when available."""
        import json as _json
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(stdout):
                cli.main(["--json", "classes", "--date", "2026-03-13"])
        data = _json.loads(stdout.getvalue())
        labels = [item.get("button_label") for item in data if item.get("button_label")]
        self.assertIn("Prenota", labels)
        self.assertIn("15 utenti in attesa", labels)

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient", FakeClient)
    def test_classes_json_snake_case_keys(self, store_cls) -> None:
        """JSON keys must all be lowercase (no camelCase or PascalCase)."""
        import json as _json
        store_cls.return_value.load.return_value = None
        stdout = io.StringIO()
        with patch("builtins.input", return_value="y"):
            with redirect_stdout(stdout):
                cli.main(["--json", "classes", "--date", "2026-03-13"])
        data = _json.loads(stdout.getvalue())
        for item in data:
            for key in item.keys():
                self.assertEqual(
                    key,
                    key.lower(),
                    f"Key '{key}' is not lowercase",
                )

    @patch("va_cli.cli.CredentialStore")
    def test_json_ready_strips_nulls(self, store_cls) -> None:
        """_json_ready must strip None values from dicts."""
        store_cls.return_value.load.return_value = None
        result = cli._json_ready({"a": 1, "b": None, "c": "ok"})
        self.assertNotIn("b", result)
        self.assertEqual(result["a"], 1)

    @patch("va_cli.cli.CredentialStore")
    def test_json_ready_normalizes_camel_case_keys(self, store_cls) -> None:
        """_json_ready must convert CamelCase keys to snake_case."""
        store_cls.return_value.load.return_value = None
        result = cli._json_ready({"IsLoggedIn": True, "DisplayName": "Anita"})
        self.assertIn("is_logged_in", result)
        self.assertIn("display_name", result)
        self.assertNotIn("IsLoggedIn", result)
        self.assertNotIn("DisplayName", result)

    @patch("va_cli.cli.CredentialStore")
    def test_json_ready_recurses_into_lists_and_dicts(self, store_cls) -> None:
        """_json_ready must recurse into nested structures."""
        store_cls.return_value.load.return_value = None
        result = cli._json_ready({"Data": [{"NestedKey": 1, "Skip": None}]})
        self.assertEqual(result["data"][0]["nested_key"], 1)
        self.assertNotIn("skip", result["data"][0])

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient")
    def test_login_notify_f_on_failure_sends_error(self, client_cls, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        instance = client_cls.return_value.__enter__.return_value
        instance.login.side_effect = VAError("bad credentials")
        mock_notifier = MagicMock()
        with patch("va_cli.cli._build_notifier", return_value=mock_notifier):
            with self.assertRaises(SystemExit):
                cli.main(["login", "--user", "x", "--passwd", "y", "--notify", "f"])
        mock_notifier.send.assert_called_once_with("error", "Login failed: bad credentials")

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient")
    def test_classes_notify_f_on_failure_sends_error(self, client_cls, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        instance = client_cls.return_value.__enter__.return_value
        instance.has_saved_session.return_value = True
        instance.list_classes.side_effect = VAError("no classes")
        mock_notifier = MagicMock()
        with patch("va_cli.cli._build_notifier", return_value=mock_notifier):
            with self.assertRaises(SystemExit):
                cli.main(["--dangerously-approve-token", "classes", "--notify", "f", "--date", "2026-03-13"])
        mock_notifier.send.assert_called_once()
        self.assertIn("Failed to list classes", mock_notifier.send.call_args[0][1])

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient")
    def test_book_notify_sf_on_success_sends_success(self, client_cls, store_cls) -> None:
        store_cls.return_value.load.return_value = None
        instance = client_cls.return_value.__enter__.return_value
        instance.book.return_value = {"StatusCode": 200}
        mock_notifier = MagicMock()
        with patch("va_cli.cli._build_notifier", return_value=mock_notifier):
            exit_code = cli.main(["--dangerously-approve-token", "book", "208239c232", "--notify", "sf"])
        self.assertEqual(exit_code, 0)
        mock_notifier.send.assert_called_once_with("success", "Booking confirmed for token 208239c232")

    @patch("va_cli.cli.CredentialStore")
    @patch("va_cli.cli.VirginActiveClient")
    def test_json_ready_dataclass_conversion(self, client_cls, store_cls) -> None:
        """_json_ready must convert dataclasses via asdict."""
        store_cls.return_value.load.return_value = None
        store_cls.return_value.load.return_value = None
        from dataclasses import dataclass

        @dataclass
        class Point:
            x: int
            y: int | None

        result = cli._json_ready(Point(x=10, y=None))
        self.assertEqual(result["x"], 10)
        self.assertNotIn("y", result)


if __name__ == "__main__":
    unittest.main()
