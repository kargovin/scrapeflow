"""Chrome's launch flags — the stealth set (ADR-008) plus plain-http navigation (BUG-034)."""

from worker.config import Settings, browser_launch_args, settings


def test_defaults_carry_the_stealth_flags_and_allow_http():
    assert browser_launch_args(settings) == [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=HttpsFirstBalancedModeAutoEnable,HttpsUpgrades",
    ]


def test_allow_http_can_be_switched_off():
    s = Settings(
        credentials_encryption_key=settings.credentials_encryption_key,
        playwright_allow_http=False,
    )
    assert not any("HttpsFirst" in a for a in browser_launch_args(s))
