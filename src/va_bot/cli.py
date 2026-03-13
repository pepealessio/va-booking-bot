from __future__ import annotations

import argparse
import json
import os
import sys
import termios
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import tty

import httpx

from va_cli.client import VAError, VirginActiveClient
from va_cli.config import Config
from va_cli.credentials import CredentialStore
from va_cli.models import CalendarClass, CalendarDateOption

from .config import BotConfig, BotConfigError, BotRule, dump_config, load_config, write_config
from .runtime import BotService
from .state import BotStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="va-bot", description="Virgin Active booking bot")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--debug", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Interactively create a bot config.")
    init.add_argument("--config", type=Path, default=Path("va-bot.yml"))

    validate = subparsers.add_parser("validate", help="Validate a bot config against live classes.")
    validate.add_argument("--config", type=Path, default=Path("va-bot.yml"))

    plan = subparsers.add_parser("plan", help="Show the next planned booking windows.")
    plan.add_argument("--config", type=Path, default=Path("va-bot.yml"))

    run = subparsers.add_parser("run", help="Run the booking bot continuously.")
    run.add_argument("--config", type=Path, default=Path("va-bot.yml"))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app_config = _resolve_app_config()

    try:
        if args.command == "init":
            payload = init_config(args.config, app_config, debug=args.debug)
        else:
            bot_config = load_config(args.config)
            state_store = BotStateStore(app_config.state_dir / "bot-state.json")
            with VirginActiveClient(app_config, verbose=args.debug) as client:
                service = BotService(
                    client,
                    bot_config,
                    app_config=app_config,
                    credential_store=CredentialStore(),
                    state_store=state_store,
                    log_fn=print if args.debug else None,
                )
                if args.command == "validate":
                    payload = [item.to_dict() for item in service.validate()]
                elif args.command == "plan":
                    payload = [_plan_to_output(item) for item in service.plan()]
                elif args.command == "run":
                    service.run_forever()
                    payload = {"status": "stopped"}
                else:
                    raise BotConfigError("Unknown command.")
    except (VAError, BotConfigError) as exc:
        parser.exit(2, f"error: {exc}\n")
    except httpx.HTTPError as exc:
        parser.exit(2, f"network error: {exc}\n")

    render(payload, as_json=args.as_json)
    return 0


def init_config(path: Path, app_config: Config, *, debug: bool) -> dict[str, Any]:
    existing = _load_existing_config(path)
    with VirginActiveClient(app_config, verbose=debug) as client:
        timezone = _prompt_default("Timezone", existing.timezone)
        preflight_minutes = int(_prompt_default("Preflight minutes before booking opens", str(existing.preflight_minutes)))
        retry_window_seconds = int(_prompt_default("Retry window in seconds", str(existing.retry_window_seconds)))
        retry_interval_seconds = float(_prompt_default("Retry interval in seconds", str(existing.retry_interval_seconds)))
        rules = [BotRule(**rule.to_dict()) for rule in existing.rules]
        while True:
            action = _choose_init_action(bool(rules))
            if action == "finish":
                break
            if action == "delete":
                target_rule = _select_option("Select rule to delete", rules, formatter=_format_rule_summary)
                rules = [rule for rule in rules if rule.name != target_rule.name]
                print(f"Deleted rule `{target_rule.name}`.")
                continue
            existing_rule = None
            if action == "edit":
                existing_rule = _select_option("Select rule to edit", rules, formatter=_format_rule_summary)
                rules = [rule for rule in rules if rule.name != existing_rule.name]
            rule = _build_rule_from_live_selection(
                client,
                app_config,
                timezone,
                preflight_minutes,
                retry_window_seconds,
                retry_interval_seconds,
                existing_rule,
            )
            rules.append(rule)
            print(f"Stored rule `{rule.name}`.")
        if not rules:
            raise BotConfigError("Config must contain at least one rule.")
    config = BotConfig(
        timezone=timezone,
        preflight_minutes=preflight_minutes,
        retry_window_seconds=retry_window_seconds,
        retry_interval_seconds=retry_interval_seconds,
        rules=rules,
    )
    write_config(path, config)
    return {"status": "written", "path": str(path), "rules": len(rules), "config": dump_config(config)}


