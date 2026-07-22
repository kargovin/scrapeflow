"""
Bot-wall detection (BUG-003).

A block is *the server deliberately serving something other than the requested
content because it identified us as a bot*. HTTP status is evidence, not
definition — the canonical case (Amazon "Continue shopping") is a 200 with a
fully valid HTML body, which is exactly why the worker used to store it as a
successful scrape.

Operative test, applied when adding any new signal: **would a real person on a
normal browser and a residential connection have gotten the real content?**
If yes it is a block; if no, that page *is* the site's honest answer. So
paywalls, login walls, geo-blocks, age gates and genuine 404s are deliberately
NOT blocks — reporting them as failures would be wrong.

Posture note: this classifier is the *inverse* of llm-worker's errors.py. That
one fails closed (unknown → terminal) because a wrong "transient" guess re-bills
the user's own API key. Here a wrong "blocked" guess fails a working job, so we
are conservative about *claiming* a block — hence the tiering below.

Detection must run on the raw HTML, before format_output. Markdown jobs lose
every HTML-level signal (scripts, meta, link tags) in conversion.

--------------------------------------------------------------------------
Tiers
--------------------------------------------------------------------------
TIER 1 — high-confidence structural markers. Fire alone, at any page size.
         These are vendor challenge harnesses; no legitimate page ships a
         bot-manager's captcha bootstrap.
TIER 2 — medium-confidence language. Only considered on small pages
         (< TIER2_MAX_BYTES), because the phrases themselves do appear in
         legitimate content — a news article *about* CAPTCHAs, a help page
         explaining bot checks. Size is what separates the wall from the
         article.
TIER 3 — structural integrity (no <body>, negligible visible text, script-only
         shell). NOT IMPLEMENTED YET — see the note at the bottom of this file.

--------------------------------------------------------------------------
Attribution
--------------------------------------------------------------------------
The TIER 1 and TIER 2 vendor patterns are adapted from Crawl4AI's
`antibot_detector.py` (Apache-2.0).

  This product includes software developed by UncleCode
  (https://x.com/unclecode) as part of the Crawl4AI project
  (https://github.com/unclecode/crawl4ai).

Our own prod-verified additions (Amazon in-house, and the text markers matching
the Walmart/Myntra walls captured 2026-07-22) are marked SCRAPEFLOW below —
Crawl4AI's list has no Amazon entry, so these are additive.
"""

import re

# Vendor tokens are a closed set. The error string "blocked:<vendor>" is a
# contract (see open-bugs.md) — these values get parsed and aggregated, so they
# must not drift into free text.
VENDOR_AMAZON = "amazon"
VENDOR_AKAMAI = "akamai"
VENDOR_PERIMETERX = "perimeterx"
VENDOR_DATADOME = "datadome"
VENDOR_CLOUDFLARE = "cloudflare"
VENDOR_IMPERVA = "imperva"
VENDOR_SUCURI = "sucuri"
VENDOR_KASADA = "kasada"
VENDOR_RECAPTCHA = "recaptcha"
VENDOR_HCAPTCHA = "hcaptcha"
VENDOR_UNKNOWN = "unknown"

# Tier 2 only applies below this. Every genuine page observed in prod was
# >= 291 KiB; the three live walls were 411 B, 464 B and 5.4 KiB. Three orders
# of magnitude of separation, so this threshold is not finely balanced.
# (Crawl4AI uses 10_000; we allow more headroom for the 5.4 KiB Amazon wall
# plus any inlined challenge script.)
TIER2_MAX_BYTES = 20_000

# A 200 with essentially no body is a block regardless of markers.
EMPTY_BODY_BYTES = 100

# Statuses that mean "blocked" on their own. NB none of the three live prod
# walls returned one of these — status is the cheapest signal, not the best.
BLOCK_STATUSES = frozenset({403, 429, 503})


