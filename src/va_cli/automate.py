from __future__ import annotations

import logging
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, UTC
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import questionary
import yaml

from .client import VAError, VirginActiveClient
from .config import Config


# ── AutomateConfig ─────────────────────────────────────────────────


class AutomateConfig:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "automate.yaml"
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── load / save ──────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"workdir": self._detect_workdir(), "classes": []}
        with open(self._path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("workdir", self._detect_workdir())
        data.setdefault("classes", [])
        return data

    def save(self, data: dict[str, Any]) -> None:
        self._ensure_dir()
        with open(self._path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    # ── class mutations ──────────────────────────────────────────

    def add(self, cls: dict[str, Any], data: dict[str, Any] | None = None) -> str:
        if data is None:
            data = self.load()
        cls_id = cls.get("id") or uuid.uuid4().hex[:12]
        cls["id"] = cls_id
        data.setdefault("classes", []).append(cls)
        self.save(data)
        return cls_id

    def remove(self, cls_id: str, data: dict[str, Any] | None = None) -> bool:
        if data is None:
            data = self.load()
        classes = data.setdefault("classes", [])
        before = len(classes)
        data["classes"] = [c for c in classes if c.get("id") != cls_id]
        if len(data["classes"]) < before:
            self.save(data)
            return True
        return False

    def get(self, cls_id: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if data is None:
            data = self.load()
        for c in data.get("classes", []):
            if c.get("id") == cls_id:
                return c
        return None

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _detect_workdir() -> str:
        script = Path(sys.argv[0]).resolve()
        return str(script.parent.parent) if script.name == "va" else str(Path.cwd())


# ── Cron computation ──────────────────────────────────────────────


def _cron_dow_from_python(py_dow: int) -> int:
    """Python: Mon=0 … Sun=6 → Cron: Sun=0, Mon=1 … Sat=6."""
    return (py_dow + 1) % 7


def compute_cron_minutes(
    py_dow: int,
    time_str: str,
    booking_open_hours: int = 48,
    login_offset_minutes: int = 5,
) -> dict[str, Any]:
    """Return book and login cron entry strings for a recurring class.

    Each class gets two cron entries:
      - book:  exactly `booking_open_hours` before the class (48 h)
      - login: `login_offset_minutes` before book time (5 min)

    Returns dict with:
      book_cron, book_minute, book_hour, book_dow
      login_cron, login_minute, login_hour, login_dow
    """
    h, m = map(int, time_str.split(":"))
    class_time = datetime(2026, 1, 1, h, m)  # dummy date, extract time math
    book_time = class_time - timedelta(hours=booking_open_hours)
    login_time = book_time - timedelta(minutes=login_offset_minutes)

    cron_dow = _cron_dow_from_python(py_dow)

    # We need the cron DOW for the login/book day (48h before the class day)
    # class_time - 48h may span multiple boundaries but we only care about DOW
    # The simplest approach: compute from the class day offset by 2 days back.
    class_date = datetime(2026, 1, 12)  # arbitrary Monday for dow=0 mapping
    # Adjust to match py_dow
    extra = py_dow  # 2026-01-12 is Monday (dow=0), so add py_dow days
    cls_date = datetime(2026, 1, 12 + py_dow)
    book_date = cls_date - timedelta(hours=booking_open_hours)
    login_date = book_date - timedelta(minutes=login_offset_minutes)
    book_cron_dow = _cron_dow_from_python(book_date.weekday())
    login_cron_dow = _cron_dow_from_python(login_date.weekday())

    book_h, book_m = book_time.hour, book_time.minute
    login_h, login_m = login_time.hour, login_time.minute

    return {
        "book_minute": book_m,
        "book_hour": book_h,
        "book_dow": book_cron_dow,
        "book_cron": f"{book_m} {book_h} * * {book_cron_dow}",
        "login_minute": login_m,
        "login_hour": login_h,
        "login_dow": login_cron_dow,
        "login_cron": f"{login_m} {login_h} * * {login_cron_dow}",
    }


def build_cron_lines(
    cls: dict[str, Any],
    workdir: str,
    va_bin: str = "va",
) -> list[str]:
    """Build the crontab lines for a single class entry."""
    cls_id = cls["id"]
    course = cls.get("course", "class").replace(" ", "_")
    cron = compute_cron_minutes(cls["day_of_week"], cls["time"])

    login_cmd = f"cd {workdir} && {va_bin} automate cron-login"
    book_cmd = f"cd {workdir} && {va_bin} automate cron-book --class {cls_id}"

    lines = [
        f"# va-booking-bot: {cls_id} (login {course})",
        f"{cron['login_cron']}  {login_cmd}",
        f"# va-booking-bot: {cls_id} (book {course})",
        f"{cron['book_cron']}  {book_cmd}",
    ]
    return lines


def all_cron_lines(config_data: dict[str, Any], va_bin: str = "va") -> list[str]:
    workdir = config_data.get("workdir", str(Path.cwd()))
    lines: list[str] = []
    for cls in config_data.get("classes", []):
        lines.extend(build_cron_lines(cls, workdir, va_bin))
    return lines


# ── Crontab management ────────────────────────────────────────────


_MARKER_PREFIX = "# va-booking-bot:"


def _current_crontab() -> str:
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
        )
        return result.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def _install_crontab(content: str) -> None:
    proc = subprocess.run(
        ["crontab", "-"],
        input=content,
        text=True,
    )
    if proc.returncode != 0:
        raise VAError(f"Failed to install crontab: {proc.stderr.strip()}")


def install_crontab_entries(config_data: dict[str, Any]) -> list[str]:
    """Read current crontab, replace existing va-booking-bot entries, write back.
    Returns list of cron lines that were installed."""
    new_lines = all_cron_lines(config_data)
    current = _current_crontab()
    kept: list[str] = [
        line for line in current.splitlines()
        if not line.startswith(_MARKER_PREFIX) and line.strip()
    ]
    merged = kept + new_lines
    _install_crontab("\n".join(merged) + "\n")
    return new_lines


def remove_crontab_entries() -> int:
    """Remove all va-booking-bot entries from crontab. Returns number removed."""
    current = _current_crontab()
    lines = current.splitlines()
    kept: list[str] = []
    skip_next = False
    removed = 0
    for line in lines:
        if line.startswith(_MARKER_PREFIX):
            # Skip marker comment and the cron line that follows it
            removed += 1
            skip_next = True
            if not line.strip():
                removed += 1
            continue
        if skip_next and line.strip():
            removed += 1
            skip_next = False
            continue
        skip_next = False
        if line.strip():
            kept.append(line)
    _install_crontab("\n".join(kept) + "\n" if kept else "")
    return removed


def list_crontab_entries(config_data: dict[str, Any]) -> list[str]:
    return all_cron_lines(config_data)


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


# ── Worker: cron-login ────────────────────────────────────────────


def worker_cron_login(state_dir: Path) -> dict[str, Any]:
    """Cron-triggered login worker. Restores credentials from .env/keyring."""
    logger = _setup_logger(state_dir)
    config = Config.from_env()
    logger.info("cron-login: starting")
    try:
        with VirginActiveClient(config, verbose=False) as client:
            session = client.login()
            logger.info("cron-login: success, session updated")
            return {"status": "success"}
    except Exception as e:
        logger.error(f"cron-login: failed: {e}")
        raise


# ── Worker: cron-book ─────────────────────────────────────────────


def worker_cron_book(
    state_dir: Path,
    cls_id: str,
) -> dict[str, Any]:
    """Cron-triggered booking worker with retry logic.

    1. Load class config entry
    2. Create client, restore session
    3. Search for matching class on today+2d (the class day since cron runs 48h before)
    4. Attempt booking
    5. On failure, sleep retry_interval and retry up to max_retries
    """
    config_helper = AutomateConfig(state_dir)
    data = config_helper.load()
    cls = config_helper.get(cls_id, data)
    if not cls:
        raise VAError(f"No class configuration found for id={cls_id}")

    max_retries = cls.get("max_retries", 10)
    retry_interval = cls.get("retry_interval", 60)
    logger = _setup_logger(state_dir)

    logger.info(
        f"cron-book: starting for {cls_id} "
        f"(club={cls.get('club')}, course={cls.get('course')}, "
        f"dow={cls['day_of_week']}, time={cls['time']})"
    )

    config = Config.from_env()

    for attempt in range(1, max_retries + 1):
        logger.info(f"cron-book: attempt {attempt}/{max_retries}")
        try:
            result = _do_book_attempt(config, cls)
            status_code = result.get("StatusCode", result.get("statusCode"))
            if status_code == 200 or (isinstance(status_code, str) and status_code == "200"):
                logger.info(f"cron-book: SUCCESS on attempt {attempt}")
                return {"status": "success", "class_id": cls_id, "attempts": attempt}
            else:
                logger.warning(f"cron-book: non-200 status: {result}")
                _sleep_retry(attempt, max_retries, retry_interval)

        except VAError as e:
            logger.error(f"cron-book: VAError on attempt {attempt}: {e}")
            _sleep_retry(attempt, max_retries, retry_interval)

        except Exception as e:
            logger.error(f"cron-book: error on attempt {attempt}: {e}")
            _sleep_retry(attempt, max_retries, retry_interval)

    logger.error(f"cron-book: FAILED after {max_retries} attempts for {cls_id}")
    raise VAError(
        f"Failed to book class {cls_id} after {max_retries} attempts"
    )


def _do_book_attempt(
    config: Config,
    cls: dict[str, Any],
) -> dict[str, Any]:
    """Single booking attempt: restore session, find class, book it."""
    time_str = cls["time"]
    target_date = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d")
    filters: dict[str, str | None] = {
        "club": cls.get("club"),
        "course": cls.get("course"),
        "date": target_date,
        "trainer": None,
        "target": None,
    }

    with VirginActiveClient(config, verbose=False) as client:
        if not client.has_saved_session():
            try:
                client.login()
            except Exception:
                raise VAError("No saved session and cannot log in")

        try:
            found = client.list_classes(filters, use_auth=True, approve=lambda _: True)
        except VAError:
            client.login()
            found = client.list_classes(filters, use_auth=True, approve=lambda _: True)

        matched = [c for c in found if c.start_time == time_str]
        if not matched:
            course_name = cls.get("course")
            if course_name:
                matched = [
                    c for c in found
                    if course_name.lower() in (c.title or "").lower()
                ]
        if not matched:
            raise VAError(
                f"Cannot find class matching time={time_str}, date={target_date}. "
                f"Found {len(found)} classes for that date."
            )

        target = matched[0]
        logger = logging.getLogger("va-automate")
        logger.info(f"cron-book: found class {target.token} ({target.title})")

        return client.book(target.token, approve=lambda _: True)


def _sleep_retry(attempt: int, max_retries: int, interval: int) -> None:
    if attempt < max_retries:
        logger = logging.getLogger("va-automate")
        logger.info(f"cron-book: sleeping {interval}s before retry {attempt + 1}")
        import time
        time.sleep(interval)


# ── Interactive add flow ──────────────────────────────────────────


def interactive_add(
    client: VirginActiveClient,
    config: AutomateConfig,
) -> dict[str, Any]:
    """Walk through selection: club, course, day, time → save to config."""

    # 1. Club
    clubs = client.get_calendar_filters().clubs
    if not clubs:
        raise VAError("No clubs found.")
    club_label = questionary.select(
        "Select club:",
        choices=[c.label for c in clubs],
    ).ask()
    if not club_label:
        raise VAError("Aborted: no club selected")
    club = next(c for c in clubs if c.label == club_label)

    # 2. Course
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

    # 3. Day of week
    day_names = [
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    ]
    day_sel = questionary.select(
        "Select day of week:",
        choices=day_names,
    ).ask()
    if not day_sel:
        raise VAError("Aborted")
    day_of_week = day_names.index(day_sel)

    # 4. Pick a date that matches the selected day in the near future
    #    Show classes for the next 7 days to let user pick from actual times
    today = datetime.now(UTC).date()
    candidate_dates: list[str] = []
    for d in range(1, 14):
        dt = today + timedelta(days=d)
        if dt.weekday() == day_of_week:
            candidate_dates.append(dt.strftime("%Y-%m-%d"))
    if not candidate_dates:
        raise VAError("No matching date found in the next 14 days.")

    search_date = candidate_dates[0]
    filters: dict[str, str | None] = {
        "club": club.label,
        "course": course_label,
        "date": search_date,
        "trainer": None,
        "target": None,
    }

    try:
        classes = client.list_classes(filters, use_auth=True, approve=lambda _: True)
    except VAError:
        classes = client.list_classes(filters, use_auth=False)

    if not classes:
        raise VAError(
            f"No classes found for {club.label} on {search_date}. "
            "Try different filters or a different club."
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

    selected_cls = classes[sel_idx]

    if not selected_cls or not selected_cls.start_time:
        raise VAError("Selected class has no valid time")

    time_str = selected_cls.start_time

    # 5. Confirm details
    preview = (
        f"  Club:    {club.label}\n"
        f"  Course:  {course_label or '(any)'}\n"
        f"  Day:     {day_names[day_of_week]}\n"
        f"  Time:    {time_str}\n"
        f"  Trainer: {selected_cls.trainer or '-'}\n"
        f"  Room:    {selected_cls.room or '-'}"
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

    # 6. Cron schedule preview
    cron = compute_cron_minutes(day_of_week, time_str)
    cron_preview = (
        f"\n  Cron schedule (48h before class):\n"
        f"  Login: {cron['login_minute']:02d} {cron['login_hour']:02d} * * {cron['login_dow']}\n"
        f"  Book:  {cron['book_minute']:02d} {cron['book_hour']:02d} * * {cron['book_dow']}"
    )
    print(cron_preview)

    confirmed = questionary.confirm("Add this recurring booking?").ask()
    if not confirmed:
        raise VAError("Aborted by user")

    cls_entry = {
        "club": club.label,
        "course": course_label,
        "day_of_week": day_of_week,
        "time": time_str,
        "trainer": selected_cls.trainer,
        "room": selected_cls.room,
        "max_retries": max_retries_val,
        "retry_interval": retry_interval_val,
    }
    new_id = config.add(cls_entry)

    # 7. Offer crontab install
    schedule_now = questionary.confirm("Install crontab entries now?").ask()
    cron_lines = []
    if schedule_now:
        data = config.load()
        cron_lines = install_crontab_entries(data)

    return {
        "status": "success",
        "class_id": new_id,
        "entry": cls_entry,
        "cron_lines": cron_lines,
    }


# ── Public entry points for cli.py dispatch ───────────────────────


def cmd_add(client: VirginActiveClient) -> Any:
    config = AutomateConfig(client.config.state_dir)

    if client.has_saved_session():
        result = interactive_add(client, config)
    else:
        print("No saved session. Logging in first...")
        try:
            client.login()
        except VAError as e:
            print(f"Login failed: {e}")
            print("Save session with `va login` then retry.")
            return {"status": "error", "error": str(e)}
        result = interactive_add(client, config)
    return result


def cmd_list(state_dir: Path) -> Any:
    config = AutomateConfig(state_dir)
    data = config.load()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    rows: list[dict[str, Any]] = []
    for cls in data.get("classes", []):
        cron = compute_cron_minutes(cls["day_of_week"], cls["time"])
        rows.append({
            "id": cls["id"],
            "club": cls.get("club", "-"),
            "course": cls.get("course") or "(any)",
            "day": day_names[cls["day_of_week"]],
            "time": cls["time"],
            "login_cron": cron["login_cron"],
            "book_cron": cron["book_cron"],
            "max_retries": cls.get("max_retries", 10),
        })

    if not rows:
        return {"status": "info", "message": "No recurring classes configured."}
    return rows


def cmd_remove(state_dir: Path, cls_id: str) -> Any:
    config = AutomateConfig(state_dir)
    found = config.remove(cls_id)
    if not found:
        raise VAError(f"No class with id={cls_id}")
    return {"status": "success", "removed": cls_id}


def cmd_schedule(state_dir: Path) -> Any:
    config = AutomateConfig(state_dir)
    data = config.load()
    if not data.get("classes"):
        return {"status": "info", "message": "No classes to schedule."}
    lines = install_crontab_entries(data)
    return {"status": "success", "installed": len(lines), "lines": lines}


def cmd_unschedule(state_dir: Path) -> Any:
    removed = remove_crontab_entries()
    return {"status": "success", "removed": removed}


def cmd_cron_login(state_dir: Path) -> Any:
    return worker_cron_login(state_dir)


def cmd_cron_book(state_dir: Path, cls_id: str) -> Any:
    return worker_cron_book(state_dir, cls_id)
