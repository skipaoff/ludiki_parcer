from app.keystore.keystore import Keystore, api_key_name, mask
from app.system.log_setup import SecretRedactor


class MemoryBackend:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        del self.values[(service, username)]


def test_mask_never_reveals_the_middle():
    assert mask("abcdEFGHIJKLmnopwxyz") == "abcd…wxyz"
    assert mask("short") == "…"
    assert mask(None) is None
    assert mask("") is None


def test_round_trip_and_delete():
    backend = MemoryBackend()
    keystore = Keystore(SecretRedactor(), backend)

    keystore.set(api_key_name("binance"), "key-value-123456")

    assert keystore.get("binance:api_key") == "key-value-123456"
    assert keystore.masked("binance:api_key") == "key-…3456"
    keystore.delete("binance:api_key")
    keystore.delete("binance:api_key")
    assert keystore.get("binance:api_key") is None


def test_every_secret_that_passes_through_is_redacted_from_logs():
    backend = MemoryBackend()
    backend.set_password("ludik", "mexc:api_secret", "stored-before-start-999")
    redactor = SecretRedactor()
    keystore = Keystore(redactor, backend)

    keystore.set("binance:api_secret", "written-now-abcdef")
    keystore.get("mexc:api_secret")

    text = redactor.redact("sign with written-now-abcdef and stored-before-start-999")
    assert "written-now-abcdef" not in text
    assert "stored-before-start-999" not in text
