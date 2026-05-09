# Virgin Active Italy CLI and Booking Bot

Command-line tools for Virgin Active Italy class discovery, booking, cancellation, and recurring booking automation.

This repository contains one tool:

- `va`: the low-level CLI for login, class discovery, booking, cancellation, and debugging

The project talks directly to the Virgin Active Italy web endpoints used by the public calendar and member booking flow.

## Features

### `va`

- login with CLI flags, `.env`, keyring, or interactive prompts
- saved session reuse
- public or authenticated class discovery
- filter support for club, course, trainer, target, date, and time
- direct booking and cancellation by composite class token
- debug commands for auth checks and filter discovery

### `va automate` — recurring booking automation

- interactive class selection (club, course, day of week, time)
- automatic crontab scheduling (login 5 min before, book at opening, both 48 h before class)
- per-class retry logic with configurable max attempts and interval
- session recovery on expiry during cron runs
- Telegram push notifications on booking success or failure
- configuration via `.va_state/automate.yaml`

## Important Disclaimer

This project is unofficial and depends on live Virgin Active Italy web behavior. It may stop working if the site changes its markup, auth flow, or booking endpoints.

Use it at your own risk and review the code before running unattended automation with your account.

## Installation

```bash
python -m venv venv
venv/bin/pip install -e .
```

Available command:

```bash
venv/bin/va --help
```

## Quick Start

### 1. Set credentials

Minimal `.env`:

```env
VA_USERNAME=you@example.com
VA_PASSWORD=your-password
```

`va login` resolves credentials in this order:

1. `--user` and `--passwd`
2. `.env` via `VA_USERNAME` and `VA_PASSWORD`
3. system keyring
4. interactive prompt

### 2. Log in

```bash
venv/bin/va login
```

Or save credentials to the keyring:

```bash
venv/bin/va login --user you@example.com --passwd secret --save
```

### 3. Find classes

```bash
venv/bin/va classes --club "Roma EUR" --date 2026-03-15
venv/bin/va classes --club "Roma EUR" --date 2026-03-15 --time 18:00
venv/bin/va --json classes --club "Roma EUR" --date 2026-03-15
```

### 4. Book a class

```bash
venv/bin/va --dangerously-approve-token book 355132c220
```

## Configuration

The shared runtime uses these environment variables.

### Credentials

| Variable | Default | Meaning |
| --- | --- | --- |
| `VA_USERNAME` | unset | Username used when explicit CLI credentials are not passed. |
| `VA_PASSWORD` | unset | Password used when explicit CLI credentials are not passed. |

### Local State

| Variable | Default | Meaning |
| --- | --- | --- |
| `VA_STATE_DIR` | `.va_state` in the current working directory | Directory used to store local runtime state such as sessions. |

Session file:

```text
<VA_STATE_DIR>/session.json
```

### HTTP Endpoints

These normally do not need to be changed.

| Variable | Default |
| --- | --- |
| `VA_LOGIN_PAGE_URL` | `https://shop.virginactive.it/account/login` |
| `VA_LOGIN_SUBMIT_URL` | `https://shop.virginactive.it/account/login` |
| `VA_LOGIN_STATUS_URL` | `https://www.virginactive.it/rest-api/login-status` |
| `VA_CALENDAR_PAGE_URL` | `https://www.virginactive.it/calendario-corsi` |
| `VA_CALENDAR_FILTER_URL` | `https://www.virginactive.it/calendario-corsi/JFilter` |
| `VA_INTEGRATION_BASE_URL` | `https://www.virginactive.it/VirginIntegrations/IntegrationPlatform` |

### Runtime

| Variable | Default | Meaning |
| --- | --- | --- |
| `VA_TIMEOUT_SECONDS` | `20` | HTTP timeout used by the internal clients. |
| `VA_QUEUE_FULL_THRESHOLD` | `15` | Queue length at or above which a class is marked `overbooked` instead of `queue`. |
| `VA_BOOKING_OPEN_HOURS` | `48` | Hours before a class that bookings open. Classes older than this window showing `full` are remapped to `not_yet_open`. |

## `va` Usage

### Login

```bash
venv/bin/va login
venv/bin/va login --user you@example.com --passwd secret --save
venv/bin/va --debug login
```

Behavior:

- `.env` is read-only input; the CLI never writes back to it
- `--save` stores credentials in the system keyring after a successful login
- `va logout` clears both the saved session and saved keyring credentials

### Classes

Examples:

```bash
venv/bin/va classes
venv/bin/va classes --club "Roma EUR" --date 2026-03-15
venv/bin/va classes --club "Roma EUR" --date 2026-03-15 --time 18:00
venv/bin/va classes --club "Roma EUR" --date 2026-03-15 --from-time 18:00 --to-time 20:00
venv/bin/va classes --no-auth --club "Roma EUR" --date 2026-03-15
venv/bin/va --json classes --course "Reformer Pilates Align" --date 2026-03-15
```

Supported filters:

- `--course`
- `--trainer`
- `--club`
- `--target`
- `--date`
- `--no-auth`
- `--time`
- `--from-time`
- `--to-time`

Time filter rules:

- `--time HH:MM` matches classes with that exact start time
- `--from-time HH:MM` keeps classes starting at or after that time
- `--to-time HH:MM` keeps classes starting at or before that time
- `--time` cannot be combined with `--from-time` or `--to-time`

Output:

- plain mode prints a table
- JSON mode prints machine-readable objects with snake_case keys; null fields are omitted
- class identifiers are the composite token form `<bookingId>c<center>`, for example `355132c220`
- JSON objects include `booking_id` and `booking_center` as separate fields for automation scripting
- `debug whoami` JSON keys are converted from CamelCase to snake_case (`IsLoggedIn` → `is_logged_in`)

