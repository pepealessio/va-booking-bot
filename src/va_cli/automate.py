from __future__ import annotations

import logging
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
    """Return book and login cron minute/hour/DOW strings for a recurring class.

    Each class gets two cron entries:
      - login: login_offset_minutes before book (5 min)
      - book:  exactly booking_open_hours before the class (48 h)
    """
    h, m = map(int, time_str.split(":"))
    class_time = datetime(2026, 1, 1, h, m)
    book_time = class_time - timedelta(hours=booking_open_hours)
    login_time = book_time - timedelta(minutes=login_offset_minutes)

    cls_date = datetime(2026, 1, 12 + py_dow)  # 2026-01-12 is Monday
    book_date = cls_date - timedelta(hours=booking_open_hours)
    login_date = book_date - timedelta(minutes=login_offset_minutes)

    return {
        "book_hour": book_time.hour,
        "book_minute": book_time.minute,
        "book_dow": _python_dow_to_cron(book_date.weekday()),
        "login_hour": login_time.hour,
        "login_minute": login_time.minute,
        "login_dow": _python_dow_to_cron(login_date.weekday()),
    }


def build_cron_entry(
    club: str,
    day_of_week: int,
    time_str: str,
    course: str | None = None,
    max_retries: int = 10,
    retry_interval: int = 60,
    va_bin: str = "va",
) -> list[str]:
    """Return two cron lines (login + book) as strings for copy-paste."""
    cron = compute_cron_times(day_of_week, time_str)
    course_part = f" --course '{course}'" if course else ""
    login_line = "%02d %02d * * %d %s login" % (
        cron["login_minute"], cron["login_hour"], cron["login_dow"], va_bin,
    )
    book_line = "%02d %02d * * %d %s book --recurring --club '%s'%s --day %d --time '%s' --retry %d --retry-interval %d" % (
        cron["book_minute"], cron["book_hour"], cron["book_dow"], va_bin,
        club, course_part, day_of_week, time_str, max_retries, retry_interval,
    )
    comment = "# %s — %s — %s %s" % (club, course or "any class", DOW_NAMES[day_of_week], time_str)
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


# ── Worker: cron-login (thin, calls login via client param) ────────


def worker_cron_login() -> dict[str, Any]:
    """Cron-triggered login worker. Called with client already created by cli.py."""
    return {"status": "success"}


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


def interactive_add(client: VirginActiveClient) -> dict[str, Any]:
    """Walk through selection and print copy-paste cron entries."""

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

    preview = (
        f"  Club:    {club_label}\n"
        f"  Course:  {course_label or '(any)'}\n"
        f"  Day:     {DOW_NAMES[day_of_week]}\n"
        f"  Time:    {time_str}\n"
        f"  Trainer: {selected.trainer or '-'}\n"
        f"  Room:    {selected.room or '-'}"
    )
    print(preview)

    max_retries_text = questionary.text(
        "Max retries (default 10):",
        default="10",
    ).ask()
    max_retries_val = int(max_retries_text) if max_retries_text else 10

    retry_interval_text = questionary.text(
        "Retry interval in seconds (default 60):",
        default="60",
    ).ask()
    retry_interval_val = int(retry_interval_text) if retry_interval_text else 60

    cron = compute_cron_times(day_of_week, time_str)
    cron_preview = (
        f"\n  Schedule (48h before class, cron DOW):\n"
        f"  Login: {cron['login_minute']:02d} {cron['login_hour']:02d} * * {cron['login_dow']}\n"
        f"  Book:  {cron['book_minute']:02d} {cron['book_hour']:02d} * * {cron['book_dow']}"
    )
    print(cron_preview)

    confirmed = questionary.confirm("Generate cron entries?").ask()
    if not confirmed:
        raise VAError("Aborted by user")

    lines = build_cron_entry(
        club=club_label,
        course=course_label,
        day_of_week=day_of_week,
        time_str=time_str,
        max_retries=max_retries_val,
        retry_interval=retry_interval_val,
    )
    print()
    print("Copy these lines into your crontab (`crontab -e`):")
    print()
    for line in lines:
        print(f"  {line}")

    return {
        "status": "success",
        "lines": lines,
    }


# ── CLI entry points ──────────────────────────────────────────────


def cmd_add(client: VirginActiveClient) -> Any:
    if not client.has_saved_session():
        print("No saved session. Logging in first...")
        try:
            client.login()
        except VAError as e:
            return {"status": "error", "error": str(e)}
    return interactive_add(client)
