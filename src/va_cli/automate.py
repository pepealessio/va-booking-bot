from __future__ import annotations

import logging
import random
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import questionary

from .client import VAError, VirginActiveClient, ApprovalCallback
from .notifier import NullNotifier, cancel_keyboard, from_config, TelegramNotifier
from .store import BookingRecord, BookingStore, generate_id

# ── Day name helpers ─────────────────────────────────────────────


DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


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
        result[f"{label}_dow"] = ((py_dow - extra_days) % 7 + 1) % 7

    return result


VA_MARKER_PREFIX = "# va-automate:"


def _generate_id() -> str:
    """Generate a short, unique ID for a cron entry set."""
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
    """Return three cron lines (comment + find/login + book) as strings for copy-paste.

    The find/login line resolves the class token at login time and writes it
    to ``/tmp/va_booking_<entry_id>``.  The book line reads that file so the
    booking call has a virtually zero-delay class resolution at the bell.
    """
    if entry_id is None:
        entry_id = _generate_id()
    cron = compute_cron_times(day_of_week, time_str)
    marker = f"{VA_MARKER_PREFIX}{entry_id}"
    tmp_file = f"/tmp/va_booking_{entry_id}"

    course_part = f" --course '{course}'" if course else ""
    find_cmd = (
        f"{va_bin} login --notify f && {va_bin} --dangerously-approve-token --json classes --notify f"
        f" --club '{club}'{course_part}"
        f" --day {day_of_week} --time '{time_str}'"
        f" | python3 -c \"import sys,json;d=json.load(sys.stdin)[0];print(d['id']);print(f\\\"{{d.get('club','')}}|{{d.get('title','')}}|{{d.get('date','')}}|{{d.get('start_time','')}}\\\")\""
        f" > {tmp_file}"
    )
    login_line = "%02d %02d * * %d %s %s" % (
        cron["login_minute"], cron["login_hour"], cron["login_dow"], find_cmd, marker,
    )
    book_line = "%02d %02d * * %d %s --dangerously-approve-token book --notify sf \"$(head -1 %s)\" --info \"$(tail -1 %s)\" --retry %d --retry-interval %d %s" % (
        cron["book_minute"], cron["book_hour"], cron["book_dow"], va_bin,
        tmp_file, tmp_file, max_retries, retry_interval, marker,
    )
    comment = "# %s — %s — %s %s %s" % (club, course or "any class", DOW_NAMES[day_of_week], time_str, marker)
    return [comment, login_line, book_line]


# ── Worker: book with retry ───────────────────────────────────────


def _fmt_class_info(token: str, info: str | None) -> str:
    """Format human-readable class description from ``info``.

    Accepts ``Club|Title|Date|Time`` (4 parts) or the legacy 3-part
    ``Club|Title|Time``.  Falls back to the raw token when no info.
    """
    if info:
        parts = info.split("|")
        if len(parts) == 4:
            club, title, date, time_str = parts
            day = datetime.strptime(date, "%Y-%m-%d").strftime("%A") if date else ""
            day_part = f" on {day}" if day else ""
            return f"**{title}** at {club}{day_part} ({time_str})"
        if len(parts) == 3:
            club, title, time_str = parts
            return f"**{title}** at {club} ({time_str})"
    return f"token **{token}**"


