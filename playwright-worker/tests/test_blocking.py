"""
Unit tests for worker/blocking.py — detect_block().

All tests are synchronous: detect_block is pure (no I/O, no async).

The three "live wall" fixtures are trimmed from artifacts pulled out of prod
MinIO on 2026-07-22 (BUG-003 audit) — real bytes, not invented ones. The
negative cases matter as much as the positives: a false "blocked" fails a
working job, so most of this file is about NOT firing.
"""

import pytest

from worker.blocking import (
    TIER2_MAX_BYTES,
    VENDOR_AKAMAI,
    VENDOR_AMAZON,
    VENDOR_CLOUDFLARE,
    VENDOR_DATADOME,
    VENDOR_IMPERVA,
    VENDOR_KASADA,
    VENDOR_PERIMETERX,
    VENDOR_SUCURI,
    VENDOR_UNKNOWN,
    detect_block,
)

# ---------------------------------------------------------------------------
# Live prod fixtures (trimmed from real MinIO artifacts, 2026-07-22)
# ---------------------------------------------------------------------------

# job 1db4f858… run d8dd7cf3… — amazon.com/…/B01NBKTPTS, stored as `completed`
AMAZON_WALL = """<!DOCTYPE html><html class="a-no-js" lang="en-us"><head>
<title dir="ltr">Amazon.com</title>
<script>
    var ue_sn = "opfcaptcha.amazon.com",
        ue_id = 'M66YPJ1GVSF5XKARNQZA';
</script>
<script src="https://images-na.ssl-images-amazon.com/images/G/01/csminstrumentation/csm-captcha-instrumentation.min.js"></script>
</head><body>
<!-- To discuss automated access to Amazon data please contact api-services-support@amazon.com. -->
<div class="a-box a-alert a-alert-info a-spacing-base">
    <h4>Click the button below to continue shopping</h4>
</div>
<form method="get" action="/errors/validateCaptcha" name="">
    <input type="hidden" name="amzn" value="GzDnBl0PStXiakbbRmhP+Q==">
    <button type="submit" class="a-button-text" alt="Continue shopping">Continue shopping</button>
</form>
</body></html>"""

# myntra.com/…/36854940/buy — 411 B in prod
MYNTRA_WALL = """<html><head>
<title>Access Denied</title>
</head><body>
<h1>Access Denied</h1>
You don't have permission to access "http://www.myntra.com/jeans/…/buy" on this server.<p>
Reference #18.d3df3517.1783094356.58e2b976
</p><p>https://errors.edgesuite.net/18.d3df3517.1783094356.58e2b976</p>
</body></html>"""

# walmart.com/ip/…/25920745 — the raw HTML behind the 464 B markdown artifact
WALMART_WALL = """<html><head><title>Robot or human?</title></head><body>
<h1>Robot or human?</h1>
<p>Activate and hold the button to confirm that you&rsquo;re human. Thank You!</p>
<script>window._pxAppId = 'PXu6b0qd2S';</script>
</body></html>"""


def _big_page(marker: str = "", size: int = 400_000) -> str:
    """A genuine, full-size page. Padded past TIER2_MAX_BYTES."""
    filler = "<p>Real product copy and reviews. </p>" * 200
    body = (filler * (size // len(filler) + 1))[:size]
    return (
        f"<html><head><title>Real Page</title></head><body>{marker}{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# Live walls — all three must be caught
# ---------------------------------------------------------------------------


def test_amazon_wall_detected():
    result = detect_block(AMAZON_WALL, status=200)
    assert result.blocked
    assert result.vendor == VENDOR_AMAZON
    assert result.tier == 1
    assert result.error == "blocked:amazon"


def test_myntra_akamai_wall_detected():
    result = detect_block(MYNTRA_WALL, status=200)
    assert result.blocked
    assert result.vendor == VENDOR_AKAMAI
    assert result.tier == 1
    assert result.error == "blocked:akamai"


def test_walmart_perimeterx_wall_detected():
    result = detect_block(WALMART_WALL, status=200)
    assert result.blocked
    assert result.vendor == VENDOR_PERIMETERX
    assert result.tier == 1


def test_live_walls_are_caught_at_tier1_regardless_of_status():
    """All three prod walls returned 200 — none relied on the status signal."""
    for html in (AMAZON_WALL, MYNTRA_WALL, WALMART_WALL):
        assert detect_block(html, status=200).tier == 1
        assert detect_block(html, status=None).blocked


# ---------------------------------------------------------------------------
# Tier 1 — vendor fingerprints fire alone, at any page size
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,expected_vendor",
    [
        ("Reference #18.d3df3517.1783094356.58e2b976", VENDOR_AKAMAI),
        ("Pardon Our Interruption", VENDOR_AKAMAI),
        ("AkamaiGHost", VENDOR_AKAMAI),
        ('<span class="cf-error-code">1020</span>', VENDOR_CLOUDFLARE),
        ("/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1", VENDOR_CLOUDFLARE),
        ("cf_chl_opt", VENDOR_CLOUDFLARE),
        ("window._pxAppId = 'PXabc'", VENDOR_PERIMETERX),
        ("captcha.px-cdn.net/foo.js", VENDOR_PERIMETERX),
        ("geo.captcha-delivery.com", VENDOR_DATADOME),
        ("_Incapsula_Resource?SWJIYLWA=", VENDOR_IMPERVA),
        ("Incapsula incident ID: 123-456", VENDOR_IMPERVA),
        ("Sucuri WebSite Firewall - Access Denied", VENDOR_SUCURI),
        ("KPSDK.scriptStart = KPSDK.now()", VENDOR_KASADA),
        ("opfcaptcha.amazon.com", VENDOR_AMAZON),
        ("Request blocked by network security policy", VENDOR_UNKNOWN),
    ],
)
def test_tier1_markers_fire_alone(marker, expected_vendor):
    """Tier 1 is decisive even on a full-size page — no size gate."""
    result = detect_block(_big_page(marker), status=200)
    assert result.blocked
    assert result.tier == 1
    assert result.vendor == expected_vendor


def test_tier1_signals_record_why_it_fired():
    result = detect_block(MYNTRA_WALL, status=200)
    assert any(s.startswith("tier1:") for s in result.signals)
    assert "status:200" in result.signals


# ---------------------------------------------------------------------------
# Status signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
def test_block_statuses_fire_alone(status):
    result = detect_block(_big_page(), status=status)
    assert result.blocked
    assert result.vendor == VENDOR_UNKNOWN
    assert result.signals == [f"status:{status}"]


@pytest.mark.parametrize("status", [200, 201, 301, 404, 500])
def test_non_block_statuses_do_not_fire_alone(status):
    """404/500 are honest server answers, not bot walls."""
    assert not detect_block(_big_page(), status=status).blocked


def test_missing_status_is_not_evidence():
    """page.goto() returns None for same-document navigation."""
    assert not detect_block(_big_page(), status=None).blocked


# ---------------------------------------------------------------------------
# Tier 2 — size-gated. This is the core false-positive guard.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "Access Denied",
        "Just a moment...",
        "Checking your browser before accessing",
        "Verifying you are human",
        "Robot Check",
        "Are you a robot",
        "Request unsuccessful",
        "unusual traffic from your computer network",
        "Access to This Page Has Been Blocked",
    ],
)
def test_tier2_phrases_fire_on_small_pages(phrase):
    # Padded past EMPTY_BODY_BYTES so this exercises tier 2, not the
    # empty-body shortcut — but kept well under TIER2_MAX_BYTES.
    pad = "<p>challenge</p>" * 30
    html = f"<html><body><h1>{phrase}</h1>{pad}</body></html>"
    result = detect_block(html, status=200)
    assert result.blocked
    assert result.tier == 2


