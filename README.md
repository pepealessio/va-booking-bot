# Virgin Active Italy CLI and Booking Bot

Command-line tools for Virgin Active Italy class discovery, booking, cancellation, and recurring booking automation.

This repository currently ships two tools:

- `va`: the low-level CLI for login, class discovery, booking, cancellation, and debugging
- `va-bot`: a higher-level recurring booking bot built on top of the same internal client logic

The project talks directly to the Virgin Active Italy web endpoints used by the public calendar and member booking flow.

## Features

### `va`

- login with CLI flags, `.env`, keyring, or interactive prompts
- saved session reuse
- public or authenticated class discovery
- filter support for club, course, trainer, target, date, and time
- direct booking and cancellation by composite class token
- debug commands for auth checks and filter discovery

### `va-bot`

- YAML-based recurring booking rules
- interactive config creation with live data selectors
- edit/delete/create flow for existing bot configs
- config validation against live visible classes
- long-running runner for automatic booking exactly 48 hours before class start
- bot-level debug logs for scheduling, auth recovery, preflight, retries, and booking

## Important Disclaimer

This project is unofficial and depends on live Virgin Active Italy web behavior. It may stop working if the site changes its markup, auth flow, or booking endpoints.

Use it at your own risk and review the code before running unattended automation with your account.

## Installation

```bash
python -m venv venv
venv/bin/pip install -e .
```

Available commands:

```bash
venv/bin/va --help
venv/bin/va-bot --help
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

### 5. Create and run a recurring bot

```bash
venv/bin/va-bot init --config va-bot.yml
venv/bin/va-bot validate --config va-bot.yml
venv/bin/va-bot plan --config va-bot.yml
venv/bin/va-bot --debug run --config va-bot.yml
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
| `VA_STATE_DIR` | `.va_state` in the current working directory | Directory used to store local runtime state such as sessions and bot state. |

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
- JSON mode prints machine-readable objects
- class identifiers are the composite token form `<bookingId>c<center>`, for example `355132c220`

Status values:

- `bookable`
- `queue`
- `unavailable`

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

### Global Flags

| Flag | Meaning |
| --- | --- |
| `--json` | Print JSON instead of plain text output. |
| `--debug` | Print request and troubleshooting diagnostics. |
| `--dangerously-approve-token` | Skip the interactive approval prompt for authenticated actions. |

## `va-bot` Usage

`va-bot` is the recurring automation layer. It keeps the raw `va` commands low level and adds bot-specific workflows.

### Interactive Config

```bash
venv/bin/va-bot init --config va-bot.yml
```

What `init` does:

- uses live Virgin Active data to guide rule creation
- lets you select club, weekday, sample visible date, and class occurrence
- supports Up/Down + Enter in a normal terminal
- falls back to numbered prompts in non-interactive environments
- if the config already exists, it opens in maintenance mode so you can create, edit, delete, and save rules

### Validate

```bash
venv/bin/va-bot validate --config va-bot.yml
```

Validation checks:

- the config file is valid
- each enabled rule matches a visible live date for the configured weekday
- each enabled rule resolves to exactly one matching class for that date and time

### Plan

```bash
venv/bin/va-bot plan --config va-bot.yml
```

Shows:

- next class start
- booking-open instant
- preflight time

### Run

```bash
venv/bin/va-bot --debug run --config va-bot.yml
```

Runner behavior:

- computes the next booking window for each enabled rule
- preflights shortly before booking opens
- attempts booking exactly 48 hours before class start
- retries briefly for retryable errors
- automatically reuses or refreshes saved login state when possible
- stores bot runtime state inside `VA_STATE_DIR`

Debug mode shows:

- next scheduled action and wait time
- chunked wait loop progress
- auth/session recovery
- preflight token resolution
- booking attempts, retries, and final outcome

### Bot Config Example

```yaml
timezone: Europe/Rome
preflight_minutes: 2
retry_window_seconds: 15
retry_interval_seconds: 1
rules:
  - name: upper-body-roma-eur-sun-1600
    club: Roma EUR
    course: Upper Body
    weekday: sunday
    time: "16:00"
```

## Automation Notes

For a class starting at `2026-03-15 18:00` in `Europe/Rome`, the booking attempt should start at `2026-03-13 18:00` in `Europe/Rome`.

Practical guidance:

- run the bot in `Europe/Rome`
- use a machine that stays awake and keeps correct time
- on a Raspberry Pi or other always-on Linux box, `va-bot run` is a reasonable deployment model
- the runner now waits in chunks rather than one long sleep, so it rechecks the clock continuously

For Android or laptop use, process sleep/suspend is still a bigger risk than on an always-on Raspberry Pi.

## Development

Run the test suite:

```bash
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -v
```

Useful files:

- [`docs/agent-handoff.md`](docs/agent-handoff.md): reverse-engineering notes and production quirks
- `src/va_cli/`: low-level CLI surface
- `src/va_bot/`: recurring booking bot
- `tests/`: regression tests

## Notes

- filter labels like club names are translated into the site’s hidden internal values automatically
- the login flow bridges `shop.virginactive.it` and `www.virginactive.it` automatically
- the public calendar uses infinite scroll; the client follows pagination automatically
- the bot assumes the booking token resolved during preflight is still valid at booking time
