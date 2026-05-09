from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, UTC, timedelta

import httpx

from tests.fixtures import load as load_fixture
from va_cli.calendar_parser import CalendarClassParser, CalendarPageParser
from va_cli.client import VAError, VirginActiveClient
from va_cli.config import Config
from va_cli.models import CalendarClass


class ConfigBuilder:
    """Convenience builder for Config instances used in tests."""

    def __init__(self) -> None:
        self._state_dir: Path | None = None

    def with_temp_state(self, tmpdir: tempfile.TemporaryDirectory) -> "ConfigBuilder":
        self._state_dir = Path(tmpdir.name)
        return self

    def build(self) -> Config:
        state_dir = self._state_dir or Path(tempfile.gettempdir())
        return Config(
            username="member@example.com",
            password="secret",
            login_page_url="https://shop.virginactive.it/account/login",
            login_submit_url="https://shop.virginactive.it/account/login",
            login_status_url="https://www.virginactive.it/rest-api/login-status",
            calendar_page_url="https://www.virginactive.it/calendario-corsi",
            calendar_filter_url="https://www.virginactive.it/calendario-corsi/JFilter",
            integration_base_url="https://www.virginactive.it/VirginIntegrations/IntegrationPlatform",
            state_dir=state_dir,
            timeout_seconds=5,
            queue_full_threshold=999,
            booking_open_hours=999,
        )


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