def _build_rule_from_live_selection(
    client: VirginActiveClient,
    app_config: Config,
    timezone: str,
    preflight_minutes: int,
    retry_window_seconds: int,
    retry_interval_seconds: float,
    existing_rule: BotRule | None,
) -> BotRule:
    filters = client.get_calendar_filters()
    clubs = [item.label for item in filters.clubs if item.label and not item.label.casefold().startswith("scegli")]
    club = _select_option("Select club", clubs, initial_index=_index_of(clubs, existing_rule.club if existing_rule else None))
    weekday_options = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    weekday = _select_option(
        "Select recurring weekday",
        weekday_options,
        formatter=lambda item: item.title(),
        initial_index=_index_of(weekday_options, existing_rule.weekday if existing_rule else None),
    )
    dates = client.get_calendar_dates({"club": club, "course": None, "trainer": None, "target": None, "date": None})
    if not dates:
        raise BotConfigError(f"No visible dates found for club `{club}`.")
    weekday_dates = [item for item in dates if _weekday_name(item.date) == weekday]
    if not weekday_dates:
        raise BotConfigError(f"No visible dates found for club `{club}` on `{weekday}`.")
    date_choice = _select_option(
        "Select a sample visible date for that weekday",
        weekday_dates,
        formatter=lambda item: f"{item.date} | {item.weekday} {item.day_number}",
    )
    use_auth = client.has_saved_session()
    classes = client.list_classes(
        {"club": club, "course": None, "trainer": None, "target": None, "date": date_choice.date},
        use_auth=use_auth,
        approve=(lambda _purpose: True) if use_auth else None,
    )
    classes = [item for item in classes if item.start_time and item.title and item.club == club]
    if not classes:
        raise BotConfigError(f"No classes found for club `{club}` on {date_choice.date}.")
    class_choice = _select_option(
        "Select the class occurrence to automate",
        classes,
        formatter=_format_class_choice,
        initial_index=_match_class_index(classes, existing_rule),
        header=_class_choice_header(),
    )
    suggested_name = _suggest_rule_name(class_choice, club, weekday)
    default_name = existing_rule.name if existing_rule else suggested_name
    name = _prompt_default("Rule name", default_name)
    trainer = None
    if class_choice.trainer:
        lock_default = "y" if existing_rule and existing_rule.trainer == class_choice.trainer else "n"
        answer = input(f"Lock trainer `{class_choice.trainer}` in the rule? [y/N] [{lock_default}]: ").strip().lower()
        answer = answer or lock_default
        if answer in {"y", "yes"}:
            trainer = class_choice.trainer
    target = existing_rule.target if existing_rule else None
    if filters.targets:
        target_options = [item.label for item in filters.targets]
        target = _prompt_optional_choice(
            "Target filter",
            target_options,
            prompt_text="Target filter (optional, press Enter to keep current/skip)",
            default=target,
        )
    rule = BotRule(
        name=name,
        club=club,
        course=class_choice.title,
        weekday=weekday,
        time=class_choice.start_time,
        trainer=trainer,
        target=target,
    )
    bot_config = BotConfig(
        timezone=timezone,
        preflight_minutes=preflight_minutes,
        retry_window_seconds=retry_window_seconds,
        retry_interval_seconds=retry_interval_seconds,
        rules=[rule],
    )
    service = BotService(
        client,
        bot_config,
        app_config=app_config,
        credential_store=CredentialStore(),
        state_store=BotStateStore(app_config.state_dir / "bot-state.json"),
    )
    result = service.validate()[0]
    if not result.ok:
        raise BotConfigError(f"Validation failed for `{rule.name}`: {result.message}")
    print(
        f"Validated rule `{rule.name}` for {result.date}: "
        f"{rule.club} | {rule.course} | {rule.weekday} {rule.time} | token {result.token}."
    )
    return rule


def _load_existing_config(path: Path) -> BotConfig:
    if path.exists():
        return load_config(path)
    return BotConfig()


def _choose_init_action(has_rules: bool) -> str:
    if not has_rules:
        print("No existing rules. Create the first rule.")
        return "create"
    return _select_option(
        "Choose action",
        ["create", "edit", "delete", "finish"],
        formatter=lambda item: {
            "create": "Create new rule",
            "edit": "Edit existing rule",
            "delete": "Delete existing rule",
            "finish": "Finish and save config",
        }[item],
    )


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
            print(item)
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
        return
    print(payload)


def _resolve_app_config() -> Config:
    base = Config.from_env()
    if base.username and base.password:
        return base
    saved = CredentialStore().load()
    if saved:
        return base.with_credentials(saved.username, saved.password)
    return base


def _prompt(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value:
        raise BotConfigError(f"{label} is required.")
    return value


def _prompt_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _prompt_choice(label: str, options: list[str], *, prompt_text: str | None = None) -> str:
    value = input(f"{prompt_text or f'{label}'}: ").strip()
    if not value:
        raise BotConfigError(f"{label} is required.")
    for option in options:
        if option.casefold() == value.casefold():
            return option
    raise BotConfigError(f"{label} must match one of the live options exactly.")


def _prompt_optional_choice(
    label: str,
    options: list[str],
    *,
    prompt_text: str | None = None,
    default: str | None = None,
) -> str | None:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt_text or f'{label} (optional)'}{suffix}: ").strip()
    if not value:
        return default
    for option in options:
        if option.casefold() == value.casefold():
            return option
    raise BotConfigError(f"{label} must match one of the live options exactly.")


