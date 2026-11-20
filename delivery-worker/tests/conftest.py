import os

from cryptography.fernet import Fernet

# Module level: Settings() runs at import time, before any fixture.
os.environ.setdefault("DESTINATION_ENCRYPTION_KEY", Fernet.generate_key().decode())