def worker_book(
    *,
    client: VirginActiveClient,
    token: str,
    approve: ApprovalCallback,
    max_retries: int = 10,
    retry_interval: int = 5,
    notify: str | None = None,
    info: str | None = None,
) -> dict[str, Any]:
    """Book a class with retry loop and optional Telegram notification.

    Tries to book the given token up to ``max_retries`` times.
    Sends a Telegram notification on success or when all retries are exhausted
    only when the ``notify`` flag (``"s"``, ``"f"``, or ``"sf"``) allows it.
    """
    logger = logging.getLogger("va-automate")
    notifier = from_config(None) if notify else NullNotifier()

    class_desc = _fmt_class_info(token, info)
    last_error: str | None = None

    for attempt in range(1, max_retries + 1):
        logger.info("book attempt %d/%d for token %s", attempt, max_retries, token)
        try:
            result = client.book(token, approve=approve)
            status_code = result.get("StatusCode", result.get("statusCode"))
            if status_code == 200 or (isinstance(status_code, str) and status_code == "200"):
                logger.info("SUCCESS on attempt %d", attempt)
                if notify and "s" in notify:
                    if isinstance(notifier, TelegramNotifier):
                        store = BookingStore(client.config.state_dir / "bookings.json")
                        record = BookingRecord(
                            id=generate_id(),
                            token=token,
                            class_desc=class_desc,
                            chat_id=int(notifier.chat_id),
                        )
                        store.save(record)
                        msg_id = notifier.send(
                            "success",
                            f"Booking confirmed for {class_desc} (attempt {attempt})",
                            reply_markup=cancel_keyboard(record.id),
                        )
                        if msg_id is not None:
                            record.message_id = msg_id
                            store.save(record)
                    else:
                        notifier.send(
                            "success",
                            f"Booking confirmed for {class_desc} (attempt {attempt})",
                        )
                return {"status": "success", "token": token, "attempts": attempt}
            else:
                last_error = f"status {status_code}"
                logger.warning("non-200 status on attempt %d: %s", attempt, result)
                if attempt < max_retries:
                    time.sleep(retry_interval)

        except VAError as e:
            last_error = str(e)
            logger.error("VAError on attempt %d: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(retry_interval)

        except Exception as e:
            last_error = str(e)
            logger.error("error on attempt %d: %s", attempt, e)
            if attempt < max_retries:
                time.sleep(retry_interval)

    detail = f" — {last_error}" if last_error else ""
    logger.error("FAILED after %d attempts for token %s%s", max_retries, token, detail)
    if notify and "f" in notify:
        notifier.send(
            "error",
            f"Booking failed for {class_desc} after {max_retries} attempts{detail}",
        )
    raise VAError(f"Failed to book token {token} after {max_retries} attempts{detail}")


# ── Interactive add flow → print cron lines ────────────────────────


def _pick_club(client: VirginActiveClient) -> str:
    clubs = client.get_calendar_filters().clubs
    if not clubs:
        raise VAError("No clubs found.")
    label = questionary.select("Select club:", choices=[c.label for c in clubs]).ask()
    if not label:
        raise VAError("Aborted: no club selected")
    return label


def _pick_course(client: VirginActiveClient) -> str | None:
    courses = client.get_calendar_filters().courses
    if not courses:
        return None
    choices = ["(any)"] + [c.label for c in courses]
    label = questionary.select("Select course:", choices=choices).ask()
    if not label:
        raise VAError("Aborted")
    return None if label == "(any)" else label


def _pick_day() -> int:
    sel = questionary.select("Select day of week:", choices=DOW_NAMES).ask()
    if not sel:
        raise VAError("Aborted")
    return DOW_NAMES.index(sel)


def _fetch_candidate_classes(client: VirginActiveClient, club: str | None, course: str | None, day_of_week: int) -> list:
    today = datetime.now(UTC).date()
    candidate_date = None
    for d in range(1, 14):
        dt = today + timedelta(days=d)
        if dt.weekday() == day_of_week:
            candidate_date = dt.strftime("%Y-%m-%d")
            break
    if not candidate_date:
        raise VAError("No matching date in next 14 days.")

    filters: dict[str, str | None] = {
        "club": club, "course": course, "date": candidate_date,
        "trainer": None, "target": None,
    }
    try:
        classes = client.list_classes(filters, use_auth=True, approve=lambda _: True)
    except VAError:
        classes = client.list_classes(filters, use_auth=False)

    if not classes:
        raise VAError(f"No classes found for {club} on {candidate_date}. Try different filters.")
    return classes


def _pick_class(classes) -> tuple:
    options = []
    for i, c in enumerate(classes):
        lbl = f"{c.start_time or '?'} | {c.title}"
        if c.trainer:
            lbl += f" ({c.trainer})"
        if c.room:
            lbl += f" — {c.room}"
        options.append({"name": lbl, "value": i})
    sel = questionary.select("Select a class:", choices=options).ask()
    if sel is None:
        raise VAError("Aborted")
    return classes[sel]


def interactive_add(client: VirginActiveClient, max_retries: int = 10, retry_interval: int = 60) -> None:
    """Walk through selection and print cron lines to stdout.

    Commentary (summary, usage tips) goes to stderr so the cron lines
    on stdout remain clean for piping to ``crontab -``.
    Returns ``None`` — the caller should not render anything extra.
    """
    # questionary writes to sys.stdout; redirect to stderr so that
    # ``va automate add | crontab -`` works (stdout is the pipe, but
    # the interactive prompts render on stderr which is still the TTY).
    club: str | None = None
    course: str | None = None
    day_of_week = 0
    classes: list = []
    selected = None

    old_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        club = _pick_club(client)
        course = _pick_course(client)
        day_of_week = _pick_day()
        classes = _fetch_candidate_classes(client, club, course, day_of_week)
        selected = _pick_class(classes)
    finally:
        sys.stdout = old_stdout

    if not selected or not selected.start_time:
        raise VAError("Selected class has no valid time")
    time_str = selected.start_time

    print(
        f"  Club:    {club}\n"
        f"  Course:  {course or '(any)'}\n"
        f"  Day:     {DOW_NAMES[day_of_week]}\n"
        f"  Time:    {time_str}\n"
        f"  Trainer: {selected.trainer or '-'}\n"
        f"  Room:    {selected.room or '-'}",
        file=sys.stderr,
    )

    old_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        max_retries = questionary.text("Max retry attempts:", default=str(max_retries)).ask()
        if max_retries:
            max_retries = int(max_retries)
        retry_interval = questionary.text("Retry interval (seconds):", default=str(retry_interval)).ask()
        if retry_interval:
            retry_interval = int(retry_interval)
    finally:
        sys.stdout = old_stdout

    entry_id = _generate_id()
    lines = build_cron_entry(
        club=club, course=course, day_of_week=day_of_week, time_str=time_str,
        max_retries=max_retries, retry_interval=retry_interval,
        entry_id=entry_id, va_bin=sys.argv[0],
    )

    for line in lines:
        print(line)

    print(file=sys.stderr)
    print("Install these cron lines:", file=sys.stderr)
    print(f"  {sys.argv[0]} automate add | crontab -", file=sys.stderr)
    print("List entries:", file=sys.stderr)
    print(f"  crontab -l | {sys.argv[0]} automate list", file=sys.stderr)
    print("Remove this entry:", file=sys.stderr)
    print(f"  {sys.argv[0]} automate remove {entry_id} | crontab -", file=sys.stderr)


# ── CLI entry points ──────────────────────────────────────────────


def cmd_add(client: VirginActiveClient, args: Any = None) -> None:
    max_retries = getattr(args, "retry", 10)
    retry_interval = getattr(args, "retry_interval", 60)

    club = getattr(args, "club", None)
    day = getattr(args, "day", None)
    time_str = getattr(args, "time", None)

    if club is not None and day is not None and time_str is not None:
        course = getattr(args, "course", None)
        _print_noninteractive(club, day, time_str, course, max_retries, retry_interval)
        return

    if not client.has_saved_session():
        print("No saved session. Logging in first...", file=sys.stderr)
        try:
            client.login()
        except VAError as e:
            print(f"error: {e}", file=sys.stderr)
            return
    interactive_add(client, max_retries, retry_interval)


def _print_noninteractive(
    club: str,
    day: int,
    time_str: str,
    course: str | None,
    max_retries: int,
    retry_interval: int,
) -> None:
    entry_id = _generate_id()
    lines = build_cron_entry(
        club=club, course=course, day_of_week=day, time_str=time_str,
        max_retries=max_retries, retry_interval=retry_interval,
        entry_id=entry_id, va_bin=sys.argv[0],
    )
    for line in lines:
        print(line)
    print(file=sys.stderr)
    print("Install these cron lines:", file=sys.stderr)
    print(f"  {sys.argv[0]} automate add | crontab -", file=sys.stderr)
    print("List entries:", file=sys.stderr)
    print(f"  crontab -l | {sys.argv[0]} automate list", file=sys.stderr)
    print("Remove this entry:", file=sys.stderr)
    print(f"  {sys.argv[0]} automate remove {entry_id} | crontab -", file=sys.stderr)


# ── List / remove from crontab ────────────────────────────────────


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


def _extract_marker_id(line: str) -> str | None:
    """Extract the va-automate entry ID from a cron line."""
    if VA_MARKER_PREFIX not in line:
        return None
    for part in line.split():
        if "va-automate:" in part:
            return part.split("va-automate:")[1]
    return None


def _cronline_to_dict(line: str) -> dict[str, str] | None:
    """Parse a va-automate find/login line into a dict with id, club, course, day, time."""
    if not line.strip() or line.startswith("#"):
        return None
    if "--json classes" not in line:
        return None
    parts = line.split()
    entry_id = _extract_marker_id(line)
    if not entry_id:
        return None

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
        "course": course if not day_str else (course or "any"),
        "day": day_name,
        "time": time_val,
    }