class LoginTests(unittest.TestCase):
    """Tests for login, CSRF extraction, and session persistence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = ConfigBuilder().with_temp_state(self._tmp).build()

    def test_login_fetches_csrf_and_persists_cookies(self) -> None:
        """Full login path: GET page -> extract CSRF -> POST -> SSO bridge -> persist."""
        calls = {"login_status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://shop.virginactive.it/account/login" and request.method == "GET":
                return httpx.Response(
                    200,
                    text='<form><input type="hidden" name="_csrf_token" value="csrf123"></form>',
                )
            if url == "https://shop.virginactive.it/account/login" and request.method == "POST":
                return httpx.Response(
                    200,
                    text="<html>dashboard</html>",
                    headers={"set-cookie": "sessionid=abc123; Path=/; Domain=shop.virginactive.it"},
                )
            if url == "https://www.virginactive.it/rest-api/login-status":
                calls["login_status"] += 1
                return httpx.Response(200, json={"IsLoggedIn": calls["login_status"] > 1})
            if url == "https://shop.virginactive.it/account/subscriptions":
                return httpx.Response(
                    200,
                    text=(
                        '<a href="https://www.virginactive.it/loginbytokenglobal?token=abc'
                        '&amp;landingurl=https://www.virginactive.it/calendario-corsi">Calendario corsi</a>'
                    ),
                )
            if url.startswith("https://www.virginactive.it/loginbytokenglobal"):
                return httpx.Response(
                    200,
                    text="ok",
                    headers={"set-cookie": "va-site=site123; Path=/; Domain=www.virginactive.it"},
                )
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            session = client.login()

        self.assertEqual(session.last_login_url, "https://www.virginactive.it/calendario-corsi")
        saved = json.loads((Path(self._tmp.name) / "session.json").read_text(encoding="utf-8"))
        self.assertTrue(any(cookie["name"] == "sessionid" for cookie in saved["cookies"]))
        self.assertTrue(any(cookie["name"] == "va-site" for cookie in saved["cookies"]))

    def test_login_csrf_extraction_from_real_page(self) -> None:
        """Verify CSRF extraction works against the real login page fixture."""
        login_html = load_fixture("login_page.html")
        form_match = VirginActiveClient.LOGIN_FORM_PATTERN.search(login_html)
        target = form_match.group("form") if form_match else login_html
        match = VirginActiveClient.CSRF_PATTERN.search(target)
        self.assertIsNotNone(match, "Could not extract CSRF from real login page")
        self.assertTrue(len(match.group("token")) > 10, "CSRF token looks too short")

    def test_sso_link_extraction_from_real_subscriptions(self) -> None:
        """Verify SSO link extraction works against the real subscriptions page."""
        sub_html = load_fixture("subscriptions_page.html")
        client = VirginActiveClient.__new__(VirginActiveClient)
        url = client._extract_global_login_url(sub_html)
        self.assertIn("/loginbytokenglobal", url)
        self.assertIn("calendario-corsi", url)

    def test_login_failure_detection(self) -> None:
        """If POST returns the login page again, login must raise."""
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    text='<form action="/account/login"><input name="_csrf_token" value="t"></form>',
                )
            return httpx.Response(200, text="<form name='loginForm'>Ho dimenticato la password</form>")

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(VAError, msg="Login did not complete"):
                client.login()

    def test_logout_clears_session_file(self) -> None:
        session_path = Path(self._tmp.name) / "session.json"
        session_path.write_text(
            json.dumps({"cookies": [{"name": "sessionid", "value": "abc", "domain": "www.virginactive.it", "path": "/"}]}),
            encoding="utf-8",
        )
        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            self.assertTrue(client.has_saved_session())
            client.logout()
        self.assertFalse(session_path.exists())


# ---------------------------------------------------------------------------
# Calendar page filter parsing
# ---------------------------------------------------------------------------


class CalendarFiltersTests(unittest.TestCase):
    """Tests for parsing filter dropdowns from calendar page / filter fixtures."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = ConfigBuilder().with_temp_state(self._tmp).build()

    def test_get_calendar_filters_parses_options(self) -> None:
        """Dedicated filter fixture: parses courses, trainers, clubs, targets."""
        filter_html = load_fixture("calendar_filters.html")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=filter_html)

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            filters = client.get_calendar_filters()

        self.assertEqual(filters.courses[0].label, "Scegli il corso")
        self.assertEqual(filters.courses[1].value, "874c6bff-4365-4d6e-93f9-0c6ab5fbba20")
        self.assertEqual(filters.courses[1].label, "Calisthenics Performance")
        self.assertEqual(filters.trainers[1].label, "Marco Rossi")
        self.assertEqual(filters.clubs[1].label, "Roma EUR")
        # Roma EUR has a UUID value -- critical for filter resolution
        self.assertEqual(filters.clubs[1].value, "6bf52b86-7e8d-4c49-afb2-924d2e55c98e")
        self.assertEqual(filters.clubs[2].label, "Salerno")
        self.assertEqual(filters.clubs[2].value, "a10ce8aa-3832-49e2-9d09-09c697260b63")
        self.assertEqual(filters.targets[0].value, "force-id")

    def test_real_calendar_page_parses_all_filters(self) -> None:
        """Parse the real calendar page fixture and verify counts."""
        cal_html = load_fixture("calendar_page.html")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=cal_html)

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            filters = client.get_calendar_filters()

        self.assertGreater(len(filters.courses), 10, "Should have many courses")
        self.assertGreater(len(filters.trainers), 50, "Should have many trainers")
        self.assertGreater(len(filters.clubs), 10, "Should have many clubs")
        self.assertEqual(len(filters.targets), 4, "Expect 4 target buttons")

    def test_real_calendar_page_uuid_club_values(self) -> None:
        """Verify real club values are UUIDs, not plain labels."""
        cal_html = load_fixture("calendar_page.html")
        parser = CalendarPageParser()
        parser.feed(cal_html)

        # At least some clubs should have UUID values
        uuid_count = sum(1 for c in parser.filters.clubs if "-" in c.value)
        self.assertGreater(uuid_count, 0, "Should parse clubs with UUID values")

    def test_real_calendar_page_zero_padded_dates(self) -> None:
        """Real date fixture has zero-padded day numbers (e.g. '09')."""
        cal_html = load_fixture("calendar_page.html")
        parser = CalendarPageParser()
        parser.feed(cal_html)

        # Check that dates are parsed and day numbers exist
        self.assertGreater(len(parser.date_options), 0)
        selected = [d for d in parser.date_options if d.selected]
        self.assertTrue(any(d for d in selected), "Should have a selected date")


