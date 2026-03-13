# Virgin Active CLI Agent Handoff

This document explains how the current CLI talks to Virgin Active Italy, why the code is structured the way it is, and which production quirks forced the current implementation.

It is written for another coding agent, not for an end user.

## Scope

The CLI implements four distinct capabilities:

1. Log in to `shop.virginactive.it` with username/password.
2. Reuse that login to establish an authenticated session on `www.virginactive.it`.
3. Read filter metadata and class listings from `https://www.virginactive.it/calendario-corsi`.
4. Book and unbook classes through the Virgin Active integration endpoints.

The current implementation lives mainly in:

- [src/va_cli/client.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/client.py)
- [src/va_cli/calendar_parser.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/calendar_parser.py)
- [src/va_cli/cli.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/cli.py)
- [src/va_cli/config.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/config.py)
- [src/va_cli/credentials.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/credentials.py)
- [src/va_cli/session.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/session.py)
- [tests/test_client.py](/home/alessiopepe/projects/va-book-bot/tests/test_client.py)

## High-Level Architecture

`VirginActiveClient` owns two `httpx.Client` instances:

- `public_http`: for public calendar reads that must not consume the saved authenticated session.
- `auth_http`: for login, session restoration, authenticated listing, booking, and cancel.

Session state is persisted as cookies in `.va_state/session.json` via `SessionStore`. The file stores:

- cookie list
- `last_login_at`
- `last_login_url`

No token is stored separately. The source of truth is the cookie jar.

Saved credentials are separate from session cookies and are stored in the system keyring via `CredentialStore`.

Credential precedence in the current CLI is:

1. `va login --user ... --passwd ...`
2. `VA_USERNAME` / `VA_PASSWORD`
3. system keyring
4. interactive prompt

## Config

Defined in [src/va_cli/config.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/config.py).

Important environment variables:

- `VA_USERNAME`
- `VA_PASSWORD`
- `VA_STATE_DIR`
- `VA_LOGIN_PAGE_URL`
- `VA_LOGIN_SUBMIT_URL`
- `VA_LOGIN_STATUS_URL`
- `VA_CALENDAR_PAGE_URL`
- `VA_CALENDAR_FILTER_URL`
- `VA_INTEGRATION_BASE_URL`
- `VA_TIMEOUT_SECONDS`

Defaults point at the production Italy site:

- login page: `https://shop.virginactive.it/account/login`
- login status: `https://www.virginactive.it/rest-api/login-status`
- calendar page: `https://www.virginactive.it/calendario-corsi`
- calendar filter: `https://www.virginactive.it/calendario-corsi/JFilter`
- integration base: `https://www.virginactive.it/VirginIntegrations/IntegrationPlatform`

## Authentication Flow

### Step 1: Shop Login

Implemented in `VirginActiveClient.login()`.

Sequence:

1. Clear any stale `auth_http` cookies before login.
2. `GET https://shop.virginactive.it/account/login`
3. Extract `_csrf_token` from the login form only.
4. `POST https://shop.virginactive.it/account/login` with:
   - `_csrf_token`
   - `username`
   - `password`
5. Detect failure by checking whether the returned HTML still looks like the login page.

Important production details:

- `403` happened until the client started sending browser-like headers.
- The POST also needed `Origin` and `Referer`.
- The first CSRF token on the page was not reliable; extraction had to be scoped to the login form.
- Verbose logging must redact both raw and URL-encoded credentials.

### Step 2: Bridge Shop Session to WWW Session

This is the non-obvious part.

Logging into `shop.virginactive.it` is not enough to become logged in on `www.virginactive.it`. The calendar booking endpoints depend on the `www` session.

Implemented in `VirginActiveClient._ensure_site_session()`.

Sequence:

1. `GET https://www.virginactive.it/rest-api/login-status`
2. If `IsLoggedIn` is already `true`, stop.
3. Otherwise request `GET https://shop.virginactive.it/account/subscriptions`
4. Parse the page HTML and extract a `loginbytokenglobal` link that lands on `https://www.virginactive.it/calendario-corsi`
5. `GET` that extracted bridge URL
6. Re-check `GET https://www.virginactive.it/rest-api/login-status`
7. If `IsLoggedIn` is now `true`, save the updated combined cookie jar

