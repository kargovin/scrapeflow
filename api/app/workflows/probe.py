from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from app.workflows import steps
    from app.workflows.activities.contracts import (
        LLMInput,
        LLMOutput,
        ScrapeInput,
        ScrapeOutput,
    )


class ScrapeProbeInput(BaseModel):
    scrape: ScrapeInput
    engine: steps.Engine = "http"


@workflow.defn
class ScrapeProbeWorkflow:
    """Operator-only: one Scrape activity, no DB, no ledger row — scripts/probe_scrape.py starts it."""

    @workflow.run
    async def run(self, input: ScrapeProbeInput) -> ScrapeOutput:
        return await steps.scrape(input.scrape, input.engine)


class LLMProbeInput(BaseModel):
    scrape: ScrapeInput
    provider: Literal["anthropic", "openai_compatible"]
    encrypted_api_key: str
    base_url: str | None = None
    model: str
    output_schema: dict[str, Any]


class LLMProbeOutput(BaseModel):
    scrape: ScrapeOutput
    llm: LLMOutput


@workflow.defn
class LLMProbeWorkflow:
    """Operator-only: Scrape, then LLMExtract on the object it wrote — scripts/probe_llm.py."""

    @workflow.run
    async def run(self, input: LLMProbeInput) -> LLMProbeOutput:
        scraped = await steps.scrape(input.scrape)
        extracted = await steps.llm_extract(
            LLMInput(
                artifact_id=input.scrape.artifact_id,
                raw_minio_path=scraped.result.path,
                provider=input.provider,
                encrypted_api_key=input.encrypted_api_key,
                base_url=input.base_url,
                model=input.model,
                output_schema=input.output_schema,
            )
        )
        return LLMProbeOutput(scrape=scraped, llm=extracted)