# ---------------------------------------------------------------------------
# Class card parsing -- public and authenticated
# ---------------------------------------------------------------------------


class ClassCardParsingTests(unittest.TestCase):
    """Tests for parsing class cards from JFilter HTML responses."""

    def test_jfilter_public_parses_class(self) -> None:
        """Public JFilter fixture: anchor elements with id tokens."""
        html = load_fixture("jfilter_public_roma_eur.html")
        p = CalendarClassParser(selected_date="2026-05-09")
        p.feed(html)

        self.assertEqual(len(p.classes), 4)
        first = p.classes[0]
        self.assertTrue(first.token.endswith("c220"), f"Club 220, got {first.token}")
        self.assertTrue(first.booking_id.isdigit())
        self.assertEqual(first.booking_center, "220")
        self.assertEqual(first.status, "unavailable")
        self.assertIsNotNone(first.title)
        self.assertIsNotNone(first.start_time)
        self.assertIsNotNone(first.end_time)
        self.assertIsNotNone(first.club)

    def test_jfilter_auth_parses_class(self) -> None:
        """Auth JFilter (Sat) fixture: button elements with id + onclick."""
        html = load_fixture("jfilter_auth_roma_eur.html")
        p = CalendarClassParser(selected_date="2026-05-09")
        p.feed(html)

        self.assertEqual(len(p.classes), 4)
        for cls in p.classes:
            self.assertEqual(cls.status, "bookable", f"Expected bookable, got {cls.status} for {cls.title}")
            self.assertEqual(cls.booking_center, "220")

    def test_jfilter_auth_friday_full(self) -> None:
        """Auth JFilter (Fri) fixture: full classes with <a href='#'> buttons."""
        html = load_fixture("jfilter_auth_Roma_EUR_20260512.html")
        p = CalendarClassParser(selected_date="2026-05-12")
        p.feed(html)

        self.assertEqual(len(p.classes), 5)
        for cls in p.classes:
            self.assertEqual(cls.status, "full")
            self.assertEqual(cls.button_label, "Prenotazioni non disponibili")

    def test_jfilter_auth_monday_mixed(self) -> None:
        """Auth JFilter (Mon) fixture: mix of bookable and full in one response."""
        html = load_fixture("jfilter_auth_Roma_EUR_20260513.html")
        p = CalendarClassParser(selected_date="2026-05-13")
        p.feed(html)

        self.assertEqual(len(p.classes), 5)
        statuses = {c.status for c in p.classes}
        self.assertIn("bookable", statuses)
        self.assertIn("full", statuses)

    def test_button_id_only_parsing(self) -> None:
        """Authenticated card with button id only (no onclick)."""
        html = load_fixture("jfilter_button_id_only.html")
        p = CalendarClassParser(selected_date="2026-05-14")
        p.feed(html)

        self.assertEqual(len(p.classes), 1)
        self.assertEqual(p.classes[0].token, "326999c104")
        self.assertEqual(p.classes[0].booking_id, "326999")
        self.assertEqual(p.classes[0].booking_center, "104")
        self.assertEqual(p.classes[0].button_label, "Troppo tardi")
        self.assertEqual(p.classes[0].status, "unavailable")

    def test_onclick_only_parsing(self) -> None:
        """Authenticated card with onclick only (no id token)."""
        html = load_fixture("jfilter_onclick_only.html")
        p = CalendarClassParser(selected_date="2026-05-14")
        p.feed(html)

        self.assertEqual(len(p.classes), 1)
        self.assertEqual(p.classes[0].token, "355726c220")
        self.assertEqual(p.classes[0].booking_id, "355726")
        self.assertEqual(p.classes[0].booking_center, "220")
        self.assertEqual(p.classes[0].button_label, "15 utenti in attesa")
        self.assertEqual(p.classes[0].status, "queue")
        self.assertEqual(p.classes[0].queue_length, 15)

    def test_queue_full_status(self) -> None:
        """Class with full queue label must not fall back to bookable."""
        html = load_fixture("jfilter_queue_full.html")
        p = CalendarClassParser(selected_date="2026-05-14")
        p.feed(html)

        self.assertEqual(len(p.classes), 1)
        self.assertEqual(p.classes[0].token, "355727c220")
        self.assertEqual(p.classes[0].button_label, "Lista di attesa piena")
        self.assertEqual(p.classes[0].status, "queue_full")

    def test_class_room_extraction(self) -> None:
        """Verify room is extracted from the second line of club field."""
        html = load_fixture("jfilter_public_roma_eur.html")
        p = CalendarClassParser(selected_date="2026-05-09")
        p.feed(html)

        rooms = [c.room for c in p.classes if c.room]
        self.assertGreater(len(rooms), 0, "Should parse at least one room")

    def test_all_classes_have_tokens(self) -> None:
        """Verify that all fixtures parse with valid booking tokens."""
        jfilter_fixtures = [
            "jfilter_public_roma_eur.html",
            "jfilter_auth_roma_eur.html",
            "jfilter_auth_Roma_EUR_20260512.html",
            "jfilter_auth_Roma_EUR_20260513.html",
            "jfilter_button_id_only.html",
            "jfilter_onclick_only.html",
            "jfilter_queue_full.html",
            "jfilter_pagination_page1.html",
            "jfilter_pagination_page2.html",
        ]
        for name in jfilter_fixtures:
            html = load_fixture(name)
            p = CalendarClassParser(selected_date="2026-05-14")
            p.feed(html)
            for cls in p.classes:
                self.assertIsNotNone(cls.token, f"{name}: class '{cls.title}' has no token")
                self.assertIsNotNone(cls.booking_id, f"{name}: class '{cls.title}' has no booking_id")
                self.assertIsNotNone(cls.booking_center, f"{name}: class '{cls.title}' has no center")