Why this exists:

- The shop site contains a "Calendario corsi" link that looks like:
  `https://www.virginactive.it/loginbytokenglobal?token=...&landingurl=https://www.virginactive.it/calendario-corsi`
- That request is the SSO bridge from the e-commerce member area to the main site.

Without this step:

- `whoami` on `www` returns `IsLoggedIn: false`
- authenticated `classes`
- `book`
- `cancel`

all fail or behave like the public site.

## Public Calendar Data Model

### Main Public Page

`GET https://www.virginactive.it/calendario-corsi`

This page contains:

- filter dropdowns for:
  - `class_ids`
  - `trainer_ids`
  - `club_ids`
- target buttons for `targets_ids`
- the visible date rail

These are parsed by `CalendarPageParser`.

### Important Filter Detail

The visible label is not always the submitted value.

Example:

- label: `Roma EUR`
- submitted value: `6bf52b86-7e8d-4c49-afb2-924d2e55c98e`

The CLI accepts visible labels on the command line, but `VirginActiveClient._resolve_filter_values()` maps them back to the site’s real option values before calling `JFilter`.

If another agent removes this translation, club filtering will silently break.

## Class Listing Endpoint

### Endpoint

`GET https://www.virginactive.it/calendario-corsi/JFilter`

Expected query params:

- `club_ids`
- `class_ids`
- `trainer_ids`
- `day_selected`
- `targets_ids`

### Required Headers

The client sends AJAX-style headers:

- `X-Requested-With: XMLHttpRequest`
- `Accept: application/json, text/javascript, */*; q=0.01`
- `Referer: https://www.virginactive.it/calendario-corsi`

Without this, the site may return a full page instead of the AJAX payload.

### Response Shapes

The endpoint is inconsistent. It may return either:

1. JSON with `class_calendar`
2. Full HTML page

The client handles both in `_fetch_calendar_payload()`.

### Required Date Behavior

`day_selected` must effectively be treated as required.

Observed behavior:

- calling `JFilter` without `day_selected` may return a shell page or an unhelpful payload
- calling `JFilter` with `day_selected=YYYY-MM-DD` returns the real class cards

Because of this, `_build_calendar_params()` defaults the date if the user does not pass `--date`.

Defaulting logic:

1. fetch the public calendar page
2. parse the selected date from the day rail
3. use that value as `day_selected`
4. if no selected day exists, use the first visible date

This fallback is important to keep `va classes` useful with no explicit date.

### Infinite Scroll Pagination

`JFilter` does not return the full result set in one response. The first response contains up to 5 classes. The site JavaScript then keeps requesting:

- initial request: no `page`
- second request: `page=2`
- third request: `page=3`

and so on.

This behavior is implemented in the site bundle `list-class-filter-bundle` and mirrored in `VirginActiveClient._fetch_calendar_payload()`.

Current client logic:

- fetch first page
- if it returns 5 classes, fetch `page=2`
- continue while each page returns at least 5 classes
- stop when a page returns fewer than 5 classes or zero classes

If another agent removes this loop, `va classes` will truncate at 5 results.

## HTML Parsing Details

Implemented in [src/va_cli/calendar_parser.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/calendar_parser.py).

### `CalendarPageParser`

Parses:

- course dropdown options
- trainer dropdown options
- club dropdown options
- target buttons
- available date rail

### `CalendarClassParser`

Parses:

- date rail from the AJAX payload
- class cards with `calendarLesson classLine`
- start/end time
- duration
- title
- trainer
- club
- room
- booking token
- button label

### Production Parsing Quirks

There were three separate live HTML shapes that matter.

#### Shape 1: Public unauthenticated cards

Booking action often appears as an anchor with an `id` like:

`id="208239c232"`

That token is directly parseable as:

- `booking_id = 208239`
- `center = 232`

#### Shape 2: Authenticated cards with direct button id

Some authenticated cards use:

`<button id="326999c104">Troppo tardi</button>`

This is the same token format but on a button instead of an anchor.

