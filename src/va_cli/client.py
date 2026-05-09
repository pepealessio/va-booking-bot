from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import quote_plus
from collections.abc import Callable
from typing import Any

import httpx

from .calendar_parser import CalendarClassParser, CalendarPageParser
from .config import Config
from .models import CalendarClass, CalendarDateOption, CalendarFilters, SessionState, utc_now
from .session import SessionStore


class VAError(RuntimeError):
    """Raised when the CLI cannot complete a Virgin Active action."""


ApprovalCallback = Callable[[str], bool]


class VirginActiveClient:
    PAGE_SIZE = 5
    MAX_CLASS_PAGES = 100
    CSRF_PATTERN = re.compile(
        r'name="_csrf_token"\s+value="(?P<token>[^"]+)"',
        re.IGNORECASE,
    )
    LOGIN_FORM_PATTERN = re.compile(
        r'<form[^>]+action="/account/login"[^>]*>(?P<form>.*?)</form>',
        re.IGNORECASE | re.DOTALL,
    )
    GLOBAL_LOGIN_LINK_PATTERN = re.compile(
        r'href="(?P<href>https://www\.virginactive\.it/loginbytokenglobal[^"]+landingurl=https://www\.virginactive\.it/calendario-corsi[^"]*)"',
        re.IGNORECASE,
    )
    BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    AJAX_HEADERS = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.virginactive.it/calendario-corsi",
    }

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        verbose: bool = False,
    ) -> None:
        self.config = config
        self.verbose = verbose
        self.store = SessionStore(config.session_path)
        base_headers = dict(self.BROWSER_HEADERS)
        self.public_http = httpx.Client(
            follow_redirects=True,
            timeout=config.timeout_seconds,
            transport=transport,
            headers=base_headers,
        )
        self.auth_http = httpx.Client(
            follow_redirects=True,
            timeout=config.timeout_seconds,
            transport=transport,
            headers=base_headers,
        )
        self._restore_session()

    def close(self) -> None:
        self.public_http.close()
        self.auth_http.close()

    def __enter__(self) -> "VirginActiveClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def login(self) -> SessionState:
        if not self.config.username or not self.config.password:
            raise VAError("Missing VA_USERNAME or VA_PASSWORD.")
        self.auth_http.cookies.clear()
        login_page = self.auth_http.get(self.config.login_page_url)
        self._log_response(login_page, "GET login page")
        login_page.raise_for_status()
        csrf_token = self._extract_csrf_token(login_page.text)
        payload = {
            "_csrf_token": csrf_token,
            "username": self.config.username,
            "password": self.config.password,
        }
        response = self.auth_http.post(
            self.config.login_submit_url,
            data=payload,
            headers={
                "Origin": "https://shop.virginactive.it",
                "Referer": self.config.login_page_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self._log_response(response, "POST login")
        response.raise_for_status()
        if self._looks_like_login_page(response.text):
            raise VAError("Login did not complete successfully; still on login page.")
        self._ensure_site_session()
        session = SessionState(
            cookies=_cookies_to_list(self.auth_http.cookies),
            last_login_at=utc_now(),
            last_login_url=self.config.calendar_page_url,
        )
        self.store.save(session)
        return session

    def logout(self) -> None:
        self.auth_http.cookies.clear()
        self.public_http.cookies.clear()
        if self.config.session_path.exists():
            self.config.session_path.unlink()

    def has_saved_session(self) -> bool:
        return bool(self.store.load().cookies)

    def whoami(self, *, approve: ApprovalCallback | None = None) -> dict[str, Any]:
        self._require_approved_session(
            "inspect your authenticated Virgin Active session",
            approve,
        )
        response = self.auth_http.get(self._require_url(self.config.login_status_url, "VA_LOGIN_STATUS_URL"))
        self._log_response(response, "GET login status")
        response.raise_for_status()
        payload = self._decode_payload(response)
        return payload if isinstance(payload, dict) else {"raw": payload}

    def get_calendar_filters(self) -> CalendarFilters:
        parser = self._fetch_calendar_page_parser(use_auth=False)
        return parser.filters

    def get_calendar_dates(self, filters: dict[str, str | None] | None = None) -> list[CalendarDateOption]:
        payload = self._fetch_calendar_payload(filters or {}, use_auth=False)
        return payload["dates"]

    def list_classes(
        self,
        filters: dict[str, str | None],
        *,
        use_auth: bool = False,
        approve: ApprovalCallback | None = None,
    ) -> list[CalendarClass]:
        payload = self._fetch_calendar_payload(filters, use_auth=use_auth, approve=approve)
        return payload["classes"]

    def book(
        self,
        token_or_booking_id: str,
        *,
        center: str | None = None,
        approve: ApprovalCallback | None = None,
    ) -> dict[str, Any]:
        self._require_approved_session("book a Virgin Active class", approve)
        self._ensure_site_session()
        booking_id, booking_center = self._split_booking_token(token_or_booking_id, center)
        url = f"{self.config.integration_base_url}/BookClass"
        response = self.auth_http.get(
            url,
            params={"bookingId": booking_id, "bookingCenter": booking_center},
        )
        self._log_response(response, "GET book class")
        response.raise_for_status()
        payload = self._decode_payload(response)
        return self._normalize_integration_payload(payload, "BookClassResult")

    def cancel(
        self,
        token_or_booking_id: str,
        *,
        center: str | None = None,
        approve: ApprovalCallback | None = None,
    ) -> dict[str, Any]:
        self._require_approved_session("cancel a Virgin Active class booking", approve)
        self._ensure_site_session()
        booking_id, booking_center = self._split_booking_token(token_or_booking_id, center)
        url = f"{self.config.integration_base_url}/UnbookClass"
        response = self.auth_http.get(
            url,
            params={"bookingId": booking_id, "bookingCenter": booking_center},
        )
        self._log_response(response, "GET unbook class")
        response.raise_for_status()
        payload = self._decode_payload(response)
        return self._normalize_integration_payload(payload, "UnbookClassResult")

    def _fetch_calendar_payload(
        self,
        filters: dict[str, str | None],
        *,
        use_auth: bool,
        approve: ApprovalCallback | None = None,
    ) -> dict[str, Any]:
        if use_auth:
            self._require_approved_session(
                "load your authenticated Virgin Active class availability",
                approve,
            )
            self._ensure_site_session()
        params = self._build_calendar_params(filters)
        client = self.auth_http if use_auth else self.public_http
        classes: list[CalendarClass] = []
        dates: list[CalendarDateOption] = []
        page: int | None = None

        # The website uses infinite scroll and keeps fetching page=2, page=3, ...
        # after the initial JFilter response whenever a page contains 5 classes.
        for _ in range(self.MAX_CLASS_PAGES):
            page_params = dict(params)
            if page is not None:
                page_params["page"] = str(page)
            parsed = self._fetch_calendar_page_payload(
                client,
                page_params,
                selected_date=params.get("day_selected"),
            )
            if not dates:
                dates = parsed["dates"]
            page_classes = parsed["classes"]
            if not page_classes:
                break
            classes.extend(page_classes)
            if len(page_classes) < self.PAGE_SIZE:
                break
            page = 2 if page is None else page + 1

        for index, item in enumerate(classes, start=1):
            item.index = index
        return {"classes": classes, "dates": dates}

    def _build_calendar_params(self, filters: dict[str, str | None]) -> dict[str, str]:
        resolved = self._resolve_filter_values(filters)
        params = {"club_ids": resolved.get("club") or ""}
        if resolved.get("course"):
            params["class_ids"] = resolved["course"] or ""
        if resolved.get("trainer"):
            params["trainer_ids"] = resolved["trainer"] or ""
        selected_date = filters.get("date") or self._get_default_calendar_date()
        if selected_date:
            params["day_selected"] = selected_date
        if filters.get("target"):
            params["targets_ids"] = filters["target"] or ""
        return params

    def _fetch_calendar_page_payload(
        self,
        client: httpx.Client,
        params: dict[str, str],
        *,
        selected_date: str | None,
    ) -> dict[str, Any]:
        response = client.get(
            self.config.calendar_filter_url,
            params=params,
            headers=self.AJAX_HEADERS,
        )
        self._log_response(response, "GET calendar filter")
        response.raise_for_status()
        payload = self._decode_payload(response)
        if isinstance(payload, dict) and "class_calendar" in payload:
            calendar_html = str(payload["class_calendar"])
        elif isinstance(payload, str) and "<html" in payload.lower():
            calendar_html = payload
        else:
            raise VAError("Unexpected calendar payload; expected class_calendar HTML.")
        parser = CalendarClassParser(selected_date=selected_date)
        parser.feed(calendar_html)
        self._remap_statuses(parser.classes)
        return {"classes": parser.classes, "dates": parser.date_options}

    def _resolve_filter_values(self, filters: dict[str, str | None]) -> dict[str, str | None]:
        if not any(filters.get(key) for key in ("course", "trainer", "club")):
            return filters
        options = self.get_calendar_filters()
        return {
            "course": self._resolve_option_value(filters.get("course"), options.courses),
            "trainer": self._resolve_option_value(filters.get("trainer"), options.trainers),
            "club": self._resolve_option_value(filters.get("club"), options.clubs),
            "date": filters.get("date"),
            "target": filters.get("target"),
        }

    def _resolve_option_value(self, provided: str | None, options) -> str | None:
        if not provided:
            return None
        lowered = provided.casefold()
        for option in options:
            if option.value == provided or option.label == provided:
                return option.value
            if option.label.casefold() == lowered:
                return option.value
        return provided

    def _fetch_calendar_page_parser(self, *, use_auth: bool) -> CalendarPageParser:
        client = self.auth_http if use_auth else self.public_http
        response = client.get(self.config.calendar_page_url)
        self._log_response(response, "GET calendar page")
        response.raise_for_status()
        parser = CalendarPageParser()
        parser.feed(response.text)
        return parser

    def _get_default_calendar_date(self) -> str | None:
        parser = self._fetch_calendar_page_parser(use_auth=False)
        for option in parser.date_options:
            if option.selected:
                return option.date
        return parser.date_options[0].date if parser.date_options else None

    def _require_approved_session(
        self,
        purpose: str,
        approve: ApprovalCallback | None,
    ) -> None:
        state = self.store.load()
        if not state.cookies:
            raise VAError("No saved authenticated session found. Run `va login` first.")
        if approve is not None and not approve(purpose):
            raise VAError("Token use was not approved.")

    def _restore_session(self) -> None:
        state = self.store.load()
        for cookie in state.cookies:
            self.auth_http.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )

    def _ensure_site_session(self) -> None:
        login_status_url = self._require_url(self.config.login_status_url, "VA_LOGIN_STATUS_URL")
        status_response = self.auth_http.get(login_status_url)
        self._log_response(status_response, "GET login status")
        status_response.raise_for_status()
        payload = self._decode_payload(status_response)
        if isinstance(payload, dict) and payload.get("IsLoggedIn") is True:
            return
        subscriptions = self.auth_http.get("https://shop.virginactive.it/account/subscriptions")
        self._log_response(subscriptions, "GET subscriptions")
        subscriptions.raise_for_status()
        global_login_url = self._extract_global_login_url(subscriptions.text)
        bridge = self.auth_http.get(global_login_url)
        self._log_response(bridge, "GET global login bridge")
        bridge.raise_for_status()
        final_status = self.auth_http.get(login_status_url)
        self._log_response(final_status, "GET login status")
        final_status.raise_for_status()
        final_payload = self._decode_payload(final_status)
        if not isinstance(final_payload, dict) or final_payload.get("IsLoggedIn") is not True:
            raise VAError("Authenticated site session could not be established for calendario-corsi.")
        self.store.save(
            SessionState(
                cookies=_cookies_to_list(self.auth_http.cookies),
                last_login_at=utc_now(),
                last_login_url=self.config.calendar_page_url,
            )
        )

    def _extract_csrf_token(self, html: str) -> str:
        form_match = self.LOGIN_FORM_PATTERN.search(html)
        target = form_match.group("form") if form_match else html
        match = self.CSRF_PATTERN.search(target)
        if not match:
            raise VAError("Could not find login CSRF token.")
        return match.group("token")

    def _extract_global_login_url(self, html: str) -> str:
        match = self.GLOBAL_LOGIN_LINK_PATTERN.search(html)
        if not match:
            raise VAError("Could not find the calendar SSO link in the subscriptions page.")
        return unescape(match.group("href"))

    def _looks_like_login_page(self, html: str) -> bool:
        markers = (
            'name="loginForm"',
            'action="/account/login"',
            "Ho dimenticato la password",
        )
        return any(marker in html for marker in markers)

    def _remap_statuses(self, classes: list[CalendarClass]) -> None:
        """Apply context-aware status overrides based on config thresholds and class timing.

        1. Overbooked: queue classes with queue_length >= threshold become 'overbooked'.
        2. Not yet open: 'full' classes whose booking window hasn't opened yet become 'not_yet_open'.
        """
        threshold = self.config.queue_full_threshold
        open_hours = self.config.booking_open_hours
        cutoff = datetime.now(UTC) + timedelta(hours=open_hours)

        for cls in classes:
            # High queue → overbooked
            if cls.status == "queue" and cls.queue_length is not None and cls.queue_length >= threshold:
                cls.status = "overbooked"

            # Full but booking not yet open → not_yet_open
            if cls.status == "full" and cls.date and cls.start_time:
                try:
                    dt_str = f"{cls.date} {cls.start_time}"
                    class_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                    if class_dt > cutoff:
                        cls.status = "not_yet_open"
                except ValueError:
                    continue

    def _require_url(self, value: str | None, env_name: str) -> str:
        if not value:
            raise VAError(f"{env_name} is not configured.")
        return value

    def _decode_payload(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return response.json()
        try:
            return response.json()
        except json.JSONDecodeError:
            return response.text

    def _normalize_integration_payload(self, payload: Any, key: str) -> dict[str, Any]:
        if isinstance(payload, dict) and key in payload and isinstance(payload[key], dict):
            return payload[key]
        if isinstance(payload, dict):
            return payload
        return {"raw": payload}

    def _split_booking_token(self, token_or_booking_id: str, center: str | None) -> tuple[str, str]:
        if center:
            return token_or_booking_id, center
        match = re.match(r"(?P<booking_id>\d+)c(?P<center>\d+)$", token_or_booking_id)
        if not match:
            raise VAError("Booking center is required unless the token is in '<bookingId>c<center>' format.")
        return match.group("booking_id"), match.group("center")

    def _log_response(self, response: httpx.Response, label: str) -> None:
        if not self.verbose:
            return
        request = response.request
        safe_body = None
        print(f"[verbose] {label}: {request.method} {request.url} -> {response.status_code}")
        try:
            body = request.content
        except httpx.RequestNotRead:
            body = b""
        if body:
            safe_body = body.decode(errors="replace")
            for secret in (
                self.config.password,
                self.config.username,
                quote_plus(self.config.password or ""),
                quote_plus(self.config.username or ""),
            ):
                if secret:
                    safe_body = safe_body.replace(secret, "***")
        if safe_body:
            print(f"[verbose] request body: {safe_body}")


def _cookies_to_list(jar: httpx.Cookies) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for cookie in jar.jar:
        items.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
        )
    return items