# ---------------------------------------------------------------------------
# Client-level integration: JFilter fetching, pagination, date defaults
# ---------------------------------------------------------------------------


class JFilterClientTests(unittest.TestCase):
    """Tests for the client's JFilter endpoint logic (pagination, dates, etc.)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = ConfigBuilder().with_temp_state(self._tmp).build()

    def test_list_classes_parses_calendar(self) -> None:
        """Basic class listing via JFilter with explicit date."""
        html = load_fixture("jfilter_public_roma_eur.html")

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("day_selected=2026-05-09", str(request.url))
            self.assertEqual(request.headers.get("X-Requested-With"), "XMLHttpRequest")
            return httpx.Response(200, json={"class_calendar": html})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"date": "2026-05-09"})

        self.assertEqual(len(items), 4)
        self.assertTrue(all(c.booking_center == "220" for c in items))

    def test_list_classes_follows_infinite_pagination(self) -> None:
        """Client fetches page=2 when first response returns 5 classes."""
        page1 = load_fixture("jfilter_pagination_page1.html")
        page2 = load_fixture("jfilter_pagination_page2.html")

        def handler(request: httpx.Request) -> httpx.Response:
            if "page=2" in str(request.url):
                return httpx.Response(200, json={"class_calendar": page2})
            return httpx.Response(200, json={"class_calendar": page1})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"date": "2026-05-14"})

        self.assertEqual(len(items), 6)
        self.assertEqual(items[0].index, 1)
        self.assertEqual(items[-1].index, 6)

    def test_list_classes_defaults_to_selected_page_date(self) -> None:
        """When no --date, client fetches calendar page to find the selected date."""
        page_calls = {"calendar": 0, "filter": 0, "page_num": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://www.virginactive.it/calendario-corsi":
                page_calls["calendar"] += 1
                return httpx.Response(
                    200,
                    text="""
                    <div class="calendarDays">
                      <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-03-13">
                        <span>VENERDI</span><span class="dayNumber">13</span>
                      </div>
                    </div>
                    """,
                )
            page_calls["filter"] += 1
            self.assertIn("day_selected=2026-03-13", url)
            # Return fewer than 5 classes so pagination stops after first page
            return httpx.Response(
                200,
                json={"class_calendar": load_fixture("jfilter_pagination_page2.html")},
            )

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({})

        self.assertEqual(page_calls["calendar"], 1)
        self.assertEqual(page_calls["filter"], 1)
        self.assertEqual(len(items), 1)

    def test_list_classes_accepts_full_html_fallback(self) -> None:
        """When JFilter returns full HTML instead of JSON, parser still works."""
        inner = load_fixture("jfilter_public_roma_eur.html")
        full_html = f"<html><body>{inner}</body></html>"

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=full_html, headers={"content-type": "text/html"})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({})

        self.assertEqual(len(items), 4)

    def test_list_classes_resolves_club_label_to_uuid_value(self) -> None:
        """Client resolves human-readable club label to UUID option value."""
        seen = {"page": 0, "filter": 0}
        filter_html = load_fixture("calendar_filters.html")

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://www.virginactive.it/calendario-corsi":
                seen["page"] += 1
                return httpx.Response(200, text=filter_html)
            seen["filter"] += 1
            self.assertIn("club_ids=6bf52b86-7e8d-4c49-afb2-924d2e55c98e", str(request.url))
            return httpx.Response(
                200,
                json={"class_calendar": load_fixture("jfilter_auth_roma_eur.html")},
            )

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"club": "Roma EUR", "date": "2026-05-09"})

        self.assertEqual(seen["page"], 1)
        self.assertEqual(seen["filter"], 1)
        self.assertEqual(len(items), 4)

    def test_get_calendar_dates_parses_day_rail(self) -> None:
        """JFilter response includes date rail which gets parsed."""
        calendar_page_html = """
        <div class="calendarDays">
          <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-05-12">
            <span>VENERDI</span><span class="dayNumber">12</span>
          </div>
        </div>
        """
        jfilter_html = """
        <div class="calendarDays">
          <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-05-12">
            <span>VENERDI</span><span class="dayNumber">12</span>
          </div>
        </div>
        <div class="classLines">
          <div class="calendarLesson classLine">
            <div class="calendarLessonOrario"><strong>08:00</strong> 08:45<br/>45 min.</div>
            <div class="calendaClassName"><strong>One</strong></div>
            <div class="calendarLessonTrainer">T1</div>
            <div class="calendarLessonClub">Club A<br/><span class="fw300">Room 1</span></div>
            <div class="calendarButton"><a class="btn btn-red" id="100001c200">Abbonati</a></div>
          </div>
        </div>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://www.virginactive.it/calendario-corsi":
                return httpx.Response(200, text=calendar_page_html)
            return httpx.Response(200, json={"class_calendar": jfilter_html})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            dates = client.get_calendar_dates({})

        self.assertEqual(dates[0].date, "2026-05-12")
        self.assertTrue(dates[0].selected)


