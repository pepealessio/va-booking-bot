from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from .models import CalendarClass, CalendarDateOption, CalendarFilters, FilterOption


def _normalize(text: str) -> str:
    return " ".join(unescape(text).replace("\xa0", " ").split())


QUEUE_LENGTH_PATTERNS = (
    re.compile(r"(?P<count>\d+)\s+utenti?\s+in\s+attesa", re.IGNORECASE),
    re.compile(r"(?P<count>\d+)\s+persone?\s+in\s+attesa", re.IGNORECASE),
)

AVAILABLE_PLACES_PATTERNS = (
    re.compile(r"(?P<count>\d+)\s+posti?\s+(?:disponibili|rimasti|liberi)", re.IGNORECASE),
    re.compile(r"ultimi\s+(?P<count>\d+)\s+posti?", re.IGNORECASE),
)

FULL_QUEUE_MARKERS = (
    "attesa piena",
    "lista d'attesa piena",
    "coda piena",
    "waitlist full",
    "full queue",
    "queue full",
    "nessun posto in attesa",
    "nessun posto in coda",
    "posti in attesa esauriti",
    "posti in coda esauriti",
)

FULL_MARKERS = (
    "prenotazioni non disponibili",
    "prenotazione non disponibile",
    "booking not available",
    "bookings not available",
    "completo",
    "completa",
    "full",
)


def _extract_count(button_label: str | None, patterns: tuple[re.Pattern[str], ...]) -> int | None:
    if not button_label:
        return None
    for pattern in patterns:
        match = pattern.search(button_label)
        if match:
            return int(match.group("count"))
    return None


def _normalize_status(button_label: str | None) -> str:
    if not button_label:
        return "unavailable"
    lowered = button_label.casefold()
    if any(marker in lowered for marker in FULL_QUEUE_MARKERS):
        return "queue_full"
    if any(marker in lowered for marker in ("attesa", "coda", "waitlist", "queue")):
        return "queue"
    if any(marker in lowered for marker in FULL_MARKERS):
        return "full"
    if "prenota" in lowered:
        return "bookable"
    return "unavailable"


class CalendarPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.filters = CalendarFilters()
        self._current_select: dict[str, str] | None = None
        self._current_option: list[str] | None = None
        self._current_option_value: str | None = None
        self._current_target: dict[str, str] | None = None
        self._current_target_text: list[str] | None = None
        self.date_options: list[CalendarDateOption] = []
        self._current_date: dict[str, str | bool] | None = None
        self._current_date_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "select" and attr.get("data-param") in {"class_ids", "trainer_ids", "club_ids"}:
            self._current_select = {
                "data_param": attr["data-param"] or "",
                "name": attr.get("name", ""),
            }
        elif tag == "option" and self._current_select is not None:
            self._current_option = []
            self._current_option_value = attr.get("value")
        elif tag == "button" and "changeFilterButton" in (attr.get("class") or ""):
            self._current_target = {
                "value": attr.get("data-value", "") or "",
            }
            self._current_target_text = []
        elif tag == "div" and "calendarDay" in (attr.get("class") or "") and attr.get("data-day"):
            self._current_date = {
                "date": attr["data-day"] or "",
                "selected": "selected" in (attr.get("class") or ""),
            }
            self._current_date_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._current_select = None
        elif tag == "option" and self._current_select is not None and self._current_option is not None:
            label = _normalize("".join(self._current_option))
            value = _normalize(self._current_option_value or label)
            if label:
                option = FilterOption(label=label, value=value)
                data_param = self._current_select["data_param"]
                if data_param == "class_ids":
                    self.filters.courses.append(option)
                elif data_param == "trainer_ids":
                    self.filters.trainers.append(option)
                elif data_param == "club_ids":
                    self.filters.clubs.append(option)
            self._current_option = None
            self._current_option_value = None
        elif tag == "button" and self._current_target is not None and self._current_target_text is not None:
            label = _normalize("".join(self._current_target_text))
            value = _normalize(self._current_target["value"])
            if label and value:
                self.filters.targets.append(FilterOption(label=label, value=value))
            self._current_target = None
            self._current_target_text = None
        elif tag == "div" and self._current_date is not None and self._current_date_text is not None:
            parts = [item for item in (_normalize(x) for x in self._current_date_text) if item]
            if len(parts) >= 2:
                self.date_options.append(
                    CalendarDateOption(
                        date=str(self._current_date["date"]),
                        weekday=parts[0],
                        day_number=parts[1],
                        selected=bool(self._current_date["selected"]),
                    )
                )
            self._current_date = None
            self._current_date_text = None

    def handle_data(self, data: str) -> None:
        if self._current_option is not None:
            self._current_option.append(data)
        if self._current_target_text is not None:
            self._current_target_text.append(data)
        if self._current_date_text is not None:
            self._current_date_text.append(data)


