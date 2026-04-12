"""
Output formatting — mirrors the Go worker's formatter.go.
Three formats: html (passthrough), markdown, json.
"""

import json

import markdownify
from bs4 import BeautifulSoup


def format_output(html: str, output_format: str, final_url: str) -> tuple[bytes, str]:
    """Convert rendered HTML into the requested format.

    Returns (content_bytes, file_extension).
    file_extension is used to build the MinIO object key and set Content-Type.
    """
    if output_format == "html":
        return html.encode(), "html"

    if output_format == "markdown":
        text = markdownify.markdownify(html)
        return text.encode(), "md"

    if output_format == "json":
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        # Remove script/style nodes before extracting visible text —
        # same logic as Go's extractTitleAndText.
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        payload = json.dumps({"url": final_url, "title": title, "text": text})
        return payload.encode(), "json"

    raise ValueError(f"Unknown output_format: {output_format!r}")
