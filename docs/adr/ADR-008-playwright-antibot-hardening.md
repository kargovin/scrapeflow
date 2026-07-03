# ADR-008: Playwright Worker Anti-Bot Hardening

**Status:** Accepted
**Date:** 2026-07-03
**Deciders:** @karthik

---

## Context

Production scrapes routed to the Playwright worker were being blocked by bot-detection
systems **even when using residential and datacenter proxies**. Rotating IPs did not
help, which pointed away from IP reputation and toward **browser fingerprinting**.

A diagnostic run against [BrowserScan's bot-detection test](https://www.browserscan.net/bot-detection)
(from inside the deployed worker, through a proxy) returned a verdict of **Robot** with
three specific failures:

| Failed check | Evidence in the fingerprint dump | Root cause |
|---|---|---|
| **WebDriver** | `navigator.webdriver: true` | The automation flag was fully exposed |
| **User-Agent** | `...HeadlessChrome/125.0.6422.26...`; `userAgentData.brands` included `HeadlessChrome` | Bundled Chromium running in **legacy headless** writes "HeadlessChrome" into the UA string *and* Client Hints brands |
| **CDP** | (behavioral probe) | Playwright's `Runtime.enable` DevTools call is detectable |

Everything else passed — canvas, WebGL, timezone consistency, and the rest of the
Navigator surface. So the failure was **not** deep fingerprint or TLS-level detection;
the worker was running **stock, unhardened, legacy-headless Playwright Chromium**, which
is trivially flagged by the *cheapest* checks every anti-bot runs first. The residential
proxies were wasted because the browser was announcing itself as automation before IP
reputation ever mattered.

The worker previously launched with `pw.chromium.launch(headless=True)` and created a
default `browser.new_context()` — no channel, no launch hardening, no fingerprint
consideration.

**Scope note.** This is a single-service hardening decision (only `playwright-worker/`),
which would normally live in `ARCHITECTURE_DECISIONS.md`. It is recorded as an ADR
because it changes the service's **runtime shape** (a browser-engine dependency swap,
a system Chrome install, and a virtual-display requirement) with **cross-cutting infra
consequences** (k8s memory, `/dev/shm`), and because the "what actually works" findings
are non-obvious and expensive to re-derive.

---

## Decisions

### 1. Replace Playwright with Patchright (drop-in stealth fork)

`playwright==1.44.0` → `patchright` in `playwright-worker/pyproject.toml`. Patchright is
an import-compatible fork (`from patchright.async_api import async_playwright`) whose
headline feature is patching the **`Runtime.enable` CDP leak** — the mechanism behind
the failing **CDP** check, which cannot be fixed from launch flags or JavaScript. It also
provides a patched `chrome.runtime` and other automation-surface fixes. Patchright vendors
its own compatible Playwright, so we no longer pin `playwright` directly.

### 2. Use real Google Chrome via `channel="chrome"`

