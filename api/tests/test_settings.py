from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.settings import Settings


def test_llm_key_empty_rejected():
    """Empty string (i.e. key not set in env) raises ValidationError."""
    kwargs: dict[str, Any] = {"LLM_KEY_ENCRYPTION_KEY": ""}
    with pytest.raises(ValidationError, match="must be set"):
        Settings(**kwargs)


def test_llm_key_invalid_rejected():
    """A non-Fernet string raises ValidationError."""
    kwargs: dict[str, Any] = {"LLM_KEY_ENCRYPTION_KEY": "not-a-fernet-key"}
    with pytest.raises(ValidationError, match="not a valid Fernet key"):
        Settings(**kwargs)


def test_llm_key_valid_accepted():
    """A properly generated Fernet key is accepted."""
    key = Fernet.generate_key().decode()
    kwargs: dict[str, Any] = {"LLM_KEY_ENCRYPTION_KEY": key}
    s = Settings(**kwargs)
    assert s.llm_key_encryption_key == key
