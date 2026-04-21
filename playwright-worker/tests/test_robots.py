"""
Unit tests for worker/robots.py.

Two layers are tested independently:
  - _is_path_disallowed() — pure parser, no I/O
  - is_disallowed()       — full fetch+parse flow, httpx client is mocked

Mirrors the test coverage of the Go worker's robots_test.go and
worker_robots_test.go (Step 14 reference implementation).
"""

from unittest.mock import AsyncMock, MagicMock, patch


from worker.robots import _is_path_disallowed, is_disallowed


# ---------------------------------------------------------------------------
# _is_path_disallowed — pure parser tests (no network)
# ---------------------------------------------------------------------------


def test_disallowed_prefix_blocks_path():
    robots = "User-agent: *\nDisallow: /secret/\n"
    assert _is_path_disallowed(robots, "/secret/data") is True


def test_non_matching_prefix_allows_path():
    robots = "User-agent: *\nDisallow: /secret/\n"
    assert _is_path_disallowed(robots, "/public/page") is False


def test_empty_disallow_allows_all():
    """An empty Disallow: value means 'allow everything'."""
    robots = "User-agent: *\nDisallow:\n"
    assert _is_path_disallowed(robots, "/anything") is False


def test_disallow_root_blocks_everything():
    robots = "User-agent: *\nDisallow: /\n"
    assert _is_path_disallowed(robots, "/any/path") is True


def test_longest_match_wins_allow_overrides_disallow():
    """
    Disallow: /admin/  +  Allow: /admin/login
    /admin/login is 12 chars > /admin/ which is 7 chars — Allow wins.
    """
    robots = "User-agent: *\nDisallow: /admin/\nAllow: /admin/login\n"
    assert _is_path_disallowed(robots, "/admin/login") is False
    assert _is_path_disallowed(robots, "/admin/settings") is True


def test_scrapeflow_block_takes_precedence_over_wildcard():
    """
    If a ScrapeFlow block exists, the wildcard block is completely ignored.
    Wildcard Disallow: / + ScrapeFlow Disallow: /only-this
    → /other/path is allowed (wildcard ignored).
    """
    robots = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: ScrapeFlow\nDisallow: /only-this\n"
    )
    assert _is_path_disallowed(robots, "/only-this") is True
    assert _is_path_disallowed(robots, "/other/path") is False


def test_scrapeflow_empty_disallow_ignores_wildcard_rules():
    """
    ScrapeFlow block with empty Disallow overrides a wildcard Disallow: /.
    The ScrapeFlow block is exclusive — wildcard restrictions don't apply.
    """
    robots = "User-agent: *\nDisallow: /\n\n" "User-agent: ScrapeFlow\nDisallow:\n"
    assert _is_path_disallowed(robots, "/blocked-by-wildcard") is False


def test_comments_are_stripped():
    robots = "User-agent: * # comment\nDisallow: /secret/ # another comment\n"
    assert _is_path_disallowed(robots, "/secret/page") is True


def test_no_rules_allows_all():
    assert _is_path_disallowed("", "/any/path") is False


# ---------------------------------------------------------------------------
# is_disallowed — fetch layer tests (httpx client mocked)
# ---------------------------------------------------------------------------


async def test_is_disallowed_returns_true_when_path_blocked():
    robots_body = "User-agent: *\nDisallow: /secret/\n"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = robots_body

    with patch("worker.robots._DIRECT_CLIENT") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await is_disallowed("https://example.com/secret/data")

    assert result is True


async def test_is_disallowed_returns_false_when_path_allowed():
    robots_body = "User-agent: *\nDisallow: /secret/\n"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = robots_body

    with patch("worker.robots._DIRECT_CLIENT") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await is_disallowed("https://example.com/public/page")

    assert result is False


async def test_is_disallowed_returns_false_on_404():
    """A 404 on /robots.txt means no restrictions — proceed."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("worker.robots._DIRECT_CLIENT") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await is_disallowed("https://example.com/page")

    assert result is False


async def test_is_disallowed_returns_false_on_network_error():
    """Connection refused, timeout, etc. → no restrictions — proceed."""
    with patch("worker.robots._DIRECT_CLIENT") as mock_client:
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        result = await is_disallowed("https://example.com/page")

    assert result is False


async def test_robots_url_constructed_from_job_url():
    """robots.txt is fetched from the root of the job URL's host, not the job path."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = ""

    with patch("worker.robots._DIRECT_CLIENT") as mock_client:
        mock_client.get = AsyncMock(return_value=mock_resp)
        await is_disallowed("https://example.com/deep/nested/path?q=1")
        fetched_url = mock_client.get.call_args.args[0]

    assert fetched_url == "https://example.com/robots.txt"
