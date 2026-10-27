"""E.6 — the crawl gate's expected result must stay true to the fixture site it describes."""

import json
from pathlib import Path

SITE = Path(__file__).resolve().parent / "fixtures" / "crawl-site"
EXPECTED = json.loads((SITE / "expected.json").read_text())


def test_every_completed_page_names_a_file_that_exists():
    for page in EXPECTED["pages"]:
        if page["status"] == "completed":
            assert (SITE / "site" / page["file"]).is_file(), page


def test_the_counters_match_the_page_set():
    pages = EXPECTED["pages"]
    assert EXPECTED["total_queued"] == len(pages)
    assert EXPECTED["total_completed"] == sum(p["status"] == "completed" for p in pages)
    assert EXPECTED["total_failed"] == sum(p["status"] == "failed" for p in pages)


def test_the_bait_is_on_the_site_so_a_fetch_would_show():
    for path in EXPECTED["never_requested"]:
        assert (SITE / "site" / path.lstrip("/")).is_file(), path
