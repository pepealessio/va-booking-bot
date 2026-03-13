from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from va_bot.config import BotConfigError, load_config, parse_config


class BotConfigTests(unittest.TestCase):
    def test_parse_config_normalizes_rule(self) -> None:
        config = parse_config(
            {
                "rules": [
                    {
                        "name": "thursday-eur",
                        "club": "Roma EUR",
                        "course": "Reformer",
                        "weekday": "Thursday",
                        "time": "18:5",
                    }
                ]
            }
        )
        self.assertEqual(config.timezone, "Europe/Rome")
        self.assertEqual(config.rules[0].weekday, "thursday")
        self.assertEqual(config.rules[0].time, "18:05")

    def test_parse_config_rejects_duplicate_names(self) -> None:
        with self.assertRaises(BotConfigError):
            parse_config(
                {
                    "rules": [
                        {"name": "same", "club": "A", "course": "C", "weekday": "monday", "time": "10:00"},
                        {"name": "same", "club": "B", "course": "D", "weekday": "tuesday", "time": "11:00"},
                    ]
                }
            )

    def test_load_config_requires_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(BotConfigError):
                load_config(Path(temp_dir) / "missing.yml")


if __name__ == "__main__":
    unittest.main()
