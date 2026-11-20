"""ADR-015 §4: this worker cannot reach the app database, and holds no key but the destinations key."""

import importlib.util

import pytest

from worker.config import Settings


@pytest.mark.parametrize("module", ["app", "sqlalchemy", "alembic", "redis"])
def test_the_app_and_its_database_layer_are_not_installed(module):
    assert importlib.util.find_spec(module) is None


def test_settings_hold_temporal_minio_and_the_destinations_key_only():
    assert set(Settings.model_fields) == {
        "temporal_address",
        "temporal_namespace",
        "delivery_graceful_shutdown_seconds",
        "minio_endpoint",
        "minio_access_key",
        "minio_secret_key",
        "minio_bucket",
        "minio_secure",
        "destination_encryption_key",
    }


def test_other_keys_in_the_environment_are_not_read(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@postgres/db")
    monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEY", "x")
    assert not hasattr(Settings(), "database_url")


def test_the_destinations_key_is_required(monkeypatch):
    monkeypatch.delenv("DESTINATION_ENCRYPTION_KEY")
    with pytest.raises(ValueError, match="DESTINATION_ENCRYPTION_KEY must be set"):
        Settings()