# ---------------------------------------------------------------------------
# Authenticated listing with session
# ---------------------------------------------------------------------------


class AuthenticatedListingTests(unittest.TestCase):
    """Tests that authenticated listing establishes the www session."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = ConfigBuilder().with_temp_state(self._tmp).build()

    def _write_session(self) -> None:
        session_path = Path(self._tmp.name) / "session.json"
        session_path.write_text(
            json.dumps(
                {
                    "cookies": [{"name": "session-", "value": "abc", "domain": "shop.virginactive.it", "path": "/"}],
                    "last_login_at": "2026-03-12T00:00:00+00:00",
                    "last_login_url": "https://shop.virginactive.it/account/login",
                }
            ),
            encoding="utf-8",
        )

    def test_list_classes_use_auth_establishes_site_session(self) -> None:
        """Authenticated listing bridges shop -> www via SSO, then lists classes."""
        self._write_session()
        page1 = load_fixture("jfilter_auth_roma_eur.html")
        calls = {"login_status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://www.virginactive.it/rest-api/login-status":
                calls["login_status"] += 1
                return httpx.Response(200, json={"IsLoggedIn": calls["login_status"] > 1})
            if url == "https://shop.virginactive.it/account/subscriptions":
                return httpx.Response(
                    200,
                    text=(
                        '<a href="https://www.virginactive.it/loginbytokenglobal?token=abc'
                        '&amp;landingurl=https://www.virginactive.it/calendario-corsi">Calendario corsi</a>'
                    ),
                )
            if url.startswith("https://www.virginactive.it/loginbytokenglobal"):
                return httpx.Response(
                    200,
                    text="ok",
                    headers={"set-cookie": "va-site=site123; Path=/; Domain=www.virginactive.it"},
                )
            if url == "https://www.virginactive.it/calendario-corsi":
                return httpx.Response(
                    200,
                    text="""
                    <div class="calendarDays">
                      <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-05-09">
                        <span>SABATO</span><span class="dayNumber">09</span>
                      </div>
                    </div>
                    """,
                )
            if url.startswith("https://www.virginactive.it/calendario-corsi/JFilter"):
                return httpx.Response(200, json={"class_calendar": page1})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({}, use_auth=True, approve=lambda _: True)

        self.assertEqual(len(items), 4)
        self.assertGreaterEqual(calls["login_status"], 2)


# ---------------------------------------------------------------------------
# Booking and cancel
# ---------------------------------------------------------------------------


class BookingTests(unittest.TestCase):
    """Tests for the book/cancel endpoints and approval gate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = ConfigBuilder().with_temp_state(self._tmp).build()

    def _write_session(self) -> None:
        session_path = Path(self._tmp.name) / "session.json"
        session_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "sessionid", "value": "abc", "domain": "www.virginactive.it", "path": "/"}
                    ],
                    "last_login_at": "2026-03-12T00:00:00+00:00",
                    "last_login_url": "https://shop.virginactive.it/account/login",
                }
            ),
            encoding="utf-8",
        )

    def test_book_requires_approval_and_uses_composite_token(self) -> None:
        self._write_session()
        calls = {"login_status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://www.virginactive.it/rest-api/login-status":
                calls["login_status"] += 1
                return httpx.Response(200, json={"IsLoggedIn": calls["login_status"] > 1})
            if url == "https://shop.virginactive.it/account/subscriptions":
                return httpx.Response(
                    200,
                    text=(
                        '<a href="https://www.virginactive.it/loginbytokenglobal?token=abc'
                        '&amp;landingurl=https://www.virginactive.it/calendario-corsi">Calendario corsi</a>'
                    ),
                )
            if url.startswith("https://www.virginactive.it/loginbytokenglobal"):
                return httpx.Response(
                    200,
                    text="ok",
                    headers={"set-cookie": "va-site=site123; Path=/; Domain=www.virginactive.it"},
                )
            self.assertIn("bookingId=208239", url)
            self.assertIn("bookingCenter=232", url)
            return httpx.Response(200, json={"BookClassResult": {"StatusCode": 200, "StatusMessage": "ok"}})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            payload = client.book("208239c232", approve=lambda _: True)

        self.assertEqual(payload["StatusCode"], 200)

    def test_book_rejects_without_approval(self) -> None:
        self._write_session()

        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with self.assertRaises(VAError):
                client.book("208239c232", approve=lambda _: False)

    def test_cancel_rejects_without_approval(self) -> None:
        self._write_session()

        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with self.assertRaises(VAError):
                client.cancel("208239c232", approve=lambda _: False)

    def test_whoami_requires_approval(self) -> None:
        self._write_session()

        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with self.assertRaises(VAError):
                client.whoami(approve=lambda _: False)

    def test_book_no_session_raises(self) -> None:
        """No saved session means book / cancel / whoami fail immediately."""
        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with self.assertRaises(VAError, msg="No saved session"):
                client.book("123c456", approve=lambda _: True)

            with self.assertRaises(VAError, msg="No saved session"):
                client.cancel("123c456", approve=lambda _: True)

            with self.assertRaises(VAError, msg="No saved session"):
                client.whoami(approve=lambda _: True)


# ---------------------------------------------------------------------------
# Date rail parsing
# ---------------------------------------------------------------------------


class DateParsingTests(unittest.TestCase):
    """Tests for date rail parsing across fixtures."""

    def test_real_calendar_page_dates(self) -> None:
        """Parse dates from the real calendar page."""
        cal_html = load_fixture("calendar_page.html")
        parser = CalendarPageParser()
        parser.feed(cal_html)

        self.assertGreater(len(parser.date_options), 5)
        dates = [d.date for d in parser.date_options]
        self.assertTrue(all(len(d) == 10 for d in dates), "Dates should be YYYY-MM-DD")
        weekdays = [d.weekday for d in parser.date_options]
        self.assertTrue(all(d.isupper() for d in weekdays), "Weekdays should be uppercase")

    def test_zero_padded_day_numbers(self) -> None:
        """Single-digit days are zero-padded in the fixture."""
        cal_html = load_fixture("calendar_page.html")
        parser = CalendarPageParser()
        parser.feed(cal_html)

        # Today is 2026-05-09 (Saturday), should have day_number '09'
        selected = [d for d in parser.date_options if d.selected]
        if selected:
            self.assertEqual(selected[0].day_number, "09")


# ---------------------------------------------------------------------------
# Status remapping: overbooked and not_yet_open
# ---------------------------------------------------------------------------


class StatusRemappingTests(unittest.TestCase):
    """Tests for context-aware status overrides (overbooked, not_yet_open)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _build_config(self, threshold: int = 15, open_hours: int = 48) -> Config:
        return Config(
            username="member@example.com",
            password="secret",
            login_page_url="https://shop.virginactive.it/account/login",
            login_submit_url="https://shop.virginactive.it/account/login",
            login_status_url="https://www.virginactive.it/rest-api/login-status",
            calendar_page_url="https://www.virginactive.it/calendario-corsi",
            calendar_filter_url="https://www.virginactive.it/calendario-corsi/JFilter",
            integration_base_url="https://www.virginactive.it/VirginIntegrations/IntegrationPlatform",
            state_dir=Path(self._tmp.name),
            timeout_seconds=5,
            queue_full_threshold=threshold,
            booking_open_hours=open_hours,
        )

    def test_queue_becomes_overbooked_when_at_threshold(self) -> None:
        """Queue with queue_length >= threshold should become 'overbooked'."""
        now = datetime.now(UTC)
        open_hours = 12
        today = now.strftime("%Y-%m-%d")

        config = self._build_config(threshold=15, open_hours=open_hours)
        client = VirginActiveClient(config)
        classes = []
        c1 = CalendarClass(
            index=1,
            token="100c200",
            booking_id="100",
            booking_center="200",
            title="Yoga",
            date=today,
            start_time="23:59",
            end_time="23:59",
            status="queue",
            queue_length=15,
        )
        c2 = CalendarClass(
            index=2,
            token="200c200",
            booking_id="200",
            booking_center="200",
            title="Pilates",
            date=today,
            start_time="23:59",
            end_time="23:59",
            status="queue",
            queue_length=5,
        )
        client._remap_statuses([c1, c2])
        self.assertEqual(c1.status, "overbooked")
        self.assertEqual(c2.status, "queue")
        client.close()

    def test_queue_not_overbooked_below_threshold(self) -> None:
        """Queue below threshold stays 'queue'."""
        now = datetime.now(UTC)
        today = now.strftime("%Y-%m-%d")

        config = self._build_config(threshold=20, open_hours=12)
        client = VirginActiveClient(config)
        c = CalendarClass(
            index=1,
            token="100c200",
            booking_id="100",
            booking_center="200",
            title="Yoga",
            date=today,
            start_time="23:59",
            end_time="23:59",
            status="queue",
            queue_length=15,
        )
        client._remap_statuses([c])
        self.assertEqual(c.status, "queue")
        client.close()

    def test_full_becomes_not_yet_open_when_distant(self) -> None:
        """Full class > open_hours in future should become 'not_yet_open'."""
        now = datetime.now(UTC)
        future_date = (now + timedelta(hours=72)).strftime("%Y-%m-%d")
        future_time = "08:00"

        config = self._build_config(threshold=15, open_hours=48)
        client = VirginActiveClient(config)
        c = CalendarClass(
            index=1,
            token="100c200",
            booking_id="100",
            booking_center="200",
            title="Yoga",
            date=future_date,
            start_time=future_time,
            end_time="09:00",
            status="full",
        )
        client._remap_statuses([c])
        self.assertEqual(c.status, "not_yet_open")
        client.close()

    def test_full_stays_full_when_near(self) -> None:
        """Full class < open_hours in future stays 'full'."""
        now = datetime.now(UTC)
        near_date = (now + timedelta(hours=24)).strftime("%Y-%m-%d")
        near_time = "08:00"

        config = self._build_config(threshold=15, open_hours=48)
        client = VirginActiveClient(config)
        c = CalendarClass(
            index=1,
            token="100c200",
            booking_id="100",
            booking_center="200",
            title="Yoga",
            date=near_date,
            start_time=near_time,
            end_time="09:00",
            status="full",
        )
        client._remap_statuses([c])
        self.assertEqual(c.status, "full")
        client.close()

    def test_bookable_and_other_statuses_unchanged(self) -> None:
        """Bookable and unavailable classes are not affected by remapping."""
        now = datetime.now(UTC)
        future_date = (now + timedelta(hours=72)).strftime("%Y-%m-%d")

        config = self._build_config(threshold=15, open_hours=48)
        client = VirginActiveClient(config)
        c1 = CalendarClass(
            index=1,
            token="100c200",
            booking_id="100",
            booking_center="200",
            title="Yoga",
            date=future_date,
            start_time="08:00",
            end_time="09:00",
            status="bookable",
        )
        c2 = CalendarClass(
            index=2,
            token="200c200",
            booking_id="200",
            booking_center="200",
            title="Pilates",
            date=future_date,
            start_time="08:00",
            end_time="09:00",
            status="unavailable",
        )
        c3 = CalendarClass(
            index=3,
            token="300c200",
            booking_id="300",
            booking_center="200",
            title="Reformer",
            date=future_date,
            start_time="08:00",
            end_time="09:00",
            status="queue_full",
        )
        client._remap_statuses([c1, c2, c3])
        self.assertEqual(c1.status, "bookable")
        self.assertEqual(c2.status, "unavailable")
        self.assertEqual(c3.status, "queue_full")
        client.close()

    def test_overbooked_and_not_yet_open_via_end_to_end(self) -> None:
        """Both overrides fire together through the full client flow."""
        now = datetime.now(UTC)
        near_date = (now + timedelta(hours=24)).strftime("%Y-%m-%d")
        far_date = (now + timedelta(hours=72)).strftime("%Y-%m-%d")

        near_html = '''
        <div class="classLines">
          <div class="calendarLesson classLine">
            <div class="calendarLessonOrario"><strong>08:00</strong> 08:45<br/>45 min.</div>
            <div class="calendaClassName"><strong>Near Yoga</strong></div>
            <div class="calendarLessonClub">Club<br/><span class="fw300">R1</span></div>
            <div class="calendarButton"><a class="btn" id="100c200">Prenotazioni non disponibili</a></div>
          </div>
        </div>'''

        far_html = '''
        <div class="classLines">
          <div class="calendarLesson classLine">
            <div class="calendarLessonOrario"><strong>08:00</strong> 08:45<br/>45 min.</div>
            <div class="calendaClassName"><strong>Far Yoga</strong></div>
            <div class="calendarLessonClub">Club<br/><span class="fw300">R1</span></div>
            <div class="calendarButton"><a class="btn" id="200c200">Prenotazioni non disponibili</a></div>
          </div>
        </div>'''

        config = self._build_config(threshold=15, open_hours=48)

        with VirginActiveClient(config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"class_calendar": near_html}))) as c1:
            near_classes = c1.list_classes({"date": near_date})

        with VirginActiveClient(config, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"class_calendar": far_html}))) as c2:
            far_classes = c2.list_classes({"date": far_date})

        self.assertEqual(near_classes[0].status, "full")
        self.assertEqual(far_classes[0].status, "not_yet_open")


if __name__ == "__main__":
    unittest.main()
