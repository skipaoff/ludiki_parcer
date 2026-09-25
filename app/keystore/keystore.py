"""
VFP: Reads and writes secrets in the credential store of the machine — Windows Credential Manager, macOS keychain, or the environment on a server — and shows them only masked.
Changes when: a new kind of secret appears or the credential store changes.
Anti-goal:
1. Secrets in files, the database, logs or API responses — only this module touches raw values.
2. A secret that is loaded but unknown to the log redactor — every read and write registers the value.

Named keystore, not secrets: .gitignore excludes `secrets*` paths.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

from app.system.log_setup import SecretRedactor

SERVICE = "ludik"
DB_PASSWORD = "postgres:ludik"
SESSION_TOKEN = "session:token"
TELEGRAM_TOKEN = "telegram:bot_token"


def api_key_name(exchange: str) -> str:
    return f"{exchange}:api_key"


def api_secret_name(exchange: str) -> str:
    return f"{exchange}:api_secret"


def api_passphrase_name(exchange: str) -> str:
    """Bitget and KuCoin keys come with a passphrase chosen when the key was made."""
    return f"{exchange}:api_passphrase"


class CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def mask(secret: str | None) -> str | None:
    """abcd…wxyz for long values, a bare ellipsis for short ones, None when absent."""
    if not secret:
        return None
    if len(secret) < 12:
        return "…"
    return f"{secret[:4]}…{secret[-4:]}"


class EnvironmentBackend:
    """
    Secrets from the environment, for a headless server where no desktop credential store exists.
    `postgres:ludik` reads LUDIK_POSTGRES_LUDIK, `telegram:bot_token` reads LUDIK_TELEGRAM_BOT_TOKEN.
    Writing is refused on purpose: on a server the owner puts secrets there, not the terminal.
    """

    prefix = "LUDIK_"

    @staticmethod
    def variable(name: str) -> str:
        return EnvironmentBackend.prefix + re.sub(r"[^A-Za-z0-9]+", "_", name).upper()

    def get_password(self, service: str, username: str) -> str | None:
        return os.environ.get(self.variable(username)) or None

    def set_password(self, service: str, username: str, password: str) -> None:
        raise PermissionError(f"secrets come from the environment here: set {self.variable(username)} and restart")

    def delete_password(self, service: str, username: str) -> None:
        raise PermissionError(f"secrets come from the environment here: unset {self.variable(username)} and restart")


def default_backend() -> CredentialBackend:
    """LUDIK_SECRETS=env picks the environment; anything else means the credential store of this desktop."""
    if os.environ.get("LUDIK_SECRETS", "").lower() == "env":
        return EnvironmentBackend()
    import keyring

    return keyring.get_keyring()


class Keystore:
    def __init__(self, redactor: SecretRedactor, backend: CredentialBackend | None = None, service: str = SERVICE):
        if backend is None:
            backend = default_backend()
        self._backend = backend
        self._redactor = redactor
        self._service = service

    def get(self, name: str) -> str | None:
        value = self._backend.get_password(self._service, name)
        self._redactor.add(value)
        return value

    def set(self, name: str, value: str) -> None:
        self._redactor.add(value)
        self._backend.set_password(self._service, name, value)

    def delete(self, name: str) -> None:
        if self._backend.get_password(self._service, name) is not None:
            self._backend.delete_password(self._service, name)

    def masked(self, name: str) -> str | None:
        return mask(self.get(name))
