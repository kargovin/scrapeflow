"""Clean: drop page boilerplate, then convert what is left to markdown."""

import re

import markdownify
from bs4 import BeautifulSoup, Tag

_DROP_TAGS = (
    "head",
    "script",
    "style",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
    "canvas",
    "nav",
    "aside",
    "form",
    "button",
    "dialog",
)
_DROP_ROLES = (
    "navigation",
    "banner",
    "contentinfo",
    "complementary",
    "search",
    "dialog",
    "alertdialog",
)
# A header/footer inside one of these is part of the content (an article's title, a byline).
_SECTIONING = ("article", "main", "section")
_BLANK_RUNS = re.compile(r"\n{3,}")


def _drop(tags: list[Tag]) -> None:
    for tag in tags:
        if not tag.decomposed:
            tag.decompose()


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    _drop(soup(_DROP_TAGS))
    _drop(soup.select(",".join(f'[role="{r}"]' for r in _DROP_ROLES)))
    _drop(soup.select('[hidden], [aria-hidden="true"]'))
    _drop([t for t in soup(["header", "footer"]) if t.find_parent(_SECTIONING) is None])
    _drop([t for t in soup("img") if str(t.get("src", "")).startswith("data:")])
    markdown = markdownify.markdownify(str(soup), heading_style="ATX")
    return _BLANK_RUNS.sub("\n\n", markdown).strip() + "\n"