Status values:

- `bookable` — class can be booked
- `full` — class is sold out (booking window open)
- `not_yet_open` — `Prenotazioni non disponibili` displayed but booking window not yet open (>48h before class)
- `queue` — waitlist position available
- `overbooked` — queue length meets or exceeds the `VA_QUEUE_FULL_THRESHOLD` (default 15); waitlist is effectively not useful
- `queue_full` — waitlist is full (e.g. "lista di attesa piena")
- `unavailable` — any other state (e.g. "Troppo tardi")

Status remapping is applied after parsing:

- A class with `queue` status and `queue_length >= VA_QUEUE_FULL_THRESHOLD` is remapped to `overbooked`.
- A class with `full` status whose start time is more than `VA_BOOKING_OPEN_HOURS` in the future is remapped to `not_yet_open`. This disambiguates distant classes where the booking window hasn't opened yet from genuinely sold-out classes.

Authenticated behavior:

- if a saved session exists, `va classes` uses it by default
- `--no-auth` forces public calendar mode
- if no saved session exists, `va classes` falls back to the public calendar
- using the saved authenticated session requires approval unless `--dangerously-approve-token` is passed

### Book and Cancel

```bash
venv/bin/va --dangerously-approve-token book 355132c220
venv/bin/va --dangerously-approve-token cancel 355132c220
```

Notes:

- booking requires a valid saved authenticated session
- the only accepted identifier is the composite token format `<bookingId>c<center>`

### Debug Commands

```bash
venv/bin/va debug whoami
venv/bin/va debug courses
venv/bin/va debug trainers
venv/bin/va debug clubs
venv/bin/va debug targets
venv/bin/va debug dates --club "Roma EUR"
```

### Automate — Recurring Booking

The `automate` subcommand lets you configure recurring class bookings that run automatically via cron.

**Add a recurring class (interactive):**

```bash
venv/bin/va automate add
```

This walks you through selecting:

1. Club
2. Course (or any)
3. Day of week (Mon–Sun)
4. Specific class (shown for the nearest matching date)
5. Retry settings (max retries, interval in seconds — default 10 retries, 60 s)

At the end it shows the computed cron schedule and offers to install crontab entries.

**Manage configured classes:**

```bash
venv/bin/va automate list                  # show all recurring classes + cron times
venv/bin/va automate remove <class_id>     # remove a class from config
```

**Crontab management:**

```bash
venv/bin/va automate schedule              # install or update crontab entries
venv/bin/va automate unschedule            # remove all automation entries from crontab
```

Each class generates two cron entries:

- **Login** — 5 minutes before the booking attempt (fresh session)
- **Book** — exactly 48 hours before the class starts

Example: a Monday 18:00 class → login Saturday 17:55, book Saturday 18:00.

**Retry behavior:**

The booking worker retries on failure (HTTP error, session expiry, non-200 response). Each retry re-searches for the class and attempts booking. Per-class settings (`max_retries`, `retry_interval`) control how long it keeps trying. All attempts are logged to `.va_state/automate.log`.

If the session expires mid-retry, the worker re-logs in and tries again.

**Internal cron workers** (called by crontab, not meant for manual use):

```bash
venv/bin/va automate cron-login            # performs login, saves session
venv/bin/va automate cron-book --class <id> # finds class, books with retry loop
```

#### Telegram Notifications

When a booking succeeds or all retries are exhausted, the bot can send a push notification to your Telegram chat. See the full setup guide at [`docs/telegram-setup.md`](docs/telegram-setup.md).

Add the `notify` section to `.va_state/automate.yaml`:

```yaml
workdir: /home/user/va-booking-bot
notify:
  provider: telegram
  token: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
  chat_id: "987654321"
classes:
  - id: abc123
    club: "Roma EUR"
    course: "Yoga Calm"
    day_of_week: 1
    time: "18:00"
    max_retries: 10
    retry_interval: 60
```

Keep secrets out of the YAML by using env vars instead:

| Variable | Overwrites |
| --- | --- |
| `VA_NOTIFY_TOKEN` | `notify.token` |
| `VA_NOTIFY_CHAT_ID` | `notify.chat_id` |

Notifications are only sent on **booking success** and **fatal booking failure** (all retries exhausted). Transient intermediate retries are silent.

Example messages:

- `✅ Booking confirmed: Roma EUR/Yoga Calm @ 18:00 (attempt 1)`
- `❌ Booking failed: Roma EUR/Yoga Calm @ 18:00 after 10 attempts`
- `❌ Login failed: Missing VA_USERNAME or VA_PASSWORD.`

#### Configuration File

All automation config is stored in `.va_state/automate.yaml`. The `workdir` is auto-detected and used by cron entries to `cd` into the correct directory before running `va`.

### Global Flags

| Flag | Meaning |
| --- | --- |
| `--json` | Print JSON instead of plain text output. |
| `--debug` | Print request and troubleshooting diagnostics. |
| `--dangerously-approve-token` | Skip the interactive approval prompt for authenticated actions. |

## Development

Run the test suite:

```bash
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -v
```

Useful files:

- [`docs/agent-handoff.md`](docs/agent-handoff.md): reverse-engineering notes and production quirks
- `src/va_cli/`: low-level CLI surface
- `tests/`: regression tests

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## Notes

- filter labels like club names are translated into the site’s hidden internal values automatically
- the login flow bridges `shop.virginactive.it` and `www.virginactive.it` automatically
- the public calendar uses infinite scroll; the client follows pagination automatically
- the bot assumes the booking token resolved during preflight is still valid at booking time
