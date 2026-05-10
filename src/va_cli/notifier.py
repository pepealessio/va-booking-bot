from __future__ import annotations

import os
from typing import Any

import httpx


class Notifier:
    """Protocol for sending notifications. Call `send(level, message)`."""

    def send(self, level: str, message: str) -> None: ...


class NullNotifier:
    """No-op notifier when nothing is configured."""

    def send(self, level: str, message: str) -> None:
        pass


class TelegramNotifier:
    """Sends messages via the Telegram Bot API."""

    PREFIXES = {
        "success": "✅",
        "error": "❌",
        "warning": "⚠️",
    }

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, level: str, message: str) -> None:
        prefix = self.PREFIXES.get(level, "")
        text = f"{prefix} {message}" if prefix else message
        try:
            httpx.post(
                self._url,
                data={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        except Exception:
            pass


def from_config(notify_cfg: dict[str, Any] | None) -> Notifier:
    """Build a notifier from optional config and/or env vars.

    Env vars `VA_NOTIFY_TOKEN` and `VA_NOTIFY_CHAT_ID` override any dict values
    and can be used on their own (pass ``None`` or an empty dict).
    Falls back to ``NullNotifier`` when configuration is incomplete.
    """
    token: str | None = None
    chat_id: str | None = None
    provider = "telegram"

    if notify_cfg:
        token = notify_cfg.get("token")
        chat_id = notify_cfg.get("chat_id")
        provider = notify_cfg.get("provider", "telegram").lower()

    env_token = os.environ.get("VA_NOTIFY_TOKEN")
    if env_token:
        token = env_token.strip()

    env_chat = os.environ.get("VA_NOTIFY_CHAT_ID")
    if env_chat:
        chat_id = env_chat.strip()

    if not token or not chat_id:
        return NullNotifier()

    if provider == "telegram":
        return TelegramNotifier(token, chat_id)

    return NullNotifier()
