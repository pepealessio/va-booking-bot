from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
    queue_full_threshold: int
    booking_open_hours: int

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
            queue_full_threshold=self.queue_full_threshold,
            booking_open_hours=self.booking_open_hours,
        )

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        state_dir = Path(os.getenv("VA_STATE_DIR") or Path.cwd() / ".va_state")

        _u = os.getenv("VA_USERNAME")
        username = _u.strip() if _u else None
        _p = os.getenv("VA_PASSWORD")
        password = _p.strip() if _p else None

        return cls(
            username=username,
            password=password,
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
            queue_full_threshold=int(os.getenv("VA_QUEUE_FULL_THRESHOLD", "15")),
            booking_open_hours=int(os.getenv("VA_BOOKING_OPEN_HOURS", "48")),
        )
