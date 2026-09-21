"""
VFP: Reads and writes secrets in the OS credential store (Windows Credential Manager) and shows them only masked.
Changes when: a new kind of secret appears or the credential store changes.
Anti-goal:
1. Secrets in files, the database, logs or API responses — only this module touches raw values.
2. A secret that is loaded but unknown to the log redactor — every read and write registers the value.

Named keystore, not secrets: .gitignore excludes `secrets*` paths.
"""

from __future__ import annotations

from typing import Protocol

from app.system.log_setup import SecretRedactor

SERVICE = "ludik"
DB_PASSWORD = "postgres:ludik"
SESSION_TOKEN = "session:token"


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


class Keystore:
    def __init__(self, redactor: SecretRedactor, backend: CredentialBackend | None = None, service: str = SERVICE):
        if backend is None:
            import keyring

            backend = keyring.get_keyring()
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
