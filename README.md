# Virgin Active Italy CLI and Booking Bot

Command-line tools for Virgin Active Italy class discovery, booking, cancellation, and recurring booking automation.

> **Disclaimer**: This project is **unofficial** and depends on live Virgin Active Italy web behavior. It may stop working if the site changes its markup, auth flow, or booking endpoints. Use at your own risk.

## Installation

```bash
python -m venv venv
source venv/bin/activate
pip install git+https://github.com/pepealessio/va-booking-bot.git
va --help
```

## Getting Started

### 1. Credentials

Minimal `.env`:

```env
VA_USERNAME=you@example.com
VA_PASSWORD=your-password
```

`va login` resolves credentials in this order: `--user`/`--passwd` flags → `.env` → system keyring → interactive prompt.

```bash
va login                          # quick
va login --user you --passwd secret --save   # save to keyring
```

### 2. Find and book a class

```bash
va classes --club "Roma EUR" --date 2026-03-15
va --json classes --club "Roma EUR" --course "Yoga Calm" --day 0 --time "18:00"
va --dangerously-approve-token book 355132c220
va --dangerously-approve-token cancel 355132c220
```

The composite token format is `<bookingId>c<center>` (e.g. `355132c220`). Booking requires a saved authenticated session.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `VA_USERNAME` | — | Account email |
| `VA_PASSWORD` | — | Account password |
| `VA_STATE_DIR` | `.va_state` | State directory (session, logs) |
| `VA_TIMEOUT_SECONDS` | `20` | HTTP timeout |
| `VA_QUEUE_FULL_THRESHOLD` | `15` | Queue length for `overbooked` status |
| `VA_BOOKING_OPEN_HOURS` | `48` | Hours before class bookings open |
| `VA_NOTIFY_TOKEN` | — | Telegram Bot API token |
| `VA_NOTIFY_CHAT_ID` | — | Telegram chat ID |

See [`docs/agent-handoff.md`](docs/agent-handoff.md) for endpoint config and reverse-engineering notes.

## Usage

### `va classes`

Filters: `--course`, `--trainer`, `--club`, `--target`, `--date`, `--day` (0=Mon..6=Sun, auto-computes date), `--no-auth`, `--time`, `--from-time`, `--to-time`.

```bash
va classes
va classes --club "Roma EUR" --day 0 --time "18:00"
va --json classes --club "Roma EUR" --course "Yoga Calm"
```

Time rules: `--time` matches exact, `--from-time`/`--to-time` set bounds. Cannot combine `--time` with range flags.

Output can be a table or JSON (with `--json`). JSON uses snake_case keys, no nulls, includes `booking_id` and `booking_center`.

Statuses: `bookable`, `full`, `not_yet_open`, `queue`, `overbooked` (queue ≥ threshold), `queue_full`, `unavailable`. Remapping: `queue` with `queue_length ≥ VA_QUEUE_FULL_THRESHOLD` → `overbooked`; `full` >48h away → `not_yet_open`.

### `va debug`

```bash
va debug whoami
va debug courses
va debug trainers
va debug clubs
va debug targets
va debug dates --club "Roma EUR"
```

### `va book / cancel`

```bash
va --dangerously-approve-token book 355132c220
va --dangerously-approve-token cancel 355132c220
```

The `book` command supports `--retry N` (max attempts, default 1) and `--retry-interval S` (seconds between retries, default 5). When retries are enabled, it loops on failure and sends a Telegram notification on success or exhaustion.

### `va automate add`

Generate cron lines for recurring booking. Two modes:

**Interactive** — prompts for club, course, day, class, retry count, and retry interval:

```bash
va automate add | crontab -
```

**Non-interactive** — all flags on the command line:

```bash
va automate add --club "Roma EUR" --course "Yoga Calm" --day 0 --time "18:00" --retry 10 --retry-interval 60 | crontab -
```

| Flag | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--club` | yes* | — | Club name |
| `--day` | yes* | — | Day of week 0=Mon..6=Sun |
| `--time` | yes* | — | Class time HH:MM |
| `--course` | no | — | Course name (omit for any) |
| `--retry` | no | 10 | Max retry attempts |
| `--retry-interval` | no | 60 | Seconds between retries |

\* Required for non-interactive mode. When all three are present, interactive prompts are skipped.

Prompts, commentary, and install tips go to **stderr** — only raw cron lines reach stdout for clean piping.

Example output:

```
# Roma EUR — Yoga Calm — Monday 18:00 # va-automate:abc12345
55 17 * * 5  va login && va --dangerously-approve-token --json classes --club 'Roma EUR' --course 'Yoga Calm' --day 0 --time '18:00' | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])" > /tmp/va_booking_abc12345 # va-automate:abc12345
00 18 * * 6  va --dangerously-approve-token book $(cat /tmp/va_booking_abc12345) --retry 10 --retry-interval 60 # va-automate:abc12345
```

The **find/login line** runs 5 min before the book line (both 48 h before class). It logs in, resolves the class token via `va --json classes`, and writes it to `/tmp/va_booking_<ID>`. The **book line** reads the pre-resolved token and retries on failure.

Install (appends to existing crontab):

```bash
(crontab -l; va automate add) | crontab -
```

### `va automate list`

```bash
crontab -l | va automate list           # table
crontab -l | va automate list --json    # JSON
crontab -l | va automate list --raw     # cron lines only
```

stdin must be piped from `crontab -l`.

### `va automate remove`

```bash
crontab -l | va automate remove abc12345 | crontab -   # by ID
crontab -l | va automate remove | crontab -            # interactive
```

stdin must be piped from `crontab -l` **and** stdout must be piped to `crontab -`. The command refuses to run if either end is a terminal — this prevents accidentally printing or discarding crontab data.

### Telegram Notifications

Set `VA_NOTIFY_TOKEN` and `VA_NOTIFY_CHAT_ID` to receive push notifications on booking success or failure. See the full guide at [`docs/telegram-setup.md`](docs/telegram-setup.md).

All cron booking attempts are logged to `.va_state/automate.log`.

### Global Flags

| Flag | Meaning |
| --- | --- |
| `--json` | JSON output |
| `--debug` | Request diagnostics |
| `--dangerously-approve-token` | Skip interactive approval prompt |

## Development

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Useful files:
- [`docs/agent-handoff.md`](docs/agent-handoff.md) — reverse-engineering notes
- [`docs/telegram-setup.md`](docs/telegram-setup.md) — Telegram bot setup
- `src/va_cli/` — CLI code
- `tests/` — regression tests
