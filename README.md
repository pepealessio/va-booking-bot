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

- interactive `va automate add` with `--install` to write crontab directly or `--raw` for piping
- `va automate list` with table, JSON, and `--raw` modes
- `va automate remove <id>` for non-interactive entry removal
- `va book --retry` retry loop with Telegram notification on success/failure
- find/login cron entry resolves class token 5 min before the bell via `va --json classes`
- book cron entry reads pre-resolved token for zero-delay booking
- `va classes --day 0..6` auto-computes the next date for a day-of-week
- Telegram push notifications on booking success or failure

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

Files stored in `<VA_STATE_DIR>`:

| File | Purpose |
| --- | --- |
| `session.json` | Persisted authentication cookies |
| `automate.log` | Runtime log for recurring booking attempts |

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
venv/bin/va --json classes --course "Reformer Pilates Align" --date 2026-03-15
```

Find the next Monday's classes without knowing the exact date:

```bash
venv/bin/va --json classes --club "Roma EUR" --course "Yoga Calm" --day 0 --time "18:00"
```

Supported filters:

- `--course`
- `--trainer`
- `--club`
- `--target`
- `--date` (explicit date; overrides `--day` if both given)
- `--day` 0-6 (Mon-Sun, auto-computes next calendar date)
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

The `automate` subcommand manages cron entries for recurring class bookings. No config file is written — the cron line **is** the configuration.

#### `va automate add`

Interactively select a class and create cron entries:

```bash
va automate add
```

This walks you through selecting:

1. Club
2. Course (or any)
3. Day of week (Mon–Sun)
4. Specific class (shown for the nearest matching date)
5. Retry settings (max retries, interval in seconds — default 10 retries, 60 s)

**Install directly into crontab:**

```bash
va automate add --install
```

**Pipe to crontab (for scripting):**

```bash
va automate add --raw | crontab -
```

`--raw` prints only the cron lines with no commentary, making it pipe-friendly.

At the end it prints three cron lines (unless `--install` or `--raw` is used). Example for a Monday 18:00 Yoga class:

```
# Roma EUR — Yoga Calm — Monday 18:00 # va-automate:abc12345
55 17 * * 5  va login && va --dangerously-approve-token --json classes --club 'Roma EUR' --course 'Yoga Calm' --day 0 --time '18:00' | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])" > /tmp/va_booking_abc12345 # va-automate:abc12345
00 18 * * 6  va --dangerously-approve-token book $(cat /tmp/va_booking_abc12345) --retry 10 --retry-interval 60 # va-automate:abc12345
```

The **find/login line** runs 5 minutes before the book line (both 48 h before class, on the preceding day). It logs in, immediately resolves the class token by calling `va --json classes` with the configured filters, and writes the first result's ID to `/tmp/va_booking_<ID>`. This ensures the class is discovered well before the bell, avoiding any delay at booking time.

The **book line** reads the pre-resolved token from the tmp file and attempts to book with retry. The retry interval is short (seconds) because the class resolution is already done — only the booking call needs to go through.

#### `va automate list`

List recurring booking entries currently in your crontab:

```bash
va automate list               # table view
va automate list --json         # JSON output
va automate list --raw          # raw cron lines only (pipe-friendly)
```

#### `va automate remove`

Remove a booking entry from your crontab. Pass the entry ID non-interactively, or omit it for interactive selection:

```bash
va automate remove abc12345    # remove by ID (non-interactive)
va automate remove              # interactive selection
```

#### `va book <token> --retry`

The cron book line calls `va book` with a pre-resolved token. When `--retry` is given (> 1), it enters a retry loop:

1. Calls the Virgin Active booking API with the given token
2. On failure: sleeps `--retry-interval` seconds, retries up to `--retry` times
3. On success or exhaustion: sends Telegram notification (if configured)

Direct usage (with explicit token):

```bash
va --dangerously-approve-token book 355132c220 --retry 10 --retry-interval 5
```

| Flag | Required | Meaning |
| --- | --- | --- |
| `token` | yes | Class token in `<bookingId>c<center>` format |
| `--retry` | no | Max retry attempts (default 1, no retry) |
| `--retry-interval` | no | Seconds between retries (default 5) |

#### Telegram Notifications

When a booking succeeds or all retries are exhausted, the bot can send a push notification to your Telegram chat. See the full setup guide at [`docs/telegram-setup.md`](docs/telegram-setup.md).

Configure via environment variables:

| Variable | Meaning |
| --- | --- |
| `VA_NOTIFY_TOKEN` | Telegram Bot API token |
| `VA_NOTIFY_CHAT_ID` | Your Telegram chat ID |

Notifications are only sent on **booking success** and **fatal booking failure** (all retries exhausted). Transient intermediate retries are silent.

Example messages:

- `✅ Booking confirmed: Roma EUR/Yoga Calm @ 18:00 (attempt 1)`
- `❌ Booking failed: Roma EUR/Yoga Calm @ 18:00 after 10 attempts`

All cron booking attempts are also logged to `.va_state/automate.log`.

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
- [`docs/telegram-setup.md`](docs/telegram-setup.md): Telegram bot setup guide
- `src/va_cli/`: low-level CLI surface
- `tests/`: regression tests

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).

## Notes

- filter labels like club names are translated into the site's hidden internal values automatically
- the login flow bridges `shop.virginactive.it` and `www.virginactive.it` automatically
- the public calendar uses infinite scroll; the client follows pagination automatically
- the bot assumes the booking token resolved during preflight is still valid at booking time
- the find/login line resolves the booking token at login time (5 min before book) via `va --json classes` and saves it — the book line reads the file and books with zero class-resolution delay