#### Shape 3: Authenticated cards with `onclick`

Some authenticated cards, especially filtered club results such as `Roma EUR`, do not expose the token as an `id` at all.

Instead they use:

`onclick="bookClass(355726,220)"`

or potentially:

`onclick="unbookClass(...)"`.

`CalendarClassParser` therefore supports two token extraction strategies:

1. parse `id="<bookingId>c<center>"`
2. if missing, parse `onclick="bookClass(bookingId, center)"` or `unbookClass(...)` and synthesize the token as `<bookingId>c<center>`

If this fallback is removed, authenticated club-filtered results can look empty even though the server returned class cards.

### Parser Depth Handling

The parser tracks nested `<div>` depth for:

- the date rail entries
- each class card

This is necessary because the HTML inside a class card is nested and cannot be finalized on the first closing `</div>`.

Earlier versions finalized too early and lost most cards.

## Booking and Cancel APIs

### Endpoints

- book: `GET {integration_base_url}/BookClass`
- cancel: `GET {integration_base_url}/UnbookClass`

With the default config:

- `https://www.virginactive.it/VirginIntegrations/IntegrationPlatform/BookClass`
- `https://www.virginactive.it/VirginIntegrations/IntegrationPlatform/UnbookClass`

### Query Params

- `bookingId`
- `bookingCenter`

### CLI Token Format

The CLI accepts a composite token:

`<bookingId>c<center>`

Example:

`355132c220`

This is split by `_split_booking_token()` into:

- `booking_id = 355132`
- `center = 220`

Current CLI behavior only accepts the composite token on `book` and `cancel`. The split `--center` form was removed at the CLI layer for simplicity.

### Live Button Semantics

`button_label` reflects the server-side state the site rendered, for example:

- `Abbonati`
- `Prenota`
- `Troppo tardi`
- `15 utenti in attesa`

The CLI now normalizes these labels into a stable public status field:

- `bookable`
- `queue`
- `unavailable`

Current mapping:

- labels containing `Prenota` -> `bookable`
- labels containing `attesa` -> `queue`
- everything else -> `unavailable`

The raw button text is still kept in `button_label` and `raw`.

## Automatic Booking Model

The current CLI is sufficient to support an external scheduler, but it does not contain a scheduler itself.

For unattended booking, the intended execution model is:

1. run `va login` ahead of time to establish and persist the authenticated cookie jar
2. shortly before the booking window opens, run authenticated `va classes` for the exact club/date/time to discover the target class and composite token
3. at the exact opening timestamp, run `va book <bookingId>c<center>`

Example:

- class start: `2026-03-15 18:00` in `Europe/Rome`
- booking-open instant: `2026-03-13 18:00` in `Europe/Rome`
- pre-flight discovery can happen slightly earlier, for example at `2026-03-13 17:58`

Important assumptions and boundaries:

- the opening instant should be interpreted in `Europe/Rome`
- only the `book` call needs to happen exactly at the boundary; login, session checks, and class discovery can happen earlier
- unattended flows must pass `--dangerously-approve-token`, otherwise the CLI will block waiting for interactive approval
- the current implementation assumes a token discovered during pre-flight is still usable at the opening instant
- if that assumption stops holding in production, the automation strategy must change to fetch `classes` again immediately before `book`

Operational guidance for another agent:

- do not guess the token; always resolve it from `va classes`
- if pre-flight does not return the expected class, stop and re-query rather than booking a nearby match
- expect the first booking attempt at the boundary to occasionally be slightly early from the server's perspective; a short retry window is reasonable
- a practical retry strategy is to retry for 5 to 15 seconds on "too early" style responses or transient network failures
- session freshness is not guaranteed indefinitely; a scheduler should be prepared to refresh login before the trigger if the saved session is no longer valid

What the current docs and tests prove:

- `classes` can surface a composite booking token even when the rendered state is not currently bookable
- authenticated listing re-establishes the `www` session when needed before reading classes
- `book` splits and submits the composite token directly to `BookClass`

What is still not guaranteed by the code contract:

- that Virgin Active will always expose the final booking token before the 48-hour boundary
- that the token remains stable across the pre-flight and trigger phases
- that the integration payload always uses a stable machine-readable error schema for early-booking failures

