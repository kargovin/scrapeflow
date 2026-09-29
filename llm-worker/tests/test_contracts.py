import pytest
from pydantic import ValidationError

from worker.contracts import LLMInput, LLMOutput, StoredObject


def _input(**overrides) -> dict:
    return {
        "artifact_id": "7f1c2a9e-0000-4000-8000-000000000001",
        "raw_minio_path": "scrapeflow-results/history/a/scrape.md",
        "provider": "anthropic",
        "encrypted_api_key": "gAAAA-key",
        "model": "claude-sonnet-5-5",
        "output_schema": {"type": "object"},
        **overrides,
    }


def test_anthropic_input_needs_no_base_url():
    assert LLMInput.model_validate(_input()).base_url is None


@pytest.mark.parametrize("artifact_id", ["", None])
def test_missing_artifact_id_is_rejected(artifact_id):
    with pytest.raises(ValidationError):
        LLMInput.model_validate(_input(artifact_id=artifact_id))


def test_unknown_provider_is_rejected():
    with pytest.raises(ValidationError):
        LLMInput.model_validate(_input(provider="gemini"))


def test_negative_size_is_rejected():
    with pytest.raises(ValidationError):
        LLMOutput(result=StoredObject(path="b/k", size=-1))
