from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from getpass import getpass
from typing import Any

import httpx

from .automate import cmd_add, cmd_list, cmd_remove, worker_book
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
    classes.add_argument("--day", type=int, help="Day of week: 0=Mon … 6=Sun (computes next date)")

    book = subparsers.add_parser("book", help="Book a class.")
    book.add_argument("token", nargs="?", default=None, help="Class token in '<bookingId>c<center>' format.")
    book.add_argument("--retry", type=int, default=1, help="Max retry attempts (default 1, no retry)")
    book.add_argument("--retry-interval", type=int, default=5, help="Seconds between retries (default 5)")

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

    auto = subparsers.add_parser("automate", help="Recurring booking automation helpers.")
    auto_sub = auto.add_subparsers(dest="automate_command", required=True)

    add_parser = auto_sub.add_parser("add", help="Interactively select a class and create cron entries.")
    add_parser.add_argument("--install", action="store_true", help="Automatically install cron entries into crontab.")
    add_parser.add_argument("--raw", action="store_true", help="Print only cron lines (no commentary), suitable for piping to crontab.")

    list_parser = auto_sub.add_parser("list", help="List recurring booking entries from crontab.")
    list_parser.add_argument("--json", action="store_true", dest="list_as_json")
    list_parser.add_argument("--raw", action="store_true", help="Print only cron lines from all va-automate entries, suitable for piping.")

    remove_parser = auto_sub.add_parser("remove", help="Remove a booking entry from crontab.")
    remove_parser.add_argument("entry_id", nargs="?", default=None, help="Entry ID to remove (skips interactive prompt).")

    return parser


def _add_filter_args(parser: argparse.ArgumentParser, *, include_date: bool = True) -> None:
    parser.add_argument("--course")
    parser.add_argument("--trainer")
    parser.add_argument("--club")
    parser.add_argument("--target")
    if include_date:
        parser.add_argument("--date")


def main(argv: list[str] | None = None) -> int:
    try:
        return _run_main(argv)
    except KeyboardInterrupt:
        sys.exit(130)
    except EOFError:
        sys.exit(130)

def _run_main(argv: list[str] | None = None) -> int:
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
        if args.save or getattr(args, "credential_source", None) == "prompt":
            if not args.save:
                answer = input("Save credentials to system keyring? [y/N]: ")
                if answer.strip().lower() not in {"y", "yes"}:
                    return {"status": "success"}
            credential_store.save(client.config.username or "", client.config.password or "")
        return {"status": "success"}
    if args.command == "classes":
        use_auth = client.has_saved_session() and not args.no_auth
        date = getattr(args, "date", None)
        if date is None and getattr(args, "day", None) is not None:
            date = _next_weekday_date(args.day)
        flt: dict[str, str | None] = {
            "course": getattr(args, "course", None),
            "trainer": getattr(args, "trainer", None),
            "club": getattr(args, "club", None),
            "date": date,
            "target": getattr(args, "target", None),
        }
        classes = client.list_classes(flt, use_auth=use_auth, approve=approve if use_auth else None)
        result = []
        for item in _filter_classes_by_time(classes, args):
            out: dict[str, Any] = {
                "id": item.token, "title": item.title, "date": item.date,
                "start_time": item.start_time, "end_time": item.end_time,
                "club": item.club, "trainer": item.trainer, "room": item.room,
                "status": item.status, "booking_id": item.booking_id,
                "booking_center": item.booking_center, "duration": item.duration,
                "queue_length": item.queue_length, "available_places": item.available_places,
                "button_label": item.button_label,
            }
            result.append({k: v for k, v in out.items() if v is not None})
        return result
    if args.command == "book":
        if not args.token:
            raise VAError("book requires a token")
        max_retries = getattr(args, "retry", 1)
        if max_retries > 1:
            return worker_book(
                client=client,
                token=args.token,
                approve=approve,
                max_retries=max_retries,
                retry_interval=getattr(args, "retry_interval", 5),
            )
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
        flt: dict[str, str | None] = {
            "course": getattr(args, "course", None),
            "trainer": getattr(args, "trainer", None),
            "club": getattr(args, "club", None),
            "date": getattr(args, "date", None),
            "target": getattr(args, "target", None),
        }
        return [item.to_dict() for item in client.get_calendar_dates(flt)]
    if args.command == "automate":
        cmd = getattr(args, "automate_command", None)
        if cmd == "add":
            return cmd_add(client, install=getattr(args, "install", False), raw=getattr(args, "raw", False))
        if cmd == "list":
            result = cmd_list()
            if getattr(args, "list_as_json", False) or args.as_json:
                return result
            if getattr(args, "raw", False):
                return result.get("cron_lines", "")
            if result.get("entries"):
                return result["entries"]
            print("No booking entries in crontab.")
            return {"status": "empty"}
        if cmd == "remove":
            return cmd_remove(entry_id=getattr(args, "entry_id", None))
        raise VAError("Unknown automate subcommand.")
    raise VAError("Unknown command.")


