# Telegram Notification Setup

How to create a Telegram bot and wire it up for booking notifications.

## 1. Create your bot with BotFather

1. Open Telegram, search for **@BotFather** and open the chat
2. Send `` `/newbot` ``
3. Give your bot a display name (e.g. `VA Booking Bot`)
4. Give your bot a unique username — it **must end in `bot`** (e.g. `va_booking_mybot`)
5. BotFather will reply with an HTTP API token that looks like:

```
123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

**Copy and save this token** — you'll need it in the next step.

## 2. Start a chat with your bot

1. Search for your new bot by the username you chose (e.g. `@va_booking_mybot`)
2. Open the chat and tap **Start** (or send `` `/start` ``)

This is the chat where you'll receive booking notifications.

## 3. Find your chat ID

You need your personal chat ID so the bot knows where to send messages.

**Option A — ask a helper bot**

1. Open Telegram and search for **@userinfobot**
2. Send `` `/info` ``
3. Your chat ID is the number under `id:` (e.g. `987654321`)

**Option B — use curl**

First send any message to your custom bot, then run:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "
import sys, json
msgs = json.load(sys.stdin)['result']
for m in msgs:
    if 'message' in m:
        print(m['message']['chat']['id'])
        break
"
```

Replace `<TOKEN>` with your bot token from step 1.

## 4. Configure the bot

Set these environment variables (e.g. in your shell profile or `.env`):

```env
VA_NOTIFY_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
VA_NOTIFY_CHAT_ID="987654321"
```

## What do I get notified about?

Only two events trigger a Telegram message (no spam from transient retries):

- **Booking success** — `✅ Booking confirmed: <club>/<course> @ <time>`
- **Booking failure** — `❌ Booking failed: <club>/<course> @ <time>` (after all retries exhausted)

## Verify the connection

```bash
curl -s -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=<CHAT_ID> \
  -d "text=test message"
```

You should see `{"ok":true,"result":{...}}`. If not, double-check the token and chat ID.

## Disable notifications

Unset or remove the `VA_NOTIFY_TOKEN` / `VA_NOTIFY_CHAT_ID` environment variables.
