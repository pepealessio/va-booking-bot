from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _default_state_dir() -> Path:
    return Path.cwd() / ".va_state"


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(slots=True)
class Config:
    username: str | None
    password: str | None
    login_page_url: str
    login_submit_url: str
    login_status_url: str | None
    calendar_page_url: str
    calendar_filter_url: str
    integration_base_url: str
    state_dir: Path
    timeout_seconds: float

    @property
    def session_path(self) -> Path:
        return self.state_dir / "session.json"

    def with_credentials(self, username: str | None, password: str | None) -> "Config":
        return Config(
            username=username,
            password=password,
            login_page_url=self.login_page_url,
            login_submit_url=self.login_submit_url,
            login_status_url=self.login_status_url,
            calendar_page_url=self.calendar_page_url,
            calendar_filter_url=self.calendar_filter_url,
            integration_base_url=self.integration_base_url,
            state_dir=self.state_dir,
            timeout_seconds=self.timeout_seconds,
        )

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        state_dir = Path(os.getenv("VA_STATE_DIR") or _default_state_dir())
        return cls(
            username=_optional_env("VA_USERNAME"),
            password=_optional_env("VA_PASSWORD"),
            login_page_url=os.getenv(
                "VA_LOGIN_PAGE_URL",
                "https://shop.virginactive.it/account/login",
            ),
            login_submit_url=os.getenv(
                "VA_LOGIN_SUBMIT_URL",
                "https://shop.virginactive.it/account/login",
            ),
            login_status_url=os.getenv(
                "VA_LOGIN_STATUS_URL",
                "https://www.virginactive.it/rest-api/login-status",
            ),
            calendar_page_url=os.getenv(
                "VA_CALENDAR_PAGE_URL",
                "https://www.virginactive.it/calendario-corsi",
            ),
            calendar_filter_url=os.getenv(
                "VA_CALENDAR_FILTER_URL",
                "https://www.virginactive.it/calendario-corsi/JFilter",
            ),
            integration_base_url=os.getenv(
                "VA_INTEGRATION_BASE_URL",
                "https://www.virginactive.it/VirginIntegrations/IntegrationPlatform",
            ),
            state_dir=state_dir,
            timeout_seconds=float(os.getenv("VA_TIMEOUT_SECONDS", "20")),
        )
