from __future__ import annotations

import logging
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import questionary

from .client import VAError, VirginActiveClient
from .config import Config
from .notifier import from_config

# ── Day name helpers ─────────────────────────────────────────────


DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Python DOW → cron DOW (Mon=0->1, Tue=1->2, … Sun=6->0)
def _python_dow_to_cron(py_dow: int) -> int:
    return (py_dow + 1) % 7


# ── Cron computation ──────────────────────────────────────────────


def compute_cron_times(
    py_dow: int,
    time_str: str,
    booking_open_hours: int = 48,
    login_offset_minutes: int = 5,
) -> dict[str, Any]:
    """Return book and login cron minute/hour/DOW for a recurring class.

    Each class gets two cron entries:
      - login: login_offset_minutes before book (5 min)
      - book:  exactly booking_open_hours before the class (48 h)

    Computed as pure minute arithmetic — no calendar dates involved.
    """
    h, m = map(int, time_str.split(":"))
    class_minute = h * 60 + m
    day_minutes = 24 * 60

    result: dict[str, Any] = {}
    for label, minutes_back in (
        ("book", booking_open_hours * 60),
        ("login", booking_open_hours * 60 + login_offset_minutes),
    ):
        diff = class_minute - (minutes_back % day_minutes)
        extra_days = minutes_back // day_minutes
        if diff < 0:
            diff += day_minutes
            extra_days += 1
        result[f"{label}_hour"] = diff // 60
        result[f"{label}_minute"] = diff % 60
        result[f"{label}_dow"] = _python_dow_to_cron((py_dow - extra_days) % 7)

    return result


VA_MARKER_PREFIX = "# va-automate:"


def _generate_id() -> str:
    """Generate a short, unique ID for a cron entry set."""
    import random
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choices(chars, k=8))


def build_cron_entry(
    club: str,
    day_of_week: int,
    time_str: str,
    course: str | None = None,
    max_retries: int = 10,
    retry_interval: int = 60,
    va_bin: str = "va",
    entry_id: str | None = None,
) -> list[str]:
    """Return two cron lines (login + book) as strings for copy-paste."""
    if entry_id is None:
        entry_id = _generate_id()
    cron = compute_cron_times(day_of_week, time_str)
    course_part = f" --course '{course}'" if course else ""
    marker = f"{VA_MARKER_PREFIX}{entry_id}"
    login_line = "%02d %02d * * %d %s login %s" % (
        cron["login_minute"], cron["login_hour"], cron["login_dow"], va_bin, marker,
    )
    book_line = "%02d %02d * * %d %s book --recurring --club '%s'%s --day %d --time '%s' --retry %d --retry-interval %d %s" % (
        cron["book_minute"], cron["book_hour"], cron["book_dow"], va_bin,
        club, course_part, day_of_week, time_str, max_retries, retry_interval, marker,
    )
    comment = "# %s — %s — %s %s %s" % (club, course or "any class", DOW_NAMES[day_of_week], time_str, marker)
    return [comment, login_line, book_line]


# ── Logging helper ─────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def _setup_logger(state_dir: Path) -> logging.Logger:
    logger = logging.getLogger("va-automate")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    log_path = state_dir / "automate.log"
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    return logger


# ── Worker: book --recurring ──────────────────────────────────────


