import pytest
from pydantic import ValidationError

from worker.contracts import PlaywrightOptions, ScrapeInput, ScrapeOutput, StoredObject


def _input(**overrides) -> dict:
    return {
        "artifact_id": "7f1c2a9e-0000-4000-8000-000000000001",
        "url": "https://example.com",
        "output_format": "markdown",
        **overrides,
    }


def test_playwright_options_parse_into_the_typed_model():
    scrape_input = ScrapeInput.model_validate(
        _input(
            playwright_options={"wait_strategy": "networkidle", "timeout_seconds": 45}
        )
    )

    assert scrape_input.playwright_options == PlaywrightOptions(
        wait_strategy="networkidle", timeout_seconds=45
    )


@pytest.mark.parametrize("artifact_id", ["", None])
def test_missing_artifact_id_is_rejected(artifact_id):
    with pytest.raises(ValidationError):
        ScrapeInput.model_validate(_input(artifact_id=artifact_id))


def test_unknown_output_format_is_rejected():
    with pytest.raises(ValidationError):
        ScrapeInput.model_validate(_input(output_format="pdf"))


def test_short_content_hash_is_rejected():
    with pytest.raises(ValidationError):
        ScrapeOutput(
            result=StoredObject(path="b/k", size=1), content_hash="c0ffee12345678"
        )
