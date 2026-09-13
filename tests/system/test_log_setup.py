import logging

import pytest

from app.system.log_setup import MASK, RedactingFormatter, SecretRedactor


@pytest.mark.parametrize(
    "line",
    [
        "headers={'X-MBX-APIKEY': 'aB3dE5fG7hJ9'}",
        "GET /fapi/v1/order?symbol=BTCUSDT&timestamp=1&signature=0f3a9c77e1",
        'body {"apiKey": "mx0vglAbCdEf", "req_time": 1}',
        "listenKey=pqrstuvwxyz0123",
    ],
)
def test_credential_fields_are_masked(line):
    redacted = SecretRedactor().redact(line)
    assert MASK in redacted
    for secret in ("aB3dE5fG7hJ9", "0f3a9c77e1", "mx0vglAbCdEf", "pqrstuvwxyz0123"):
        assert secret not in redacted


def test_registered_value_is_masked_after_argument_formatting():
    redactor = SecretRedactor()
    redactor.add("super-secret-value")
    formatter = RedactingFormatter(redactor, "%(message)s")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "connecting with %s", ("super-secret-value",), None)

    assert formatter.format(record) == f"connecting with {MASK}"


def test_short_values_are_not_registered_to_avoid_masking_ordinary_words():
    redactor = SecretRedactor()
    redactor.add("abc")
    assert redactor.redact("abc def") == "abc def"
