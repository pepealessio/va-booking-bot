from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from getpass import getpass
from typing import Any

import httpx

from .client import VAError, VirginActiveClient
from .config import Config
from .credentials import CredentialStore
from .models import CalendarClass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="va", description="Virgin Active CLI")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--dangerously-approve-token",
        action="store_true",
        help="Skip the interactive approval prompt for authenticated actions.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Log in and cache the current session.")
    login.add_argument("--user")
    login.add_argument("--passwd")
    login.add_argument("--save", action="store_true")

    classes = subparsers.add_parser("classes", help="List classes.")
    _add_filter_args(classes)
    classes.add_argument("--no-auth", action="store_true", help="Force public calendar mode without the saved session.")
    classes.add_argument("--time")
    classes.add_argument("--from-time")
    classes.add_argument("--to-time")

    book = subparsers.add_parser("book", help="Book a class.")
    book.add_argument("token", help="Class token in '<bookingId>c<center>' format.")

    cancel = subparsers.add_parser("cancel", help="Cancel a booked class.")
    cancel.add_argument("token", help="Booking token in '<bookingId>c<center>' format.")

    subparsers.add_parser("logout", help="Clear saved session and stored credentials.")

    debug = subparsers.add_parser("debug", help="Debug helpers.")
    debug_sub = debug.add_subparsers(dest="debug_command", required=True)
    debug_sub.add_parser("whoami", help="Check auth/session state.")
    debug_dates = debug_sub.add_parser("dates", help="List available dates.")
    _add_filter_args(debug_dates, include_date=False)
    for name in ("courses", "trainers", "clubs", "targets"):
        debug_sub.add_parser(name, help=f"List available {name}.")

    return parser


def _add_filter_args(parser: argparse.ArgumentParser, *, include_date: bool = True) -> None:
    parser.add_argument("--course")
    parser.add_argument("--trainer")
    parser.add_argument("--club")
    parser.add_argument("--target")
    if include_date:
        parser.add_argument("--date")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    base_config = Config.from_env()
    credential_store = CredentialStore()
    credentials = _resolve_credentials(args, base_config, credential_store)
    config = base_config.with_credentials(credentials[0], credentials[1])

    try:
        with VirginActiveClient(config, verbose=args.debug) as client:
            result = dispatch(args, client, credential_store)
    except VAError as exc:
        parser.exit(2, f"error: {exc}\n")
    except httpx.HTTPError as exc:
        parser.exit(2, f"network error: {exc}\n")

    render(result, as_json=args.as_json)
    return 0


def dispatch(args: argparse.Namespace, client: VirginActiveClient, credential_store: CredentialStore) -> Any:
    approve = _approval_callback(args)

    if args.command == "login":
        client.login()
        if _should_save_credentials(args):
            credential_store.save(client.config.username or "", client.config.password or "")
        return {"status": "success"}
    if args.command == "classes":
        use_auth = client.has_saved_session() and not args.no_auth
        classes = client.list_classes(_filters_from_args(args), use_auth=use_auth, approve=approve if use_auth else None)
        return [_class_to_output(item) for item in _filter_classes_by_time(classes, args)]
    if args.command == "book":
        return client.book(args.token, approve=approve)
    if args.command == "cancel":
        return client.cancel(args.token, approve=approve)
    if args.command == "logout":
        client.logout()
        credential_store.clear()
        return {"status": "success"}
    if args.command == "debug" and args.debug_command == "whoami":
        return client.whoami(approve=approve)
    if args.command == "debug" and args.debug_command in {"courses", "trainers", "clubs", "targets"}:
        filters = client.get_calendar_filters()
        return [item.to_dict() for item in getattr(filters, args.debug_command)]
    if args.command == "debug" and args.debug_command == "dates":
        return [item.to_dict() for item in client.get_calendar_dates(_filters_from_args(args))]
    raise VAError("Unknown command.")


def render(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_json_ready(payload), indent=2, sort_keys=True))
        return
    if isinstance(payload, list):
        if not payload:
            print("No results.")
            return
        if all(isinstance(item, dict) for item in payload):
            print(_render_table(payload))
            return
        for item in payload:
            print(_format_row(item))
        return
    if isinstance(payload, dict):
        if payload.keys() == {"status"} and payload["status"] == "success":
            print("success")
            return
        for key, value in payload.items():
            print(f"{key}: {value}")
        return
    print(payload)


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command == "classes" and args.time and (args.from_time or args.to_time):
        parser.error("--time cannot be used with --from-time or --to-time")
    for name in ("time", "from_time", "to_time"):
        value = getattr(args, name, None)
        if value:
            _parse_time(value, f"--{name.replace('_', '-')}", parser)


def _resolve_credentials(
    args: argparse.Namespace,
    config: Config,
    credential_store: CredentialStore,
) -> tuple[str | None, str | None]:
    if args.command != "login":
        args.credential_source = "config"
        return config.username, config.password
    if args.user and args.passwd:
        args.credential_source = "cli"
        return args.user, args.passwd
    if config.username and config.password:
        args.credential_source = "env"
        return config.username, config.password
    saved = credential_store.load()
    if saved:
        args.credential_source = "keyring"
        return saved.username, saved.password
    username = args.user or input("Username: ").strip()
    password = args.passwd or getpass("Password: ").strip()
    args.credential_source = "prompt"
    return username or None, password or None