class CalendarClassParser(HTMLParser):
    TIME_PATTERN = re.compile(r"(?P<start>\d{1,2}:\d{2})\s+(?P<end>\d{1,2}:\d{2})")
    TOKEN_PATTERN = re.compile(r"(?P<booking_id>\d+)c(?P<center>\d+)$")
    ONCLICK_BOOK_PATTERN = re.compile(
        r"(?:bookClass|unbookClass)\((?P<booking_id>\d+)\s*,\s*(?P<center>\d+)\)",
        re.IGNORECASE,
    )

    def __init__(self, *, selected_date: str | None) -> None:
        super().__init__()
        self.selected_date = selected_date
        self.classes: list[CalendarClass] = []
        self.date_options: list[CalendarDateOption] = []
        self._current_date: dict[str, str | bool] | None = None
        self._current_date_text: list[str] | None = None
        self._date_depth = 0
        self._current_card: dict[str, str] | None = None
        self._card_depth = 0
        self._current_field: str | None = None
        self._buffers: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        class_name = attr.get("class") or ""
        if tag == "div" and "calendarDay" in class_name and attr.get("data-day"):
            self._current_date = {
                "date": attr["data-day"] or "",
                "selected": "selected" in class_name,
            }
            self._current_date_text = []
            self._date_depth = 1
            return
        if self._current_date is not None:
            self._date_depth += 1
        if tag == "div" and "calendarLesson classLine" in class_name:
            self._current_card = {}
            self._buffers = {
                "time": [],
                "title": [],
                "trainer": [],
                "club": [],
            }
            self._card_depth = 1
            return
        if self._current_card is None:
            return
        self._card_depth += 1
        if tag == "div":
            if "calendarLessonOrario" in class_name:
                self._current_field = "time"
            elif "calendaClassName" in class_name:
                self._current_field = "title"
            elif "calendarLessonTrainer" in class_name:
                self._current_field = "trainer"
            elif "calendarLessonClub" in class_name:
                self._current_field = "club"
        elif tag in {"a", "button"} and "btn" in class_name:
            token = attr.get("id", "") or ""
            onclick = attr.get("onclick", "") or ""
            # Some authenticated cards only expose booking data inside onclick.
            match = self.ONCLICK_BOOK_PATTERN.search(onclick)
            if not token and match:
                token = f'{match.group("booking_id")}c{match.group("center")}'
            self._current_card["token"] = token
            self._current_card["button_href"] = attr.get("href", "") or ""
            self._current_card["button_onclick"] = onclick
            self._current_field = "button_label"
            self._buffers.setdefault("button_label", [])
        elif tag == "br" and self._current_field in self._buffers:
            self._buffers[self._current_field].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._current_date is not None:
            self._date_depth -= 1
            if tag == "div" and self._date_depth == 0 and self._current_date_text is not None:
                parts = [item for item in (_normalize(x) for x in self._current_date_text) if item]
                if len(parts) >= 2:
                    self.date_options.append(
                        CalendarDateOption(
                            date=str(self._current_date["date"]),
                            weekday=parts[0],
                            day_number=parts[1],
                            selected=bool(self._current_date["selected"]),
                        )
                    )
                self._current_date = None
                self._current_date_text = None
            return
        if self._current_card is None:
            return
        if tag in {"a", "button"} and self._current_field == "button_label":
            self._current_field = None
        elif tag == "div" and self._current_field is not None:
            self._current_field = None
        self._card_depth -= 1
        if tag == "div" and self._current_card is not None and self._card_depth == 0 and self._buffers:
            token = self._current_card.get("token", "")
            match = self.TOKEN_PATTERN.match(token)
            if match:
                time_text = _normalize(" ".join(self._buffers["time"]).replace("\n", " "))
                time_match = self.TIME_PATTERN.search(time_text)
                title_lines = [line for line in (_normalize(x) for x in "".join(self._buffers["title"]).split("\n")) if line]
                club_lines = [line for line in (_normalize(x) for x in "".join(self._buffers["club"]).split("\n")) if line]
                trainer = _normalize(" ".join(self._buffers["trainer"]))
                button_label = _normalize(" ".join(self._buffers.get("button_label", [])))
                duration = time_text
                if time_match:
                    duration = _normalize(time_text.replace(time_match.group(0), ""))
                self.classes.append(
                    CalendarClass(
                        index=len(self.classes) + 1,
                        token=token,
                        booking_id=match.group("booking_id"),
                        booking_center=match.group("center"),
                        title=title_lines[0] if title_lines else token,
                        date=self.selected_date,
                        start_time=time_match.group("start") if time_match else None,
                        end_time=time_match.group("end") if time_match else None,
                        duration=duration or None,
                        trainer=trainer or None,
                        club=club_lines[0] if club_lines else None,
                        room=club_lines[1] if len(club_lines) > 1 else None,
                        status=_normalize_status(button_label),
                        queue_length=_extract_count(button_label, QUEUE_LENGTH_PATTERNS),
                        available_places=_extract_count(button_label, AVAILABLE_PLACES_PATTERNS),
                        button_label=button_label or None,
                        button_href=self._current_card.get("button_href"),
                        raw={
                            "time": time_text,
                            "title": title_lines,
                            "trainer": trainer,
                            "club": club_lines,
                            "button_onclick": self._current_card.get("button_onclick", ""),
                        },
                    )
                )
            self._current_card = None
            self._current_field = None
            self._buffers = {}
            self._card_depth = 0

    def handle_data(self, data: str) -> None:
        if self._current_date_text is not None:
            self._current_date_text.append(data)
        if self._current_field is not None and self._current_field in self._buffers:
            self._buffers[self._current_field].append(data)