def _select_option(label: str, options: list[Any], *, formatter=None, initial_index: int = 0, header: str | None = None):
    if not options:
        raise BotConfigError(f"{label} has no available options.")
    formatter = formatter or (lambda item: str(item))
    if _supports_tty_selector():
        return _tty_select_option(label, options, formatter=formatter, initial_index=initial_index, header=header)
    print(label)
    if header:
        print(header)
    for index, item in enumerate(options, start=1):
        print(f"{index}. {formatter(item)}")
    raw = input("Choose number: ").strip()
    if not raw.isdigit():
        raise BotConfigError("Selection must be a number.")
    selected = int(raw)
    if selected < 1 or selected > len(options):
        raise BotConfigError("Selection out of range.")
    return options[selected - 1]


def _supports_tty_selector() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.getenv("TERM", "dumb") != "dumb"


def _tty_select_option(label: str, options: list[Any], *, formatter, initial_index: int, header: str | None):
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    selected = max(0, min(initial_index, len(options) - 1))
    rendered_lines = 0
    try:
        tty.setraw(fd)
        while True:
            if rendered_lines:
                sys.stdout.write(f"\x1b[{rendered_lines}F\r")
            lines = [label, "Use Up/Down and Enter."]
            if header:
                lines.append(header)
            for index, item in enumerate(options):
                prefix = "> " if index == selected else "  "
                lines.append(f"{prefix}{formatter(item)}")
            rendered_lines = len(lines)
            sys.stdout.write("\x1b[J")
            sys.stdout.write("\r\n".join(lines))
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            key = _read_key()
            if key in {"\r", "\n"}:
                sys.stdout.write(f"\x1b[{rendered_lines}F\r\x1b[J")
                sys.stdout.flush()
                return options[selected]
            if key in {"\x1b[A", "k"}:
                selected = (selected - 1) % len(options)
            elif key in {"\x1b[B", "j"}:
                selected = (selected + 1) % len(options)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _read_key() -> str:
    first = sys.stdin.read(1)
    if first != "\x1b":
        return first
    second = sys.stdin.read(1)
    if second != "[":
        return first + second
    third = sys.stdin.read(1)
    return first + second + third


def _format_class_choice(item: CalendarClass) -> str:
    return "  ".join(
        [
            f"{(item.start_time or '--:--'):<5}",
            f"{_clip(item.title, 28):<28}",
            f"{_clip(item.trainer or '', 18):<18}",
            f"{_clip(item.room or '', 16):<16}",
            f"{(item.status or ''):<11}",
        ]
    ).rstrip()


def _class_choice_header() -> str:
    return "  ".join(
        [
            f"{'Start':<5}",
            f"{'Class':<28}",
            f"{'Trainer':<18}",
            f"{'Room':<16}",
            f"{'Status':<11}",
        ]
    ).rstrip()


def _weekday_name(date_text: str) -> str:
    return datetime.strptime(date_text, "%Y-%m-%d").strftime("%A").casefold()


def _slugify(value: str) -> str:
    chars = []
    last_dash = False
    for char in value.casefold():
        if char.isalnum():
            chars.append(char)
            last_dash = False
            continue
        if not last_dash:
            chars.append("-")
            last_dash = True
    return "".join(chars).strip("-")


def _suggest_rule_name(item: CalendarClass, club: str, weekday: str) -> str:
    weekday = weekday[:3]
    parts = [_slugify(item.title), _slugify(club), weekday, (item.start_time or "").replace(":", "")]
    return "-".join(part for part in parts if part)


def _clip(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def _format_rule_summary(rule: BotRule) -> str:
    parts = [rule.name, rule.club, rule.course, f"{rule.weekday} {rule.time}"]
    if rule.trainer:
        parts.append(rule.trainer)
    return " | ".join(parts)


def _index_of(options: list[str], value: str | None) -> int:
    if not value:
        return 0
    for index, option in enumerate(options):
        if option.casefold() == value.casefold():
            return index
    return 0


def _match_class_index(classes: list[CalendarClass], rule: BotRule | None) -> int:
    if rule is None:
        return 0
    for index, item in enumerate(classes):
        if item.title != rule.course:
            continue
        if item.start_time != rule.time:
            continue
        if rule.trainer and item.trainer != rule.trainer:
            continue
        return index
    return 0


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


def _render_table(rows: list[dict[str, Any]]) -> str:
    columns = _table_columns(rows)
    normalized = [[_stringify_cell(row.get(column)) for column in columns] for row in rows]
    widths = [max(len(column), *(len(row[index]) for row in normalized)) for index, column in enumerate(columns)]
    header = " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    separator = "-+-".join("-" * width for width in widths)
    lines = [header, separator]
    for row in normalized:
        lines.append(" | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def _table_columns(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "rule_name",
        "class_start",
        "booking_opens",
        "preflight_at",
        "date",
        "count",
        "ok",
        "token",
        "message",
    ]
    seen = {key for row in rows for key, value in row.items() if value is not None}
    return [key for key in preferred if key in seen] + sorted(seen - set(preferred))


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _plan_to_output(item: Any) -> dict[str, Any]:
    return {
        "rule_name": item.rule_name,
        "class_start": item.class_start.isoformat(),
        "booking_opens": item.booking_opens.isoformat(),
        "preflight_at": item.preflight_at.isoformat(),
    }
