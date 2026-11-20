"""A destination's address and secret, parsed from what the workflow hands an activity."""

import json

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError
from temporalio.exceptions import ApplicationError

from .config import settings
from .contracts import KIND_MODELS


def final_error(type_: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=type_, non_retryable=True)


def decrypt_secret(token: str) -> dict:
    try:
        secret = json.loads(Fernet(settings.destination_encryption_key).decrypt(token.encode()))
    except (InvalidToken, ValueError):
        # Never the cause: it would carry the token.
        raise final_error("DestinationSecret", "destination secret could not be decrypted") from None
    if not isinstance(secret, dict):
        raise final_error("DestinationSecret", "destination secret is not an object")
    return secret


def parse(kind: str, address: dict, encrypted_secret: str) -> tuple[BaseModel, BaseModel]:
    """The kind's address and secret models. Errors name fields only, never values."""
    address_model, secret_model = KIND_MODELS[kind]
    try:
        parsed_address = address_model.model_validate(address)
    except ValidationError as exc:
        raise final_error("DestinationAddress", f"{kind} address: {_fields(exc)}") from None
    try:
        parsed_secret = secret_model.model_validate(decrypt_secret(encrypted_secret))
    except ValidationError as exc:
        raise final_error("DestinationSecret", f"{kind} secret: {_fields(exc)}") from None
    return parsed_address, parsed_secret


def _fields(exc: ValidationError) -> str:
    names = sorted({".".join(str(p) for p in e["loc"]) or "(root)" for e in exc.errors()})
    return "invalid " + ", ".join(names)