## Approval Boundary

The user explicitly required approval before any action that uses their authenticated session.

Current implementation:

- public reads do not require approval
- authenticated reads and writes do

Approval is enforced by `_require_approved_session()`.

The CLI layer implements:

- interactive prompt by default
- `--dangerously-approve-token` to bypass the prompt

Operations that require approval:

- `debug whoami`
- `classes` when a saved session exists
- `book`
- `cancel`

## CLI Surface

Defined in [src/va_cli/cli.py](/home/alessiopepe/projects/va-book-bot/src/va_cli/cli.py).

Current commands:

- `va login`
- `va classes`
- `va book`
- `va cancel`
- `va logout`
- `va debug whoami`
- `va debug courses`
- `va debug trainers`
- `va debug clubs`
- `va debug targets`
- `va debug dates`

Notable flags:

- `--json`
- `--debug`
- `--dangerously-approve-token`
- `--no-auth` on `classes`

Important CLI behavior:

- `va classes` uses the saved authenticated session by default when one exists
- `va classes --no-auth` forces the public calendar path even if a saved session exists
- plain `va classes` output is rendered as a table with humanized column headers
- the displayed class identifier is the composite ID, currently labeled as `ID` in the table output

## Known Debugging History

These are the main issues that were discovered and fixed while reverse-engineering the flow.

### Login 403

Cause:

- request looked too bot-like

Fix:

- browser-like default headers
- `Origin`
- `Referer`

### Verbose Logger Crash

Cause:

- trying to access unread streaming request content on GET requests

Fix:

- catch `httpx.RequestNotRead`

### Login Looked Successful but Stayed on the Login Page

Cause:

- wrong CSRF token was being extracted

Fix:

- scope CSRF extraction to the login form

### Logged In on Shop, Logged Out on WWW

Cause:

- calendar site uses a separate `www` authenticated session

Fix:

- follow the `loginbytokenglobal` SSO bridge from the subscriptions page

### Empty Results with No Date

Cause:

- `JFilter` behavior without `day_selected` is inconsistent and often not useful

Fix:

- always send a selected date, defaulted from the calendar page

### Empty Authenticated Results for Some Clubs

Cause:

- authenticated cards used `onclick="bookClass(...)"` rather than an `id`

Fix:

- parse both token formats

### Parsed Zero Cards Even Though the Server Returned Cards

Cause:

- parser finalized cards too early on nested closing divs

Fix:

- track depth and finalize only when the outer class card closes

## What To Check First If It Breaks Again

If the live site changes, inspect these in order:

1. login page form markup and CSRF field
2. presence and shape of the `loginbytokenglobal` link on `/account/subscriptions`
3. `rest-api/login-status` JSON contract
4. `JFilter` request params and response shape
5. class card booking token location:
   - `id`
   - `onclick`
   - some future attribute
6. button label text changes

The fastest smoke tests are:

1. `va login`
2. `va debug whoami`
3. `va debug clubs`
4. `va classes --club "Roma EUR" --date YYYY-MM-DD`
5. `va classes --club "Roma EUR" --date YYYY-MM-DD` with saved session approval

`Roma EUR` is a useful regression case because it previously exposed the `onclick` token bug.

## Test Coverage

Regression tests currently cover:

- login + CSRF + session persistence
- SSO bridge to `www`
- filter parsing
- date parsing
- public class card parsing
- authenticated button-id parsing
- authenticated `onclick` token parsing
- label-to-value mapping for club filters
- authenticated list flow establishing the `www` session
- `--no-auth` forcing public listing
- booking token split and approval behavior

Run:

```bash
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -v
```

## Safe Extension Points

If another agent needs to extend the project, the least risky places are:

- add normalized booking-state classification in `CalendarClass`
- add a dedicated `bookings list` if a stable endpoint is identified
- add richer machine-readable error mapping for `BookClass` and `UnbookClass`
- expand class-status normalization if the site introduces more states

The riskiest areas to refactor are:

- auth/session bridging
- HTML parsing assumptions
- silent changes to query parameter resolution for filters
