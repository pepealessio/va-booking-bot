from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from va_cli.client import VAError, VirginActiveClient
from va_cli.config import Config


PAGE_HTML = """
<html><body>
<select class="changeFilterDropdown" name="ClassesNames" data-param="class_ids">
  <option value="">Scegli il corso</option>
  <option value="Yoga Calm">Yoga Calm</option>
  <option value="Reformer Pilates Athletic">Reformer Pilates Athletic</option>
</select>
<select class="changeFilterDropdown" name="TrainersNames" data-param="trainer_ids">
  <option value="">Scegli il trainer</option>
  <option value="Francesco De Rose">Francesco De Rose</option>
</select>
<select class="changeFilterDropdown" name="ClubsNames" data-param="club_ids">
  <option value="">Scegli il club</option>
  <option value="Roma Via Mantova">Roma Via Mantova</option>
</select>
<button class="changeFilterButton" data-param="targets_ids" data-value="force-id">Forza</button>
</body></html>
"""

CLASS_CALENDAR = """
<div class="calendar">
  <div class="calendarDays">
    <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-03-13">
      <span>VENERDI</span><span class="dayNumber">13</span>
    </div>
    <div class="calendarDay changeDay" data-param="day_selected" data-day="2026-03-14">
      <span>SABATO</span><span class="dayNumber">14</span>
    </div>
  </div>
  <div class="classLines">
    <div class="calendarLesson classLine">
      <div class="calendarLessonOrario"><strong>17:15</strong> 18:00<br/>45 min.</div>
      <div class="calendaClassName"><strong>Yoga Calm</strong></div>
      <div class="calendarLessonTrainer">Francesco De Rose</div>
      <div class="calendarLessonClub">Roma Via Mantova<br/><span class="fw300">Studio Active</span></div>
      <div class="calendarButton"><a class="btn btn-red" href="/account/login" id="208239c232"><span></span>Abbonati</a></div>
    </div>
  </div>
</div>
"""

AUTH_CLASS_CALENDAR = """
<div class="calendar">
  <div class="calendarDays">
    <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-03-12">
      <span>GIOVEDI</span><span class="dayNumber">12</span>
    </div>
  </div>
  <div class="classLines">
    <div class="calendarLesson classLine">
      <div class="calendarLessonImage"><img src="https://example.test/x.jpg" alt="Sculpt" /></div>
      <div class="calendarLessonOrario flex-grow-1"><strong>18:00</strong> 18:45<br/>45 min.</div>
      <div class="calendaClassName flex-grow-1">
        <strong>Sculpt</strong>
        <div class="calendaClassMobileDet">
          Mario Di Pasquale
          <div>
            <strong>Firenze Rovezzano</strong>
            <br />
            <span class="fw300">Studio Active</span>
          </div>
        </div>
      </div>
      <div class="calendarLessonTrainer">Mario Di Pasquale</div>
      <div class="calendarLessonClub flex-grow-1">Firenze Rovezzano<br/><span class="fw300">Studio Active</span></div>
      <div class="calendarButton"><button type="button" class="btn btn-bordered-grey" id="326999c104"><span></span>Troppo tardi</button></div>
    </div>
  </div>
</div>
"""

AUTH_ONCLICK_CLASS_CALENDAR = """
<div class="calendar">
  <div class="calendarDays">
    <div class="calendarDay changeDay selected" data-param="day_selected" data-day="2026-03-13">
      <span>VENERDI</span><span class="dayNumber">13</span>
    </div>
  </div>
  <div class="classLines">
    <div class="calendarLesson classLine">
      <div class="calendarLessonOrario flex-grow-1"><strong>07:15</strong> 08:00<br/>45 min.</div>
      <div class="calendaClassName flex-grow-1">
        <strong>Lift Club</strong>
        <div class="calendaClassMobileDet">
          Simone Nicolini
          <div>
            <strong>Roma EUR</strong>
            <br />
            <span class="fw300">Lift Area</span>
          </div>
        </div>
      </div>
      <div class="calendarLessonTrainer">Simone Nicolini</div>
      <div class="calendarLessonClub flex-grow-1">Roma EUR<br/><span class="fw300">Lift Area</span></div>
      <div class="calendarButton"><button type="button" class="btn btn-red btb-wait" onclick="bookClass(355726,220)"><span class="btn-icon btn-clock-icon"></span>15 utenti in attesa</button></div>
    </div>
  </div>
</div>
"""

