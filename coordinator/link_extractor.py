from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_SKIP_SCHEMES = {"#", "mailto:", "tel:", "javascript:"}


def extract_links(
    html: str,
    base_url: str,
    include_paths: list[str] | None,
    exclude_paths: list[str] | None,
) -> list[str]:
    """Extract unique same-origin href links from HTML, applying include/exclude path filters.

    Returns normalized URLs with fragment stripped. Never returns the base_url itself.
    """
    soup = BeautifulSoup(html, "lxml")
    base_parsed = urlparse(base_url)
    base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

    seen: set[str] = set()
    links: list[str] = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href or any(href.startswith(s) for s in _SKIP_SCHEMES):
            continue

        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        if f"{parsed.scheme}://{parsed.netloc}" != base_origin:
            continue

        # Strip fragment — two pages that differ only in fragment are the same page.
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen or normalized == base_url:
            continue
        seen.add(normalized)

        path = parsed.path or "/"

        if include_paths and not any(path.startswith(p) for p in include_paths):
            continue
        if exclude_paths and any(path.startswith(p) for p in exclude_paths):
            continue

        links.append(normalized)

    return links