@pytest.mark.parametrize(
    "phrase",
    [
        "Access Denied",
        "Just a moment...",
        "Checking your browser before accessing",
        "Verifying you are human",
        "Robot Check",
        "Are you a robot",
        "Continue shopping",
        "unusual traffic from your computer network",
    ],
)
def test_tier2_phrases_do_not_fire_on_full_size_pages(phrase):
    """The whole point of the size gate.

    A news article about CAPTCHAs, or a store page with a "Continue shopping"
    button, must not be failed. Size is what separates the wall from the
    article — these phrases are legitimate content at full page size.
    """
    assert not detect_block(_big_page(phrase), status=200).blocked


def test_tier2_boundary_just_under_threshold_fires():
    pad = "x" * (TIER2_MAX_BYTES - 200)
    html = f"<html><body><h1>Just a moment</h1>{pad}</body></html>"
    assert len(html.encode()) < TIER2_MAX_BYTES
    assert detect_block(html, status=200).blocked


def test_tier2_boundary_just_over_threshold_does_not_fire():
    pad = "x" * (TIER2_MAX_BYTES + 200)
    html = f"<html><body><h1>Just a moment</h1>{pad}</body></html>"
    assert len(html.encode()) > TIER2_MAX_BYTES
    assert not detect_block(html, status=200).blocked


def test_empty_body_on_200_is_a_block():
    assert detect_block("<html></html>", status=200).blocked


@pytest.mark.parametrize("status", [404, 410, 500])
def test_empty_body_on_error_status_is_not_a_block(status):
    """A tiny 404/500 body is an honest server answer, not a wall."""
    assert not detect_block("<html></html>", status=status).blocked


# ---------------------------------------------------------------------------
# NOT blocks — the decided policy boundary
# ---------------------------------------------------------------------------


def test_genuine_page_is_not_blocked():
    assert not detect_block(_big_page(), status=200).blocked


def test_paywall_is_not_a_block():
    """A human hits the same page — that IS the site's honest answer."""
    html = _big_page("<h2>Subscribe to continue reading</h2>")
    assert not detect_block(html, status=200).blocked


def test_login_wall_is_not_a_block():
    html = _big_page("<h2>Sign in to view this page</h2>")
    assert not detect_block(html, status=200).blocked


def test_geo_block_is_not_a_block():
    html = _big_page("<h2>This content is not available in your country</h2>")
    assert not detect_block(html, status=200).blocked


def test_age_gate_is_not_a_block():
    html = _big_page("<h2>You must be 18 or older to enter</h2>")
    assert not detect_block(html, status=200).blocked


def test_genuine_404_is_not_a_block():
    html = "<html><body><h1>404 - Page not found</h1></body></html>"
    assert not detect_block(html, status=404).blocked


def test_article_about_bot_detection_is_not_blocked():
    """The realistic false positive: content discussing the very phrases we match."""
    html = _big_page(
        "<h1>How CAPTCHAs work</h1>"
        "<p>Sites show 'Verifying you are human' and 'Just a moment' while "
        "checking your browser before accessing the content.</p>"
    )
    assert not detect_block(html, status=200).blocked


# ---------------------------------------------------------------------------
# Error string contract
# ---------------------------------------------------------------------------


def test_error_string_is_stable_contract():
    assert detect_block(AMAZON_WALL, status=200).error == "blocked:amazon"
    assert detect_block(MYNTRA_WALL, status=200).error == "blocked:akamai"
    assert detect_block(_big_page(), status=403).error == "blocked:unknown"


def test_not_blocked_result_is_falsy_on_blocked_flag():
    assert detect_block(_big_page(), status=200).blocked is False
