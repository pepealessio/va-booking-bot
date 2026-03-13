from __future__ import annotations

from dataclasses import dataclass

import keyring
from keyring.errors import KeyringError

from .client import VAError

SERVICE_NAME = "va-cli"
USERNAME_FIELD = "__username__"


@dataclass(slots=True)
class SavedCredentials:
    username: str
    password: str


class CredentialStore:
    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self.service_name = service_name

    def load(self) -> SavedCredentials | None:
        try:
            username = keyring.get_password(self.service_name, USERNAME_FIELD)
            if not username:
                return None
            password = keyring.get_password(self.service_name, username)
        except KeyringError as exc:
            raise VAError(f"Could not read credentials from system keyring: {exc}") from exc
        if not password:
            return None
        return SavedCredentials(username=username, password=password)

    def save(self, username: str, password: str) -> None:
        try:
            existing_username = keyring.get_password(self.service_name, USERNAME_FIELD)
            if existing_username and existing_username != username:
                keyring.delete_password(self.service_name, existing_username)
            keyring.set_password(self.service_name, USERNAME_FIELD, username)
            keyring.set_password(self.service_name, username, password)
        except KeyringError as exc:
            raise VAError(f"Could not save credentials to system keyring: {exc}") from exc

    def clear(self) -> None:
        try:
            username = keyring.get_password(self.service_name, USERNAME_FIELD)
            if username:
                self._delete_password(username)
                self._delete_password(USERNAME_FIELD)
        except KeyringError as exc:
            raise VAError(f"Could not clear credentials from system keyring: {exc}") from exc

    def _delete_password(self, username: str) -> None:
        try:
            keyring.delete_password(self.service_name, username)
        except KeyringError:
            pass
