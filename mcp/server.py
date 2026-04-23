"""
ScrapeFlow MCP server (stdio transport).

Exposes four tools to LLM agents:
  scrape_url      — submit a single-URL scrape job, poll until done, return content
  get_result      — fetch stored result content for a completed job by job_id
  get_job_status  — fetch current status of a job by job_id
  list_jobs       — paginated list of the authenticated user's jobs
"""

import json

import mcp.server.stdio
import mcp.types as types
import structlog
from mcp.server import Server
from mcp.server.models import InitializationOptions

from tools.get_job_status import get_job_status
from tools.get_result import get_result
from tools.list_jobs import list_jobs
from tools.scrape_url import scrape_url

log = structlog.get_logger()

app = Server("scrapeflow")


@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="scrape_url",
            description=(
                "Submit a single URL scrape job to ScrapeFlow and return the result. "
                "Single URL only — pass one URL per call. "
                "When wait_for_result is true (default), blocks until the job completes "
                "and returns the content. When false, returns immediately with the job_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to scrape (single URL only)."},
                    "output_format": {
                        "type": "string",
                        "enum": ["html", "markdown", "json"],
                        "default": "html",
                        "description": "Desired output format.",
                    },
                    "engine": {
                        "type": "string",
                        "enum": ["http", "playwright"],
                        "default": "http",
                        "description": "Scraping engine. Use playwright for JS-rendered pages.",
                    },
                    "wait_for_result": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true, poll until done and return content.",
                    },
                },
                "required": ["url"],
            },
        ),
        types.Tool(
            name="get_result",
            description=(
                "Fetch the stored result content for a completed ScrapeFlow job by job_id. "
                "Use this when you previously called scrape_url with wait_for_result=false."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "UUID of the job."},
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="get_job_status",
            description="Fetch the current status of a ScrapeFlow job by job_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "UUID of the job."},
                },
                "required": ["job_id"],
            },
        ),
        types.Tool(
            name="list_jobs",
            description="List the authenticated user's ScrapeFlow jobs, newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max results to return (1–200).",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Pagination offset.",
                    },
                },
            },
        ),
    ]


@app.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:
    log.info("tool_called", tool=name, arguments=arguments)

    if name == "scrape_url":
        result = await scrape_url(
            url=arguments["url"],
            output_format=arguments.get("output_format", "html"),
            engine=arguments.get("engine", "http"),
            wait_for_result=arguments.get("wait_for_result", True),
        )
    elif name == "get_result":
        result = await get_result(job_id=arguments["job_id"])
    elif name == "get_job_status":
        result = await get_job_status(job_id=arguments["job_id"])
    elif name == "list_jobs":
        result = await list_jobs(
            limit=arguments.get("limit", 20),
            offset=arguments.get("offset", 0),
        )
    else:
        result = {"error": f"unknown tool: {name}"}

    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="scrapeflow",
                server_version="0.1.0",
                capabilities=app.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":
    main()
