# Anti-Bot Hardening — Playwright Worker Stealth

> **Audience:** Anyone touching `playwright-worker/` launch/fingerprint behaviour, or
> debugging why scrapes get blocked.
> **Decision record:** [`docs/adr/ADR-008-playwright-antibot-hardening.md`](../adr/ADR-008-playwright-antibot-hardening.md)
> **Date:** 2026-07-03

This guide documents exactly what was changed to get the Playwright worker past
fingerprint-level bot detection, **why each piece is needed**, and how to re-verify it.
The short version: the worker was running stock legacy-headless Playwright Chromium, which
announces itself as automation on the three cheapest checks any anti-bot runs. We swapped
to a stealth Playwright fork running real, headed Google Chrome.

---

## Part 1 — The problem and the diagnosis

Scrapes were being blocked **despite residential and datacenter proxies**. Rotating IPs
didn't help → the problem was the browser, not the IP.

We ran [BrowserScan's bot-detection page](https://www.browserscan.net/bot-detection) from
inside the worker (through a proxy). Verdict: **Robot**, failing three checks. The Navigator
dump named the culprits precisely:

```
webdriver      : true
userAgent      : Mozilla/5.0 (X11; Linux x86_64) ... HeadlessChrome/125.0.6422.26 Safari/537.36
userAgentData  : brands = [ HeadlessChrome, Chromium, Not.A/Brand ]
CDP            : Robot
```

| Failure | Meaning |
|---|---|
| **WebDriver** | `navigator.webdriver === true` — the #1 cheapest bot check, run first by everyone |
| **User-Agent** | The literal string `HeadlessChrome` in both the UA and the Client Hints brands |
| **CDP** | The Chrome DevTools Protocol `Runtime.enable` leak — Playwright's automation channel is detectable |

Everything else (canvas, WebGL, timezone, the rest of Navigator) **passed**. So this was
*not* deep fingerprinting or TLS detection — it was the browser waving a "I am a bot" flag.
That's also why the proxies were useless: no IP saves you when `navigator.webdriver` is
`true` and the UA says `HeadlessChrome`.

---

## Part 2 — The investigation (what actually works)

The landscape is full of half-truths, so each fix was verified empirically inside the built
image rather than trusted from a blog. Two findings overturned the "obvious" approach:

**Finding 1 — Patchright's drop-in `launch()` does *not* clear `navigator.webdriver`.**
Installing Patchright and calling `chromium.launch()` left `webdriver = true`. The explicit
launch flag `--disable-blink-features=AutomationControlled` is what flips it to `false`.

**Finding 2 — *No* headless mode gives a clean UA, including `--headless=new`.**
The common advice is "use new headless." Verified false: new headless (real Chrome 150)
still emits `HeadlessChrome` in `navigator.userAgent`. Only a **truly headed** browser
(`headless=False`, under Xvfb in a container) reports a clean `Chrome/<ver>` UA.

The matrix that produced these conclusions:

| Config | `navigator.webdriver` | UA has "Headless" |
|---|---|---|
| Patchright `launch()`, headless, no flags | **true** | **yes** |
| Patchright `launch()`, headless, `--headless=new` | **true** | **yes** |
| Patchright `launch()`, headless, `+disable-blink-features=AutomationControlled` | false | **yes** |
| **Patchright `launch()`, headed under Xvfb, `+disable-blink-features=…`** | **false** | **no** ✅ |

The last row is the shipped config.

---

## Part 3 — The final configuration

### Engine: Patchright (stealth Playwright fork)

`playwright-worker/pyproject.toml`:

```toml
dependencies = [
    "patchright>=1.44.0",   # was: "playwright==1.44.0"
    ...
]
```

Patchright is import-compatible (`from patchright.async_api import async_playwright`). Its
job here is the one thing launch flags and JavaScript **cannot** do: patch the CDP
`Runtime.enable` leak behind the **CDP** failure. It vendors its own compatible Playwright,
so we no longer pin `playwright`.

### Launch: real Chrome, headed, hardened

`worker/main.py`:

```python
launch_args = []
if settings.playwright_no_sandbox:
    launch_args.append("--no-sandbox")
if settings.playwright_disable_dev_shm:
    launch_args.append("--disable-dev-shm-usage")
if settings.playwright_disable_automation:
    launch_args.append("--disable-blink-features=AutomationControlled")

browser = await pw.chromium.launch(
    channel=settings.playwright_channel or None,   # "chrome"
    headless=settings.playwright_headless,          # False
    args=launch_args,
)
```

| Piece | Why |
|---|---|
| `channel="chrome"` | Real Google Chrome → genuine `userAgentData.brands` (Google Chrome), matches a real build |
| `headless=False` | The **only** mode with a clean UA (see Finding 2); headless is itself a signal |
| `--disable-blink-features=AutomationControlled` | Removes `navigator.webdriver` (Finding 1) |
| `--no-sandbox` | Chrome can't launch as a non-root user in the container without it |
| `--disable-dev-shm-usage` | k8s `/dev/shm` defaults to 64 MB; headed Chrome exhausts it and crashes |

### Context: no viewport override, no UA spoofing

`worker/worker.py`:

```python
context_kwargs: dict = {"no_viewport": True}
```

`no_viewport=True` uses the real window size (the Xvfb screen) instead of a forced viewport.
A forced viewport is applied via the CDP `Emulation.setDeviceMetricsOverride` call, which is
a bot-detection signal. We deliberately do **not** set `user_agent` or extra headers —
headed real Chrome already gives a clean, self-consistent UA, and Patchright advises against
overriding it (it risks UA / `userAgentData` / `Sec-CH-UA` mismatches).

### Config knobs

`worker/config.py` (all env-overridable, prefix as-is):

| Setting | Default | Purpose |
|---|---|---|
| `playwright_channel` | `"chrome"` | Browser channel; `""` → Patchright bundled Chromium |
| `playwright_headless` | `False` | Truly headed (needs Xvfb) |
| `playwright_disable_automation` | `True` | Adds `--disable-blink-features=AutomationControlled` |
| `playwright_no_sandbox` | `True` | Adds `--no-sandbox` |
| `playwright_disable_dev_shm` | `True` | Adds `--disable-dev-shm-usage` |

### Image

`playwright-worker/Dockerfile`:

```dockerfile
# Real Google Chrome for channel="chrome" — as root, before dropping privileges.
RUN patchright install chrome

# Headed Chrome writes its profile/cache under $HOME.
RUN useradd --create-home --uid 1001 appuser
ENV HOME=/home/appuser
USER appuser

# Truly-headed Chrome needs a virtual display in a container.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", "python", "-m", "worker.main"]
```

---

## Part 4 — Verification

### Unit tests

The suite mocks the browser and does not import `main.py`, so the engine swap is test-safe.
Two `new_context` assertions were updated for `no_viewport=True`, and a pre-existing broken
proxy test (stale since the `63b2dfc` userinfo-split) was fixed at the same time.

```bash
cd playwright-worker
docker build -t scrapeflow-playwright:stealth .
docker run --rm --user root -v "$PWD":/app -w /app scrapeflow-playwright:stealth \
    python -m pytest tests/ -q
# → 70 passed
```

### Fingerprint check (the real proof)

Launch exactly as the worker does and read the tells back. Under Xvfb, headed real Chrome:

```bash
docker run --rm -e CREDENTIALS_ENCRYPTION_KEY=<fernet-key> scrapeflow-playwright:stealth \
    xvfb-run -a --server-args="-screen 0 1920x1080x24" python - <<'PY'
import asyncio
from patchright.async_api import async_playwright
async def main():
    pw = await async_playwright().start()
    b = await pw.chromium.launch(channel="chrome", headless=False,
        args=["--no-sandbox","--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"])
    p = await (await b.new_context(no_viewport=True)).new_page()
    print("webdriver:", await p.evaluate("navigator.webdriver"))
    print("ua       :", await p.evaluate("navigator.userAgent"))
    await b.close(); await pw.stop()
asyncio.run(main())
PY
```

Verified output:

```
webdriver: False
ua       : Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36
```

`webdriver` and `User-Agent` are confirmed fixed. **CDP** rides on Patchright's patch and
must be re-confirmed against BrowserScan from the deployed worker (see below).

---

## Part 5 — Operational runbook

1. **Build & push** the worker image through the normal CI/registry flow (the Xvfb wrapper
   is baked into the image `CMD`; no manifest `command` override needed).
2. **Bump memory** on the `scrapeflow-playwright-worker` Deployment in
   `govindappa-k8s-config` — headed Chrome + Xvfb needs materially more RAM than the old
   headless Chromium, multiplied by `PLAYWRIGHT_MAX_WORKERS` (3). **Not yet applied.**
3. **Re-run BrowserScan** from the deployed worker (submit a job targeting
   `https://www.browserscan.net/bot-detection` and inspect the stored output) to confirm
   **all three** checks — including CDP — now report Normal.
4. If a hard target (Cloudflare Turnstile / DataDome) still blocks after this, the next
   layers are **TLS/JA3 fingerprinting** and **behavioral simulation** — diagnose per-target
   (BrowserScan `/tls`, creepjs) before investing; see ADR-008 "Residual risks."

---

## Part 6 — Known limitations

- **Fingerprint-level only.** This does not touch TLS/JA3 or behavioral analysis.
- **`page.route` on action jobs** (CSP injection / `block_images`) uses the CDP `Fetch`
  domain, slightly reducing stealth for those jobs. Plain scrapes are unaffected.
- **Patchright floats ahead of the pinned base image's Playwright.** `channel="chrome"`
  sidesteps the bundled browsers, so drift is low-risk today but worth watching on upgrades.