def _should_save_credentials(args: argparse.Namespace) -> bool:
    if args.command != "login":
        return False
    if args.save:
        return True
    if getattr(args, "credential_source", None) == "prompt":
        answer = input("Save credentials to system keyring? [y/N]: ")
        return answer.strip().lower() in {"y", "yes"}
    return False


def _approval_callback(args: argparse.Namespace):
    if getattr(args, "dangerously_approve_token", False):
        return lambda _purpose: True

    def prompt(purpose: str) -> bool:
        answer = input(
            f"This action will use your saved Virgin Active session to {purpose}. Proceed? [y/N]: "
        )
        return answer.strip().lower() in {"y", "yes"}

    return prompt


def _filters_from_args(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "course": getattr(args, "course", None),
        "trainer": getattr(args, "trainer", None),
        "club": getattr(args, "club", None),
        "date": getattr(args, "date", None),
        "target": getattr(args, "target", None),
    }


def _filter_classes_by_time(classes: list[CalendarClass], args: argparse.Namespace) -> list[CalendarClass]:
    if not (args.time or args.from_time or args.to_time):
        return classes
    exact = _parse_time(args.time) if args.time else None
    start_bound = _parse_time(args.from_time) if args.from_time else None
    end_bound = _parse_time(args.to_time) if args.to_time else None
    filtered: list[CalendarClass] = []
    for item in classes:
        if not item.start_time:
            continue
        start_time = _parse_time(item.start_time)
        if exact is not None and start_time != exact:
            continue
        if start_bound is not None and start_time < start_bound:
            continue
        if end_bound is not None and start_time > end_bound:
            continue
        filtered.append(item)
    return filtered


def _parse_time(value: str | None, label: str = "time", parser: argparse.ArgumentParser | None = None):
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        if parser is not None:
            parser.error(f"{label} must be in HH:MM format")
        raise VAError(f"{label} must be in HH:MM format") from exc


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {
            _normalize_key(key): _json_ready(item)
            for key, item in value.items()
            if item is not None
        }
    return value


def _normalize_key(key: str) -> str:
    """Convert CamelCase to snake_case for JSON output."""
    result = []
    for i, ch in enumerate(key):
        if ch.isupper() and i > 0:
            result.append("_")
        result.append(ch.lower())
    return "".join(result)


def _format_row(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    if "label" in item and "value" in item:
        return f"{item['label']} | value={item['value']}"
    preferred_order = [
        "token",
        "title",
        "date",
        "start_time",
        "end_time",
        "club",
        "trainer",
        "room",
        "status",
    ]
    parts = [f"{key}={item[key]}" for key in preferred_order if key in item and item[key] is not None]
    return " | ".join(parts) if parts else str(item)


def _class_to_output(item: CalendarClass) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": item.token,
        "title": item.title,
        "date": item.date,
        "start_time": item.start_time,
        "end_time": item.end_time,
        "club": item.club,
        "trainer": item.trainer,
        "room": item.room,
        "status": item.status,
        "booking_id": item.booking_id,
        "booking_center": item.booking_center,
        "duration": item.duration,
        "queue_length": item.queue_length,
        "available_places": item.available_places,
        "button_label": item.button_label,
    }
    return {k: v for k, v in result.items() if v is not None}


def _render_table(rows: list[dict[str, Any]]) -> str:
    columns = _table_columns(rows)
    normalized_rows = []
    for row in rows:
        normalized_rows.append([_stringify_cell(row.get(column)) for column in columns])
    widths = []
    for index, column in enumerate(columns):
        label = _column_label(column)
        widths.append(max(len(label), *(len(row[index]) for row in normalized_rows)))
    header = " | ".join(_column_label(column).ljust(widths[index]) for index, column in enumerate(columns))
    separator = "-+-".join("-" * widths[index] for index in range(len(columns)))
    lines = [header, separator]
    for row in normalized_rows:
        lines.append(" | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def _table_columns(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "id",
        "title",
        "date",
        "start_time",
        "end_time",
        "club",
        "trainer",
        "room",
        "status",
        "queue_length",
        "available_places",
        "booking_id",
        "booking_center",
        "duration",
        "button_label",
        "label",
        "value",
        "weekday",
        "day_number",
        "selected",
    ]
    seen = {key for row in rows for key, value in row.items() if value is not None}
    columns = [key for key in preferred if key in seen]
    extras = sorted(seen - set(columns))
    return columns + extras


def _column_label(column: str) -> str:
    labels = {
        "id": "ID",
        "title": "Class",
        "date": "Date",
        "start_time": "Start",
        "end_time": "End",
        "club": "Club",
        "trainer": "Trainer",
        "room": "Room",
        "status": "Status",
        "queue_length": "Queue",
        "available_places": "Places",
        "booking_id": "Booking ID",
        "booking_center": "Center",
        "duration": "Duration",
        "button_label": "Button",
        "label": "Name",
        "value": "Value",
        "weekday": "Weekday",
        "day_number": "Day",
        "selected": "Selected",
        "overbooked": "Overbooked",
        "not_yet_open": "Not Open",
    }
    return labels.get(column, column.replace("_", " ").title())


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