FULL_PAGE_HTML = f"<html><body>{CLASS_CALENDAR}</body></html>"
SECOND_PAGE_CLASS_CALENDAR = """
<div class="calendar">
  <div class="classLines">
    <div class="calendarLesson classLine">
      <div class="calendarLessonOrario"><strong>18:15</strong> 19:00<br/>45 min.</div>
      <div class="calendaClassName"><strong>Pilates Athletic</strong></div>
      <div class="calendarLessonTrainer">Luca Dona</div>
      <div class="calendarLessonClub">Venezia Mestre<br/><span class="fw300">Studio Yoga</span></div>
      <div class="calendarButton"><a class="btn btn-red" href="/account/login" id="285042c213"><span></span>Abbonati</a></div>
    </div>
  </div>
</div>
"""

FIRST_PAGE_FIVE_CLASS_CALENDAR = """
<div class="calendar">
  <div class="classLines">
    <div class="calendarLesson classLine"><div class="calendarLessonOrario"><strong>08:00</strong> 08:45<br/>45 min.</div><div class="calendaClassName"><strong>One</strong></div><div class="calendarLessonTrainer">T1</div><div class="calendarLessonClub">Club A<br/><span class="fw300">Room 1</span></div><div class="calendarButton"><a class="btn btn-red" id="100001c200">Abbonati</a></div></div>
    <div class="calendarLesson classLine"><div class="calendarLessonOrario"><strong>09:00</strong> 09:45<br/>45 min.</div><div class="calendaClassName"><strong>Two</strong></div><div class="calendarLessonTrainer">T2</div><div class="calendarLessonClub">Club A<br/><span class="fw300">Room 2</span></div><div class="calendarButton"><a class="btn btn-red" id="100002c200">Abbonati</a></div></div>
    <div class="calendarLesson classLine"><div class="calendarLessonOrario"><strong>10:00</strong> 10:45<br/>45 min.</div><div class="calendaClassName"><strong>Three</strong></div><div class="calendarLessonTrainer">T3</div><div class="calendarLessonClub">Club A<br/><span class="fw300">Room 3</span></div><div class="calendarButton"><a class="btn btn-red" id="100003c200">Abbonati</a></div></div>
    <div class="calendarLesson classLine"><div class="calendarLessonOrario"><strong>11:00</strong> 11:45<br/>45 min.</div><div class="calendaClassName"><strong>Four</strong></div><div class="calendarLessonTrainer">T4</div><div class="calendarLessonClub">Club A<br/><span class="fw300">Room 4</span></div><div class="calendarButton"><a class="btn btn-red" id="100004c200">Abbonati</a></div></div>
    <div class="calendarLesson classLine"><div class="calendarLessonOrario"><strong>12:00</strong> 12:45<br/>45 min.</div><div class="calendaClassName"><strong>Five</strong></div><div class="calendarLessonTrainer">T5</div><div class="calendarLessonClub">Club A<br/><span class="fw300">Room 5</span></div><div class="calendarButton"><a class="btn btn-red" id="100005c200">Abbonati</a></div></div>
  </div>
</div>
"""


class VirginActiveClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config = Config(
            username="member@example.com",
            password="secret",
            login_page_url="https://shop.virginactive.it/account/login",
            login_submit_url="https://shop.virginactive.it/account/login",
            login_status_url="https://www.virginactive.it/rest-api/login-status",
            calendar_page_url="https://www.virginactive.it/calendario-corsi",
            calendar_filter_url="https://www.virginactive.it/calendario-corsi/JFilter",
            integration_base_url="https://www.virginactive.it/VirginIntegrations/IntegrationPlatform",
            state_dir=Path(self.temp_dir.name),
            timeout_seconds=5,
        )

    def test_login_fetches_csrf_and_persists_cookies(self) -> None:
        calls = {"login_status": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://shop.virginactive.it/account/login" and request.method == "GET":
                return httpx.Response(
                    200,
                    text='<form><input type="hidden" name="_csrf_token" value="csrf123"></form>',
                )
            if url == "https://shop.virginactive.it/account/login" and request.method == "POST":
                headers = {"set-cookie": "sessionid=abc123; Path=/; Domain=shop.virginactive.it"}
                return httpx.Response(200, text="<html>dashboard</html>", headers=headers)
            if url == "https://www.virginactive.it/rest-api/login-status":
                calls["login_status"] += 1
                payload = {"IsLoggedIn": calls["login_status"] > 1}
                return httpx.Response(200, json=payload)
            if url == "https://shop.virginactive.it/account/subscriptions":
                return httpx.Response(
                    200,
                    text=(
                        '<a href="https://www.virginactive.it/loginbytokenglobal?token=abc'
                        '&amp;landingurl=https://www.virginactive.it/calendario-corsi">Calendario corsi</a>'
                    ),
                )
            if url.startswith("https://www.virginactive.it/loginbytokenglobal"):
                headers = {"set-cookie": "va-site=site123; Path=/; Domain=www.virginactive.it"}
                return httpx.Response(200, text="ok", headers=headers)
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            session = client.login()

        self.assertEqual(session.last_login_url, "https://www.virginactive.it/calendario-corsi")
        saved = json.loads((Path(self.temp_dir.name) / "session.json").read_text(encoding="utf-8"))
        self.assertTrue(any(cookie["name"] == "sessionid" for cookie in saved["cookies"]))
        self.assertTrue(any(cookie["name"] == "va-site" for cookie in saved["cookies"]))

    def test_get_calendar_filters_parses_options(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=PAGE_HTML)

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            filters = client.get_calendar_filters()

        self.assertEqual(filters.courses[0].label, "Scegli il corso")
        self.assertEqual(filters.courses[1].value, "Yoga Calm")
        self.assertEqual(filters.trainers[1].label, "Francesco De Rose")
        self.assertEqual(filters.clubs[1].label, "Roma Via Mantova")
        self.assertEqual(filters.targets[0].value, "force-id")

    def test_list_classes_parses_calendar_html(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("day_selected=2026-03-13", str(request.url))
            self.assertEqual(request.headers.get("X-Requested-With"), "XMLHttpRequest")
            return httpx.Response(200, json={"class_calendar": CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"date": "2026-03-13"})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].booking_id, "208239")
        self.assertEqual(items[0].booking_center, "232")
        self.assertEqual(items[0].title, "Yoga Calm")
        self.assertEqual(items[0].room, "Studio Active")
        self.assertEqual(items[0].status, "unavailable")

    def test_list_classes_follows_infinite_pagination(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "page=2" in url:
                return httpx.Response(200, json={"class_calendar": SECOND_PAGE_CLASS_CALENDAR})
            return httpx.Response(200, json={"class_calendar": FIRST_PAGE_FIVE_CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"date": "2026-03-13"})

        self.assertEqual(len(items), 6)
        self.assertEqual(items[0].index, 1)
        self.assertEqual(items[-1].index, 6)
        self.assertEqual(items[-1].title, "Pilates Athletic")

    def test_get_calendar_dates_parses_day_rail(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"class_calendar": CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            dates = client.get_calendar_dates({})

        self.assertEqual(dates[0].date, "2026-03-13")
        self.assertTrue(dates[0].selected)
        self.assertEqual(dates[1].weekday, "SABATO")

    def test_list_classes_defaults_to_selected_page_date(self) -> None:
        calls = {"page": 0, "filter": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == "https://www.virginactive.it/calendario-corsi":
                calls["page"] += 1
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
            calls["filter"] += 1
            self.assertIn("day_selected=2026-03-13", url)
            return httpx.Response(200, json={"class_calendar": CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({})

        self.assertEqual(calls["page"], 1)
        self.assertEqual(calls["filter"], 1)
        self.assertEqual(len(items), 1)

    def test_list_classes_accepts_full_html_fallback(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=FULL_PAGE_HTML, headers={"content-type": "text/html"})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Yoga Calm")

    def test_list_classes_resolves_club_label_to_value(self) -> None:
        seen = {"page": 0, "filter": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://www.virginactive.it/calendario-corsi":
                seen["page"] += 1
                return httpx.Response(
                    200,
                    text="""
                    <select class="changeFilterDropdown" name="ClubsNames" data-param="club_ids">
                      <option value="club-uuid-1">Roma EUR</option>
                    </select>
                    """,
                )
            seen["filter"] += 1
            self.assertIn("club_ids=club-uuid-1", str(request.url))
            return httpx.Response(200, json={"class_calendar": CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"club": "Roma EUR"})

        self.assertEqual(seen["page"], 2)
        self.assertEqual(seen["filter"], 1)
        self.assertEqual(items[0].club, "Roma Via Mantova")

    def test_list_classes_parses_authenticated_button_markup(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("day_selected=2026-03-12", str(request.url))
            return httpx.Response(200, json={"class_calendar": AUTH_CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"date": "2026-03-12"})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].token, "326999c104")
        self.assertEqual(items[0].title, "Sculpt")
        self.assertEqual(items[0].trainer, "Mario Di Pasquale")
        self.assertEqual(items[0].club, "Firenze Rovezzano")
        self.assertEqual(items[0].room, "Studio Active")
        self.assertEqual(items[0].button_label, "Troppo tardi")
        self.assertEqual(items[0].status, "unavailable")

    def test_list_classes_parses_onclick_booking_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("day_selected=2026-03-13", str(request.url))
            return httpx.Response(200, json={"class_calendar": AUTH_ONCLICK_CLASS_CALENDAR})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({"date": "2026-03-13"})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].token, "355726c220")
        self.assertEqual(items[0].booking_id, "355726")
        self.assertEqual(items[0].booking_center, "220")
        self.assertEqual(items[0].button_label, "15 utenti in attesa")
        self.assertEqual(items[0].status, "queue")

    def test_logout_clears_session_file(self) -> None:
        session_path = Path(self.temp_dir.name) / "session.json"
        session_path.write_text(
            json.dumps({"cookies": [{"name": "sessionid", "value": "abc", "domain": "www.virginactive.it", "path": "/"}]}),
            encoding="utf-8",
        )
        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            self.assertTrue(client.has_saved_session())
            client.logout()
        self.assertFalse(session_path.exists())

    def test_book_requires_approval_and_uses_composite_token(self) -> None:
        session_path = Path(self.temp_dir.name) / "session.json"
        session_path.write_text(
            json.dumps(
                {
                    "cookies": [{"name": "sessionid", "value": "abc", "domain": "www.virginactive.it", "path": "/"}],
                    "last_login_at": "2026-03-12T00:00:00+00:00",
                    "last_login_url": "https://shop.virginactive.it/account/login",
                }
            ),
            encoding="utf-8",
        )
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
                headers = {"set-cookie": "va-site=site123; Path=/; Domain=www.virginactive.it"}
                return httpx.Response(200, text="ok", headers=headers)
            self.assertIn("bookingId=208239", url)
            self.assertIn("bookingCenter=232", url)
            return httpx.Response(200, json={"BookClassResult": {"StatusCode": 200, "StatusMessage": "ok"}})

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            payload = client.book("208239c232", approve=lambda _: True)

        self.assertEqual(payload["StatusCode"], 200)

    def test_book_rejects_without_approval(self) -> None:
        session_path = Path(self.temp_dir.name) / "session.json"
        session_path.write_text(
            json.dumps(
                {
                    "cookies": [{"name": "sessionid", "value": "abc", "domain": "www.virginactive.it", "path": "/"}],
                    "last_login_at": "2026-03-12T00:00:00+00:00",
                    "last_login_url": "https://shop.virginactive.it/account/login",
                }
            ),
            encoding="utf-8",
        )

        with VirginActiveClient(self.config, transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            with self.assertRaises(VAError):
                client.book("208239c232", approve=lambda _: False)

    def test_list_classes_use_auth_establishes_site_session(self) -> None:
        session_path = Path(self.temp_dir.name) / "session.json"
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
                headers = {"set-cookie": "va-site=site123; Path=/; Domain=www.virginactive.it"}
                return httpx.Response(200, text="ok", headers=headers)
            if url == "https://www.virginactive.it/calendario-corsi":
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
            if url.startswith("https://www.virginactive.it/calendario-corsi/JFilter"):
                return httpx.Response(200, json={"class_calendar": CLASS_CALENDAR})
            raise AssertionError(f"unexpected request: {request.method} {request.url}")

        with VirginActiveClient(self.config, transport=httpx.MockTransport(handler)) as client:
            items = client.list_classes({}, use_auth=True, approve=lambda _: True)

        self.assertEqual(len(items), 1)
        self.assertGreaterEqual(calls["login_status"], 2)


if __name__ == "__main__":
    unittest.main()