# --- TIER 1: high-confidence structural markers, decisive alone -------------
_TIER1: list[tuple[str, re.Pattern[str], str]] = [
    # -- Akamai --
    # The "Access Denied" interstitial carries a Reference # in this exact
    # dotted form. Matches our live Myntra wall (2026-07-22).
    (
        VENDOR_AKAMAI,
        re.compile(r"Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I),
        "akamai_reference_id",
    ),
    (VENDOR_AKAMAI, re.compile(r"Pardon\s+Our\s+Interruption", re.I), "akamai_pardon"),
    (VENDOR_AKAMAI, re.compile(r"errors\.edgesuite\.net", re.I), "akamai_edgesuite"),
    (VENDOR_AKAMAI, re.compile(r"AkamaiGHost", re.I), "akamai_ghost"),
    # -- Cloudflare --
    (
        VENDOR_CLOUDFLARE,
        re.compile(r"challenge-form.*?__cf_chl_f_tk=", re.I | re.S),
        "cloudflare_challenge_form",
    ),
    (
        VENDOR_CLOUDFLARE,
        re.compile(r'<span\s+class="cf-error-code">\d{4}</span>', re.I),
        "cloudflare_error_code",
    ),
    (
        VENDOR_CLOUDFLARE,
        re.compile(r"/cdn-cgi/challenge-platform/\S+orchestrate", re.I),
        "cloudflare_orchestrate",
    ),
    (
        VENDOR_CLOUDFLARE,
        re.compile(r"cf-browser-verification|cf_chl_", re.I),
        "cloudflare_chl",
    ),
    # -- PerimeterX / HUMAN --
    (
        VENDOR_PERIMETERX,
        re.compile(r"window\._pxAppId\s*=", re.I),
        "perimeterx_appid",
    ),
    (
        VENDOR_PERIMETERX,
        re.compile(r"captcha\.px-cdn\.net", re.I),
        "perimeterx_cdn",
    ),
    (VENDOR_PERIMETERX, re.compile(r"_pxhd|/px/captcha", re.I), "perimeterx_misc"),
    # -- DataDome --
    (
        VENDOR_DATADOME,
        re.compile(r"captcha-delivery\.com", re.I),
        "datadome_delivery",
    ),
    (VENDOR_DATADOME, re.compile(r"datadome", re.I), "datadome_name"),
    # -- Imperva / Incapsula --
    (
        VENDOR_IMPERVA,
        re.compile(r"_Incapsula_Resource", re.I),
        "imperva_resource",
    ),
    (
        VENDOR_IMPERVA,
        re.compile(r"Incapsula\s+incident\s+ID", re.I),
        "imperva_incident",
    ),
    # -- Sucuri --
    (
        VENDOR_SUCURI,
        re.compile(r"Sucuri\s+WebSite\s+Firewall", re.I),
        "sucuri_firewall",
    ),
    # -- Kasada --
    (
        VENDOR_KASADA,
        re.compile(r"KPSDK\.scriptStart\s*=\s*KPSDK\.now\(\)", re.I),
        "kasada_kpsdk",
    ),
    # -- Amazon in-house (SCRAPEFLOW — not in Crawl4AI's list) --
    # "opfcaptcha" is the captcha subdomain leaked into the page's own
    # instrumentation config. NB the wall is served *at the requested product
    # URL* — there is no redirect, so final_url is useless here.
    (
        VENDOR_AMAZON,
        re.compile(r"opfcaptcha\.amazon\.com", re.I),
        "amazon_opfcaptcha",
    ),
    (
        VENDOR_AMAZON,
        re.compile(r"csm-captcha-instrumentation", re.I),
        "amazon_captcha_instrumentation",
    ),
    (
        VENDOR_AMAZON,
        re.compile(r'action="/errors/validateCaptcha"', re.I),
        "amazon_validate_captcha",
    ),
    # -- Generic, but structural enough to stand alone --
    (
        VENDOR_UNKNOWN,
        re.compile(r"blocked\s+by\s+network\s+security", re.I),
        "generic_network_security",
    ),
]


# --- TIER 2: medium-confidence, only on small pages -------------------------
_TIER2: list[tuple[str, re.Pattern[str], str]] = [
    (VENDOR_RECAPTCHA, re.compile(r"g-recaptcha|recaptcha/api\.js", re.I), "recaptcha"),
    (VENDOR_HCAPTCHA, re.compile(r"h-captcha|hcaptcha\.com", re.I), "hcaptcha"),
    (VENDOR_UNKNOWN, re.compile(r"Access Denied", re.I), "access_denied"),
    (
        VENDOR_UNKNOWN,
        re.compile(r"Access to This Page Has Been Blocked", re.I),
        "page_blocked",
    ),
    (VENDOR_UNKNOWN, re.compile(r"Just a moment", re.I), "just_a_moment"),
    (
        VENDOR_UNKNOWN,
        re.compile(r"Checking your browser before accessing", re.I),
        "checking_browser",
    ),
    (
        VENDOR_UNKNOWN,
        re.compile(r"blocked\s+by\s+security", re.I),
        "blocked_by_security",
    ),
    (VENDOR_UNKNOWN, re.compile(r"Request unsuccessful", re.I), "request_unsuccessful"),
    (VENDOR_UNKNOWN, re.compile(r"Verifying you are human", re.I), "verifying_human"),
    (VENDOR_UNKNOWN, re.compile(r"Robot Check", re.I), "robot_check"),
    (VENDOR_UNKNOWN, re.compile(r"Are you a robot", re.I), "are_you_a_robot"),
    (
        VENDOR_UNKNOWN,
        re.compile(r"unusual traffic from your computer network", re.I),
        "unusual_traffic",
    ),
    (
        VENDOR_UNKNOWN,
        re.compile(r"Enable JavaScript and cookies to continue", re.I),
        "enable_js_cookies",
    ),
    # SCRAPEFLOW — matches our live Walmart wall (PerimeterX-served, but the
    # phrase is the only thing surviving into some renderings).
    (VENDOR_PERIMETERX, re.compile(r"Robot or human\?", re.I), "robot_or_human"),
    # SCRAPEFLOW — matches our live Amazon wall body text.
    (VENDOR_AMAZON, re.compile(r"Continue shopping", re.I), "continue_shopping"),
]


class BlockDetection:
    """Outcome of classifying one rendered page.

    `signals` records *why* the classifier fired. It is logged, not persisted —
    when a false positive turns up, that list is what makes it debuggable, and
    it is also how `blocked:unknown` rows get triaged into new fingerprints.
    """

    def __init__(
        self,
        blocked: bool,
        vendor: str | None = None,
        tier: int | None = None,
        signals: list[str] | None = None,
    ) -> None:
        self.blocked = blocked
        self.vendor = vendor
        self.tier = tier
        self.signals = signals or []

    @property
    def error(self) -> str:
        """The `error` value published on the failed ResultMessage."""
        return f"blocked:{self.vendor or VENDOR_UNKNOWN}"


NOT_BLOCKED = BlockDetection(False)


def detect_block(
    html: str,
    status: int | None = None,
    final_url: str | None = None,
) -> BlockDetection:
    """Classify a rendered page as a bot wall or genuine content.

    Called after `final_url = page.url` and before `format_output` — the one
    point where status, final URL and raw HTML are all in hand.

    `status` is optional because `page.goto()` returns None for same-document
    navigations; absence of a status is not evidence of anything.
    """
    body_bytes = len(html.encode())

    # TIER 1 — decisive alone, any size.
    for vendor, pattern, name in _TIER1:
        if pattern.search(html):
            signals = [f"tier1:{name}"]
            if status is not None:
                signals.append(f"status:{status}")
            return BlockDetection(True, vendor, 1, signals)

    # Explicit block status — decisive alone.
    if status in BLOCK_STATUSES:
        return BlockDetection(True, VENDOR_UNKNOWN, 1, [f"status:{status}"])

    # A *200* with essentially nothing in it. Gated on 200 (or an absent
    # status) deliberately: a 404 or 500 with a tiny body is an honest server
    # answer, not a wall, and failing it would violate the policy above.
    if body_bytes < EMPTY_BODY_BYTES and status in (200, None):
        return BlockDetection(
            True, VENDOR_UNKNOWN, 1, [f"empty_body:{body_bytes}", f"status:{status}"]
        )

    # TIER 2 — only on small pages. On a full-size page these phrases are
    # almost certainly legitimate content discussing bot checks.
    if body_bytes < TIER2_MAX_BYTES:
        for vendor, pattern, name in _TIER2:
            if pattern.search(html):
                signals = [f"tier2:{name}", f"small_body:{body_bytes}"]
                if status is not None:
                    signals.append(f"status:{status}")
                return BlockDetection(True, vendor, 2, signals)

    # TIER 3 (structural integrity) is deliberately not implemented yet:
    # missing <body>, < 50 chars of visible text, script-only shells. It is the
    # highest-false-positive tier — SPA shells and JSON/XML responses look
    # exactly like that — so it needs its own evidence base before it can fail
    # a user's job. Revisit when a `blocked:unknown` backlog shows walls that
    # tiers 1 and 2 miss.

    return NOT_BLOCKED
