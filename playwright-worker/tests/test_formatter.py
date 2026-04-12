"""
Unit tests for worker/formatter.py — format_output().

All tests are synchronous: format_output has no I/O, no async, no external deps.
We test every branch (html / markdown / json / unknown) and the edge cases that
matter for production correctness (missing <title>, script-tag stripping).
"""

import json

import pytest

from worker.formatter import format_output

URL = "https://example.com"


# ---------------------------------------------------------------------------
# HTML format
# ---------------------------------------------------------------------------


def test_html_passthrough():
    """html format returns the raw HTML bytes unchanged, extension is 'html'."""
    html = "<html><body><p>Hello</p></body></html>"
    content, ext = format_output(html, "html", URL)
    assert ext == "html"
    assert content == html.encode()


# ---------------------------------------------------------------------------
# Markdown format
# ---------------------------------------------------------------------------


def test_markdown_output_extension_and_content():
    """markdown format returns non-empty bytes with extension 'md'."""
    html = "<html><body><h1>Heading</h1><p>Paragraph</p></body></html>"
    content, ext = format_output(html, "markdown", URL)
    assert ext == "md"
    # markdownify converts <h1> to a # heading — just verify we got something
    assert b"Heading" in content
    assert len(content) > 0


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------


def test_json_structure_has_required_keys():
    """json format returns valid JSON with 'url', 'title', and 'text' keys."""
    html = (
        "<html><head><title>My Title</title></head><body><p>Body text</p></body></html>"
    )
    content, ext = format_output(html, "json", URL)
    assert ext == "json"
    data = json.loads(content)
    assert data["url"] == URL
    assert data["title"] == "My Title"
    assert "Body text" in data["text"]


def test_json_strips_script_and_style_tags():
    """json format removes <script> and <style> content from the extracted text."""
    html = (
        "<html><body>"
        "<script>alert('xss')</script>"
        "<style>.hidden { display:none }</style>"
        "<p>Visible content</p>"
        "</body></html>"
    )
    content, _ = format_output(html, "json", URL)
    data = json.loads(content)
    assert "alert" not in data["text"]
    assert "display" not in data["text"]
    assert "Visible content" in data["text"]


def test_json_missing_title_returns_empty_string():
    """json format with no <title> tag returns title='' instead of raising."""
    html = "<html><body><p>No title here</p></body></html>"
    content, _ = format_output(html, "json", URL)
    data = json.loads(content)
    assert data["title"] == ""


# ---------------------------------------------------------------------------
# Unknown format
# ---------------------------------------------------------------------------


def test_unknown_format_raises_value_error():
    """Unrecognised output_format raises ValueError with a descriptive message."""
    with pytest.raises(ValueError, match="Unknown output_format"):
        format_output("<html/>", "xml", URL)
