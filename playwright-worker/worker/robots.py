"""
robots.txt fetcher and path-disallow checker.

Mirrors the Go worker's internal/robots package (Step 14).
Key invariant: robots.txt is ALWAYS fetched directly — never via the job's proxy.
"""

from urllib.parse import urlparse

import httpx
import structlog

log = structlog.get_logger()

# Module-level client with no proxy configuration.
# This is intentional: robots.txt must be fetched from the real server,
# not through a proxy that may cache or alter the response.
_DIRECT_CLIENT = httpx.AsyncClient(timeout=5.0)

_ROBOTS_MAX_BYTES = 512 * 1024  # 512 KB ceiling — robots.txt files are tiny


async def is_disallowed(url: str) -> bool:
    """Return True if robots.txt disallows the URL path for ScrapeFlow."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    path = parsed.path or "/"

    try:
        resp = await _DIRECT_CLIENT.get(robots_url)
        if resp.status_code != 200:
            return False  # 404, 5xx, etc. → no restrictions
        text = resp.text[:_ROBOTS_MAX_BYTES]
    except Exception as exc:
        log.warning("robots_fetch_failed", url=robots_url, error=str(exc))
        return False  # network error → proceed

    return _is_path_disallowed(text, path)


def _is_path_disallowed(robots_text: str, path: str) -> bool:
    """
    Parse robots_text and decide whether `path` is disallowed for ScrapeFlow.

    Precedence rules (matching the Go implementation):
      1. If a 'User-agent: ScrapeFlow' block exists, use it exclusively;
         the wildcard block is completely ignored.
      2. Among matching Disallow/Allow rules, the longest prefix wins.
      3. An empty Disallow value means "allow all" (skip the rule).
    """
    scrapeflow_rules: list[tuple[bool, str]] = []  # (is_allow, path_prefix)
    wildcard_rules: list[tuple[bool, str]] = []

    current_agents: list[str] = []
    in_block = False

    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            current_agents = []
            in_block = False
            continue

        if ":" not in line:
            continue

        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            agent = value.lower()
            current_agents.append(agent)
            in_block = True
        elif field == "disallow" and in_block:
            for agent in current_agents:
                if agent == "scrapeflow":
                    scrapeflow_rules.append((False, value))
                elif agent == "*":
                    wildcard_rules.append((False, value))
        elif field == "allow" and in_block:
            for agent in current_agents:
                if agent == "scrapeflow":
                    scrapeflow_rules.append((True, value))
                elif agent == "*":
                    wildcard_rules.append((True, value))

    rules = scrapeflow_rules if scrapeflow_rules else wildcard_rules
    return _apply_rules(rules, path)


def _apply_rules(rules: list[tuple[bool, str]], path: str) -> bool:
    """Longest-matching-prefix wins. Returns True if disallowed."""
    best_len = -1
    best_allow = True  # no match → allow

    for is_allow, prefix in rules:
        if not prefix:
            continue  # empty Disallow = allow all; skip
        if path.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best_allow = is_allow

    return not best_allow
