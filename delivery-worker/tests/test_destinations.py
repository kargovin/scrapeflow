import json

import pytest
from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError

from worker.config import settings
from worker.contracts import BucketAddress, LoginSecret, PostgresAddress, SmtpAddress
from worker.destinations import decrypt_secret, parse


def _encrypt(value, key: str | None = None) -> str:
    return Fernet(key or settings.destination_encryption_key).encrypt(json.dumps(value).encode()).decode()


def test_a_secret_encrypted_under_the_key_decrypts():
    assert decrypt_secret(_encrypt({"user": "u", "password": "p"})) == {"user": "u", "password": "p"}


@pytest.mark.parametrize(
    "token",
    [_encrypt({"a": 1}, Fernet.generate_key().decode()), "not-a-token", _encrypt(["a"])],
)
def test_a_secret_that_cannot_be_read_is_final_and_names_no_value(token):
    with pytest.raises(ApplicationError) as exc:
        decrypt_secret(token)
    assert exc.value.non_retryable
    assert exc.value.type == "DestinationSecret"
    assert token not in str(exc.value)
    assert exc.value.__cause__ is None


def test_each_kind_parses_its_stored_shape():
    login = _encrypt({"user": "u", "password": "p"})
    address, secret = parse(
        "bucket",
        {"endpoint": "https://s3.example.com/", "bucket": "b", "prefix": "", "region": None},
        _encrypt({"access_key": "a", "secret_key": "s"}),
    )
    assert isinstance(address, BucketAddress) and secret.secret_key == "s"

    address, secret = parse(
        "postgres",
        {"host": "db.example.com", "port": 5432, "database": "d", "schema": "s", "table": "t",
         "ca_certificate": None},
        login,
    )
    assert isinstance(address, PostgresAddress) and address.db_schema == "s"
    assert isinstance(secret, LoginSecret)

    address, _ = parse(
        "smtp",
        {"host": "mail.example.com", "port": 465, "tls_mode": "implicit", "from": "a@example.com",
         "to": ["b@example.com"], "ca_certificate": None},
        login,
    )
    assert isinstance(address, SmtpAddress) and address.sender == "a@example.com"

    _, secret = parse("webhook", {}, _encrypt({"url": "https://h.example.com/x", "secret": None}))
    assert secret.url == "https://h.example.com/x"


def test_a_malformed_secret_names_the_field_and_not_the_value():
    with pytest.raises(ApplicationError) as exc:
        parse("bucket", {"endpoint": "https://s3.example.com/", "bucket": "b"},
              _encrypt({"access_key": "AKIA-SECRET-ISH"}))
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationSecret", True)
    assert exc.value.message == "bucket secret: invalid secret_key"
    assert "AKIA" not in str(exc.value)


def test_a_malformed_address_is_final():
    with pytest.raises(ApplicationError) as exc:
        parse("smtp", {"host": "mail.example.com", "port": 587}, _encrypt({"user": "u", "password": "p"}))
    assert exc.value.type == "DestinationAddress"
    assert exc.value.non_retryable
    assert exc.value.message == "smtp address: invalid from, tls_mode, to"