The image installs branded Google Chrome (`patchright install chrome`) and the worker
launches with `channel="chrome"`. Real Chrome (vs bundled Chromium) reports genuine
`userAgentData.brands` (`Google Chrome`) and matches a real browser build. Configurable
via `PLAYWRIGHT_CHANNEL` (set to `""` to fall back to Patchright's bundled Chromium).

### 3. Run **truly headed** under Xvfb — not headless

`headless=False`, with the container `CMD` wrapped in `xvfb-run` to provide a virtual
display. This is the decision with the least-obvious justification:

> **Every headless mode still leaks the `HeadlessChrome` UA token — including the "new"
> headless mode (`--headless=new`).** Only a *truly headed* Chrome reports a clean
> `Chrome/<ver>` user agent.

This was verified empirically (see the guide). It also aligns with the general principle
that "headless" is itself a detection signal, so headed is strictly better for evasion.

### 4. Remove `navigator.webdriver` with a launch flag

Launch arg `--disable-blink-features=AutomationControlled`. **Patchright's driver-level
patches alone did not clear `navigator.webdriver` in `launch()` mode** — this explicit
flag is what flips it to `false`. Configurable via `PLAYWRIGHT_DISABLE_AUTOMATION`.

### 5. No user-agent spoofing; `no_viewport=True`

Per Patchright guidance (and because headed real Chrome already gives a clean UA), the
worker does **not** override `user_agent` or inject headers. `browser.new_context()` sets
`no_viewport=True` so the page uses the real window size instead of a forced viewport —
a forced viewport goes through the CDP `Emulation.setDeviceMetricsOverride` call, which is
itself a bot-detection signal.

### 6. Container-safety launch flags

`--no-sandbox` (Chrome cannot start as a non-root user in the container otherwise) and
`--disable-dev-shm-usage` (k8s pods default to a 64 MB `/dev/shm`, which headed Chrome
exhausts and crashes on). Neither is JavaScript-detectable — both are process-level flags.

### 7. Image changes

`playwright-worker/Dockerfile`:
- `RUN patchright install chrome` (as root, before dropping privileges, so the binary
  lands in a world-readable system path).
- Runtime user switched to `--create-home` with `ENV HOME=/home/appuser` — headed Chrome
  writes its profile/cache under `$HOME`.
- `CMD` wrapped in `xvfb-run -a --server-args="-screen 0 1920x1080x24"`.

---

## Alternatives considered

| Option | Why rejected |
|---|---|
| **`invisible_playwright`** (the repo that prompted this) | A patched **Firefox** binary (~100 MB, C++ source patches). Swapping the whole engine is a heavy lift for a Dockerized Chromium worker; Chromium-native equivalents were a far smaller change. |
| **`rebrowser-patches`** | Node-first; requires cloning Playwright and building from source (5–15 min). Marginal Cloudflare edge not worth the toolchain change for a Python worker. |
| **Launch-flag hardening only, no fork** | `--disable-blink-features=AutomationControlled` fixes `webdriver`, but **nothing at the launch-flag or JS level fixes the CDP `Runtime.enable` leak** — that requires a patched driver (Patchright/rebrowser). |
| **New headless (`--headless=new`) instead of Xvfb** | Operationally simpler (no virtual display), but **verified to still leak the `HeadlessChrome` UA token** — it does not fix the User-Agent failure. |
| **UA spoofing** (override `user_agent` to strip "Headless") | Patchright explicitly advises against it; risks UA / `userAgentData` / `Sec-CH-UA` inconsistency. Headed real Chrome makes it unnecessary. |

---

## Consequences

**Positive:**
- All three BrowserScan failures resolved. Directly verified in the built image:
  `navigator.webdriver = false`, UA = `...Chrome/150.0.0.0 Safari/537.36` (no "Headless"),
  `userAgentData.brands = [Not;A=Brand, Chromium, Google Chrome]`. CDP relies on
  Patchright's patch (see "Residual risks").
- Clears **fingerprint-level** detection — the layer the worker was actually failing.
- Config is env-tunable; no code change needed to fall back to Chromium or headless for debugging.

**Negative / operational:**
- **Memory.** Headed Chrome + Xvfb uses materially more RAM than the old headless
  Chromium. The `scrapeflow-playwright-worker` Deployment in `govindappa-k8s-config`
  needs a memory-limit bump, and this interacts with `PLAYWRIGHT_MAX_WORKERS=3`
  (3 concurrent headed Chrome contexts). **Infra follow-up, not yet applied.**
- **Image size** grows (branded Chrome ~ the pulled 150.x build) and `patchright install
  chrome` runs `apt`/dpkg at build time (needs network).
- **Patchright vendors its own Playwright**, which floats ahead of the pinned
  `v1.44.0` base image. `channel="chrome"` sidesteps the bundled browsers, so the drift
  is low-risk today, but a future Patchright bump could surface API changes.

**Residual risks (re-verify these):**
- **CDP could not be verified locally** — BrowserScan's CDP probe needs a live browser
  session against their page. It rides on Patchright's `Runtime.enable` patch (its core
  feature). **Re-run BrowserScan against the deployed worker to confirm the CDP check
  flips to Normal.** `webdriver` and `User-Agent` were verified directly.
- **`page.route` on action jobs** (CSP injection / `block_images`) uses the CDP `Fetch`
  domain, which slightly reduces Patchright's stealth for those specific jobs. Plain
  scrapes are unaffected.
- **This is a ceiling, not a silver bullet.** It clears fingerprint-level detection but
  does **not** address TLS/JA3 fingerprinting or behavioral analysis. Hard targets
  (Cloudflare Turnstile, DataDome) may need those layers next; diagnose per-target before
  investing.

---

## Implementation reference

Full method, the config matrix that produced these findings, verification commands, and
the operational runbook are in the companion guide:
[`docs/guides/anti-bot-hardening.md`](../guides/anti-bot-hardening.md).