def worker_book_recurring(
    *,
    state_dir: Path,
    club: str | None,
    course: str | None,
    day_of_week: int | None,
    time_str: str | None,
    max_retries: int = 10,
    retry_interval: int = 60,
) -> dict[str, Any]:
    """Self-contained recurring booking worker.

    Resolves the class at runtime by filters, books it, and retries on failure.
    """
    if not club or day_of_week is None or not time_str:
        raise VAError("--recurring requires --club, --day, and --time")

    logger = _setup_logger(state_dir)
    class_desc = f"{club}/{course or 'any'} @ {time_str} ({DOW_NAMES[day_of_week]})"
    logger.info("cron-book: starting for %s", class_desc)

    # Load notifier config for Telegram
    import os
    notify_cfg = {
        "provider": os.environ.get("VA_NOTIFY_PROVIDER", "telegram"),
        "token": os.environ.get("VA_NOTIFY_TOKEN"),
        "chat_id": os.environ.get("VA_NOTIFY_CHAT_ID"),
    }
    if not notify_cfg["token"] or not notify_cfg["chat_id"]:
        notify_cfg = None
    notifier = from_config(notify_cfg)

    config = Config.from_env()

    for attempt in range(1, max_retries + 1):
        logger.info("attempt %d/%d", attempt, max_retries)
        try:
            result = _do_book_attempt(config, club, course, day_of_week, time_str)
            status_code = result.get("StatusCode", result.get("statusCode"))
            if status_code == 200 or (isinstance(status_code, str) and status_code == "200"):
                logger.info("SUCCESS on attempt %d", attempt)
                notifier.send(
                    "success",
                    f"Booking confirmed: **{class_desc}** (attempt {attempt})",
                )
                return {"status": "success", "class": class_desc, "attempts": attempt}
            else:
                logger.warning("non-200 status: %s", result)
                time.sleep(retry_interval)

        except VAError as e:
            logger.error("VAError on attempt %d: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(retry_interval)

        except Exception as e:
            logger.error("error on attempt %d: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(retry_interval)

    logger.error("FAILED after %d attempts for %s", max_retries, class_desc)
    notifier.send(
        "error",
        f"Booking failed: **{class_desc}** after {max_retries} attempts",
    )
    raise VAError(f"Failed to book {class_desc} after {max_retries} attempts")


def _do_book_attempt(
    config: Config,
    club: str,
    course: str | None,
    day_of_week: int,
    time_str: str,
) -> dict[str, Any]:
    """Find and book one class. Returns the booking result dict or raises VAError."""
    # The cron runs 48h before the class. The class is on today + 2 days.
    # But we also need the target date to match the correct day_of_week.
    # So find the next date where weekday matches day_of_week, that is >= today+2.
    today = datetime.now(UTC).date()
    target_date = None
    for d in range(2, 14):
        dt = today + timedelta(days=d)
        if dt.weekday() == day_of_week:
            target_date = dt.strftime("%Y-%m-%d")
            break
    if target_date is None:
        raise VAError("Cannot find target date for day_of_week={}".format(day_of_week))

    filters: dict[str, str | None] = {
        "club": club,
        "course": course,
        "date": target_date,
        "trainer": None,
        "target": None,
    }

    with VirginActiveClient(config, verbose=False) as client:
        if not client.has_saved_session():
            try:
                client.login()
            except VAError:
                raise VAError("No saved session and cannot log in")

        try:
            found = client.list_classes(filters, use_auth=True, approve=lambda _: True)
        except VAError:
            client.login()
            found = client.list_classes(filters, use_auth=True, approve=lambda _: True)

        matched = [c for c in found if c.start_time == time_str]
        if not matched and course:
            matched = [
                c for c in found
                if course.lower() in (c.title or "").lower()
            ]
        if not matched:
            raise VAError(
                f"Cannot find class matching time={time_str}, date={target_date}. "
                f"Found {len(found)} classes for that date."
            )

        target = matched[0]
        logger = logging.getLogger("va-automate")
        logger.info("found class %s (%s)", target.token, target.title)
        return client.book(target.token, approve=lambda _: True)


# ── Interactive add flow → print cron lines ────────────────────────


def interactive_add(
    client: VirginActiveClient,
    *,
    install: bool = False,
    raw: bool = False,
) -> dict[str, Any]:
    """Walk through selection and print/install cron entries."""

    clubs = client.get_calendar_filters().clubs
    if not clubs:
        raise VAError("No clubs found.")
    club_label = questionary.select(
        "Select club:",
        choices=[c.label for c in clubs],
    ).ask()
    if not club_label:
        raise VAError("Aborted: no club selected")

    courses = client.get_calendar_filters().courses
    course_label = None
    if courses:
        choices = ["(any)"] + [c.label for c in courses]
        course_label = questionary.select(
            "Select course:",
            choices=choices,
        ).ask()
        if not course_label:
            raise VAError("Aborted")
        if course_label == "(any)":
            course_label = None

    day_sel = questionary.select(
        "Select day of week:",
        choices=DOW_NAMES,
    ).ask()
    if not day_sel:
        raise VAError("Aborted")
    day_of_week = DOW_NAMES.index(day_sel)

    today = datetime.now(UTC).date()
    candidate_dates: list[str] = []
    for d in range(1, 14):
        dt = today + timedelta(days=d)
        if dt.weekday() == day_of_week:
            candidate_dates.append(dt.strftime("%Y-%m-%d"))
    if not candidate_dates:
        raise VAError("No matching date in next 14 days.")

    filters: dict[str, str | None] = {
        "club": club_label,
        "course": course_label,
        "date": candidate_dates[0],
        "trainer": None,
        "target": None,
    }
    try:
        classes = client.list_classes(filters, use_auth=True, approve=lambda _: True)
    except VAError:
        classes = client.list_classes(filters, use_auth=False)

    if not classes:
        raise VAError(
            f"No classes found for {club_label} on {candidate_dates[0]}. "
            "Try different filters."
        )

    options = []
    for i, c in enumerate(classes):
        lbl = f"{c.start_time or '?'} | {c.title}"
        if c.trainer:
            lbl += f" ({c.trainer})"
        if c.room:
            lbl += f" — {c.room}"
        options.append({"name": lbl, "value": i})

    sel_idx = questionary.select(
        "Select a class:",
        choices=options,
    ).ask()
    if sel_idx is None:
        raise VAError("Aborted")

    selected = classes[sel_idx]
    if not selected.start_time:
        raise VAError("Selected class has no valid time")

    time_str = selected.start_time

    if not raw:
        preview = (
            f"  Club:    {club_label}\n"
            f"  Course:  {course_label or '(any)'}\n"
            f"  Day:     {DOW_NAMES[day_of_week]}\n"
            f"  Time:    {time_str}\n"
            f"  Trainer: {selected.trainer or '-'}\n"
            f"  Room:    {selected.room or '-'}"
        )
        print(preview)

    if raw:
        max_retries_val = 10
        retry_interval_val = 60
        do_install = False
    elif install:
        max_retries_val = 10
        retry_interval_val = 60
        do_install = True
    else:
        max_retries_text = questionary.text(
            "Max retries (default 10):",
        ).ask()
        if max_retries_text is None:
            raise VAError("Aborted by user")
        max_retries_val = int(max_retries_text) if max_retries_text else 10

        retry_interval_text = questionary.text(
            "Retry interval in seconds (default 60):",
        ).ask()
        if retry_interval_text is None:
            raise VAError("Aborted by user")
        retry_interval_val = int(retry_interval_text) if retry_interval_text else 60

        do_install = questionary.confirm(
            "Install into crontab?",
            default=True,
        ).ask()
        if do_install is None:
            raise VAError("Aborted by user")

    entry_id = _generate_id()
    lines = build_cron_entry(
        club=club_label,
        course=course_label,
        day_of_week=day_of_week,
        time_str=time_str,
        max_retries=max_retries_val,
        retry_interval=retry_interval_val,
        entry_id=entry_id,
    )

    cron_content = "\n".join(lines) + "\n"

    if raw:
        print(cron_content, end="")
    elif not do_install:
        print()
        print("Copy these lines into your crontab (`crontab -e`):")
        print()
        for line in lines:
            print(f"  {line}")
        print()
        print(f"  Listing entries:    va automate list")
        print(f"  Remove this entry:  va automate remove {entry_id}")
    else:
        existing = _read_crontab()
        new_content = (existing + "\n" + cron_content) if existing else cron_content
        _write_crontab(new_content)
        print()
        print("Cron entries installed successfully!")
        print()
        for line in lines:
            print(f"  {line}")
        print()
        print(f"  Listing entries:    va automate list")
        print(f"  Remove this entry:  va automate remove {entry_id}")

    return {
        "status": "success",
        "lines": lines,
        "entry_id": entry_id,
        "installed": install,
    }


# ── CLI entry points ──────────────────────────────────────────────


def cmd_add(client: VirginActiveClient, *, install: bool = False, raw: bool = False) -> Any:
    if not client.has_saved_session():
        if not raw:
            print("No saved session. Logging in first...")
        try:
            client.login()
        except VAError as e:
            return {"status": "error", "error": str(e)}
    return interactive_add(client, install=install, raw=raw)


# ── List / remove from crontab ────────────────────────────────────


def _read_crontab() -> str:
    """Return the current crontab content as a string."""
    import subprocess
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _write_crontab(content: str) -> None:
    """Write content to the user's crontab."""
    import subprocess
    proc = subprocess.run(
        ["crontab", "-"], input=content, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise VAError(f"Failed to write crontab: {proc.stderr.strip()}")


def _join_quoted(parts: list[str], idx: int) -> str:
    """Join a sequence of tokens that were split by shell quoting."""
    if idx >= len(parts):
        return ""
    first = parts[idx]
    if first.startswith("'"):
        if first.endswith("'"):
            return first
        tokens = [first]
        for j in range(idx + 1, len(parts)):
            tokens.append(parts[j])
            if tokens[-1].endswith("'"):
                break
        return " ".join(tokens)
    elif first.startswith('"'):
        if first.endswith('"'):
            return first
        tokens = [first]
        for j in range(idx + 1, len(parts)):
            tokens.append(parts[j])
            if tokens[-1].endswith('"'):
                break
        return " ".join(tokens)
    return first


def _cronline_to_dict(line: str) -> dict[str, str] | None:
    """Parse a va-automate book cron line into a dict with id, club, course, day, time."""
    if not line.strip() or line.startswith("#"):
        return None
    if "--recurring" not in line:
        return None
    parts = line.split()
    marker_token = None
    for part in parts:
        if "va-automate:" in part and VA_MARKER_PREFIX in line:
            marker_token = part
            break
    if not marker_token:
        return None

    entry_id = marker_token.split("va-automate:")[1]
    club = course = day_str = time_val = ""

    for i, part in enumerate(parts):
        if part == "--club" and i + 1 < len(parts):
            club = _join_quoted(parts, i + 1).strip("'\"")
        elif part == "--course" and i + 1 < len(parts):
            course = _join_quoted(parts, i + 1).strip("'\"")
        elif part == "--day" and i + 1 < len(parts):
            day_str = parts[i + 1]
        elif part == "--time" and i + 1 < len(parts):
            time_val = parts[i + 1].strip("'\"")

    try:
        day_name = DOW_NAMES[int(day_str)] if day_str.isdigit() else day_str
    except (ValueError, IndexError):
        day_name = day_str

    return {
        "id": entry_id,
        "club": club,
        "course": course or "any",
        "day": day_name,
        "time": time_val,
    }


def cmd_list() -> Any:
    """Return list of booking entries managed by the va bot."""
    content = _read_crontab()
    lines = content.splitlines()
    entries: list[dict[str, str]] = []
    cron_lines: list[str] = []
    for line in lines:
        if VA_MARKER_PREFIX in line:
            entry = _cronline_to_dict(line)
            if entry:
                entries.append(entry)
                cron_lines.append(line)

    return {
        "total": len(entries),
        "entries": entries,
        "cron_lines": "\n".join(cron_lines) + "\n" if cron_lines else "",
    }


def cmd_remove(*, entry_id: str | None = None, force: bool = False) -> Any:
    """Remove a booking entry from crontab (interactive or by ID)."""
    result = cmd_list()
    entries = result["entries"]
    if not entries:
        raise VAError("No booking entries found in crontab")

    if entry_id is None:
        choices = [
            f"{e['id']}: {e['club']} — {e['course'] or 'any'} — {e['day']} {e['time']}"
            for e in entries
        ]
        choice = questionary.select(
            "Select entry to remove:",
            choices=choices,
        ).ask()
        if not choice:
            raise VAError("Aborted")
        entry_id = choice.split(":")[0].strip()

    if _do_remove(entry_id):
        print(f"Removed entry: {entry_id}")
        return {
            "status": "success",
            "removed": entry_id,
        }
    else:
        raise VAError(f"No booking entry with id={entry_id}")


def _do_remove(entry_id: str) -> bool:
    """Remove booking entry with the given ID from crontab. Returns True if removed."""
    content = _read_crontab()
    if not content:
        return False

    target = f"{VA_MARKER_PREFIX}{entry_id}"
    lines = content.splitlines()
    new_lines = [
        line for line in lines
        if not (line.strip() and target in line)
    ]
    removed_count = len(lines) - len(new_lines)
    if removed_count == 0:
        return False

    content = "\n".join(new_lines)
    content = re.sub(r"\n{3,}", "\n\n", content).rstrip("\n") + "\n" if content else ""
    _write_crontab(content)
    return True
