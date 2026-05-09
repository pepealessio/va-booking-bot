from __future__ import annotations

import os
import unittest
from unittest.mock import patch, MagicMock

import httpx

from va_cli.notifier import (
    NullNotifier,
    TelegramNotifier,
    from_config,
)


class NullNotifierTests(unittest.TestCase):
    def test_send_does_nothing(self) -> None:
        """NullNotifier.send() should be a silent no-op."""
        n = NullNotifier()
        n.send("success", "ok")
        n.send("error", "fail")

    def test_multiple_send_no_side_effects(self) -> None:
        n = NullNotifier()
        n.send("success", "a")
        n.send("error", "b")
        n.send("success", "c")


class TelegramNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "123456:test-token"
        self.chat_id = "987654321"
        self.n = TelegramNotifier(self.token, self.chat_id)

    def test_url_construction(self) -> None:
        self.assertIn("123456:test-token", self.n._url)
        self.assertTrue(self.n._url.endswith("/sendMessage"))

    def test_send_success_includes_prefix(self) -> None:
        captured: list[dict] = []

        def fake_post(url: str, data: dict, **kw) -> httpx.Response:
            captured.append(data)
            return MagicMock()

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("success", "Booking confirmed")

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["chat_id"], self.chat_id)
        self.assertIn("✅", captured[0]["text"])
        self.assertIn("Booking confirmed", captured[0]["text"])
        self.assertEqual(captured[0]["parse_mode"], "Markdown")

    def test_send_error_includes_prefix(self) -> None:
        captured: list[dict] = []

        def fake_post(url: str, data: dict, **kw) -> httpx.Response:
            captured.append(data)
            return MagicMock()

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("error", "Booking failed")

        self.assertIn("❌", captured[0]["text"])

    def test_send_markdown_bold_survives(self) -> None:
        captured: list[dict] = []

        def fake_post(url: str, data: dict, **kw) -> httpx.Response:
            captured.append(data)
            return MagicMock()

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("success", "**Yoga** at 18:00")

        self.assertIn("**Yoga**", captured[0]["text"])

    def test_send_swallows_http_error(self) -> None:
        """A Telegram API error must never raise to the caller."""

        def fake_post(*_a, **_k):
            raise httpx.ConnectError("network down")

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("success", "should not raise")

    def test_send_swallows_generic_exception(self) -> None:
        def fake_post(*_a, **_k):
            raise RuntimeError("boom")

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("success", "should not raise")

    def test_send_unknown_level_no_prefix(self) -> None:
        captured: list[dict] = []

        def fake_post(url: str, data: dict, **kw) -> httpx.Response:
            captured.append(data)
            return MagicMock()

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("unknown", "plain msg")

        self.assertEqual(captured[0]["text"], "plain msg")

    def test_chat_id_is_sent_correctly(self) -> None:
        captured: list[dict] = []

        def fake_post(url: str, data: dict, **kw) -> httpx.Response:
            captured.append(data)
            return MagicMock()

        with patch("va_cli.notifier.httpx.post", side_effect=fake_post):
            self.n.send("success", "test")

        self.assertEqual(captured[0]["chat_id"], "987654321")


class FromConfigTests(unittest.TestCase):
    def test_none_returns_null(self) -> None:
        n = from_config(None)
        self.assertIsInstance(n, NullNotifier)

    def test_empty_dict_returns_null(self) -> None:
        n = from_config({})
        self.assertIsInstance(n, NullNotifier)

    def test_missing_token_returns_null(self) -> None:
        n = from_config({"provider": "telegram", "chat_id": "123"})
        self.assertIsInstance(n, NullNotifier)

    def test_missing_chat_id_returns_null(self) -> None:
        n = from_config({"provider": "telegram", "token": "tok"})
        self.assertIsInstance(n, NullNotifier)

    def test_missing_provider_non_telegram(self) -> None:
        n = from_config({"token": "tok", "chat_id": "123", "provider": "email"})
        self.assertIsInstance(n, NullNotifier)

    def test_valid_config_returns_telegram(self) -> None:
        cfg = {"provider": "telegram", "token": "tok", "chat_id": "42"}
        n = from_config(cfg)
        self.assertIsInstance(n, TelegramNotifier)
        self.assertEqual(n.token, "tok")
        self.assertEqual(n.chat_id, "42")

    def test_provider_default_is_telegram(self) -> None:
        cfg = {"token": "tok", "chat_id": "42"}
        n = from_config(cfg)
        self.assertIsInstance(n, TelegramNotifier)

    def test_env_token_overrides_yaml(self) -> None:
        cfg = {"token": "yaml-token", "chat_id": "yaml-chat"}
        env = {
            "VA_NOTIFY_TOKEN": "env-token",
            "VA_NOTIFY_CHAT_ID": "yaml-chat",
        }
        with patch.dict("va_cli.notifier.os.environ", env, clear=False):
            n = from_config(cfg)
        self.assertIsInstance(n, TelegramNotifier)
        self.assertEqual(n.token, "env-token")

    def test_env_chat_id_overrides_yaml(self) -> None:
        cfg = {"token": "tok", "chat_id": "yaml-chat"}
        env = {"VA_NOTIFY_CHAT_ID": "env-chat"}
        with patch.dict("va_cli.notifier.os.environ", env, clear=False):
            n = from_config(cfg)
        self.assertEqual(n.chat_id, "env-chat")

    def test_env_var_alone_works_with_partial_yaml(self) -> None:
        yaml_token = {"token": "tok"}
        env = {"VA_NOTIFY_CHAT_ID": "env-chat"}
        with patch.dict("va_cli.notifier.os.environ", env, clear=False):
            n = from_config(yaml_token)
        self.assertIsInstance(n, TelegramNotifier)
        self.assertEqual(n.chat_id, "env-chat")
