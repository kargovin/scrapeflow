from cryptography.fernet import Fernet
from fastapi import Request

from app.settings import settings


def get_fernet(request: Request) -> Fernet:
    """Helper to get Fernet instance with app's LLM key encryption key."""
    return Fernet(settings.llm_key_encryption_key)