def render(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_json_ready(payload), indent=2, sort_keys=True))
        return
    if isinstance(payload, list):
        if not payload:
            print("No results.")
            return
        print(_render_table(payload))
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
    if args.command == "classes" and getattr(args, "day", None) is not None:
        dow = args.day
        if not 0 <= dow <= 6:
            parser.error("--day must be between 0 (Mon) and 6 (Sun)")


def _next_weekday_date(day_of_week: int) -> str:
    """Return the next calendar date matching day_of_week (0=Mon, 6=Sun)."""
    today = datetime.now(UTC).date()
    for d in range(14):
        dt = today + timedelta(days=d)
        if dt.weekday() == day_of_week:
            return dt.strftime("%Y-%m-%d")
    raise VAError(f"Could not compute date for day_of_week={day_of_week}")


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


def _approval_callback(args: argparse.Namespace):
    if getattr(args, "dangerously_approve_token", False):
        return lambda _purpose: True

    def prompt(purpose: str) -> bool:
        answer = input(
            f"This action will use your saved Virgin Active session to {purpose}. Proceed? [y/N]: "
        )
        return answer.strip().lower() in {"y", "yes"}

    return prompt


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
        result: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            snake = []
            for i, ch in enumerate(key):
                if ch.isupper() and i > 0:
                    snake.append("_")
                snake.append(ch.lower())
            result["".join(snake)] = _json_ready(item)
        return result
    return value


LABELS = {
    "id": "ID", "title": "Class", "date": "Date", "start_time": "Start",
    "end_time": "End", "club": "Club", "trainer": "Trainer", "room": "Room",
    "status": "Status", "queue_length": "Queue", "available_places": "Places",
    "booking_id": "Booking ID", "booking_center": "Center", "duration": "Duration",
    "button_label": "Button", "label": "Name", "value": "Value",
    "weekday": "Weekday", "day_number": "Day", "selected": "Selected",
}

COLUMN_ORDER = [
    "id", "club", "course", "day", "time", "title", "date", "start_time",
    "end_time", "trainer", "room", "status", "queue_length", "available_places",
    "booking_id", "booking_center", "duration", "button_label",
    "label", "value", "weekday", "day_number", "selected",
]


def _render_table(rows: list[dict[str, Any]]) -> str:
    seen = {key for row in rows for key, value in row.items() if value is not None}
    columns = [key for key in COLUMN_ORDER if key in seen]
    columns.extend(sorted(seen - set(columns)))

    normalized = []
    for row in rows:
        normalized.append([])
        for column in columns:
            val = row.get(column)
            if val is None:
                normalized[-1].append("")
            elif isinstance(val, bool):
                normalized[-1].append("yes" if val else "no")
            elif isinstance(val, (list, dict)):
                normalized[-1].append(json.dumps(val, ensure_ascii=False, sort_keys=True))
            else:
                normalized[-1].append(str(val))

    widths = []
    for index, column in enumerate(columns):
        label = LABELS.get(column, column.replace("_", " ").title())
        widths.append(max(len(label), *(len(row[index]) for row in normalized)))

    header = " | ".join(LABELS.get(c, c.replace("_", " ").title()).ljust(widths[i]) for i, c in enumerate(columns))
    separator = "-+-".join("-" * w for w in widths)
    lines = [header, separator]
    for row in normalized:
        lines.append(" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)