def _crontab_entries(content: str) -> list[dict[str, str]]:
    """Parse crontab *content* and return all va-automate book entries.

    Groups lines by marker, extracts details from the find/login line
    (which contains ``--json classes``).
    """
    lines = content.splitlines()
    seen: set[str] = set()
    entries: list[dict[str, str]] = []
    for line in lines:
        if "--json classes" not in line:
            continue
        entry_id = _extract_marker_id(line)
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        entry = _cronline_to_dict(line)
        if entry:
            entries.append(entry)
    return entries


def cmd_list(content: str) -> dict[str, Any]:
    """Return list of booking entries from crontab *content*."""
    entries = _crontab_entries(content)
    cron_lines = [line for line in content.splitlines() if VA_MARKER_PREFIX in line and "$(cat /tmp/va_booking_" in line]
    return {
        "total": len(entries),
        "entries": entries,
        "cron_lines": "\n".join(cron_lines) + "\n" if cron_lines else "",
    }


def cmd_remove(content: str, *, entry_id: str | None = None) -> None:
    """Remove a booking entry and print the modified crontab to stdout.

    The modified crontab goes to stdout; the confirmation message goes to
    stderr.  Returns ``None`` — the caller should not render anything
    extra.
    """
    if sys.stdout.isatty():
        raise VAError(
            "Output would go to the terminal — pipe to crontab - instead:\n"
            "  crontab -l | va automate remove | crontab -"
        )
    entries = _crontab_entries(content)
    if not entries:
        raise VAError("No booking entries found in crontab")

    if entry_id is None:
        choices = [
            f"{e['id']}: {e['club']} — {e['course'] or 'any'} — {e['day']} {e['time']}"
            for e in entries
        ]
        choice = questionary.select("Select entry to remove:", choices=choices).ask()
        if not choice:
            raise VAError("Aborted")
        entry_id = choice.split(":")[0].strip()

    new_content = _do_remove(entry_id, content)
    if new_content is None:
        raise VAError(f"No booking entry with id={entry_id}")

    if not new_content.strip():
        print("  Warning: crontab will be empty after removal.", file=sys.stderr)

    print(new_content, end="")
    print(f"  Removed entry: {entry_id}", file=sys.stderr)


def _do_remove(entry_id: str, content: str) -> str | None:
    """Return *content* with all lines matching *entry_id* removed, or ``None`` if not found."""
    if not content:
        return None
    target = f"{VA_MARKER_PREFIX}{entry_id}"
    lines = content.splitlines()
    new_lines = [line for line in lines if not (line.strip() and target in line)]
    if len(new_lines) == len(lines):
        return None
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(new_lines)).strip("\n")
    if collapsed:
        result = collapsed + "\n"
    else:
        result = ""
    return result
