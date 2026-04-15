# PRD-009 — Pre-crawl Page Actions

**Priority:** P2
**Source:** NEW — identified from firecrawl (`actions` array on scrape options)
**Status:** Ready for Architect
**Last updated:** 2026-04-15

---

## Problem

The Playwright worker currently navigates to a URL and extracts content. Many real-world pages require interaction before the useful content is visible: clicking "Accept Cookies", waiting for a lazy-loaded component, scrolling to trigger infinite scroll, or filling a search box. Without the ability to choreograph page actions, the Playwright worker is significantly limited for dynamic sites.

Firecrawl ships a first-class `actions` array on its scrape API. Crawl4ai supports JavaScript execution and virtual scroll configuration. This PRD adopts firecrawl's `actions` model as it is the clearest API design.

---

## Goals

1. Allow users to define an ordered sequence of browser actions to execute after page load and before content extraction.
2. Actions apply only to Playwright jobs (Go HTTP worker ignores them — it has no browser context).
3. Actions are defined in the job config (stored with the job, reused on each scheduled run).
4. Supported action types cover the 80% case: wait, click, scroll, type, execute JavaScript.

---

## Non-goals

- Visual element detection / AI-assisted clicking (Phase 4)
- Actions on the Go HTTP worker (no browser context)
- Looping or conditional actions (Phase 4 — actions are a flat ordered list)
- Recording actions from a browser session (Phase 4 tooling)
- Taking intermediate screenshots between actions (screenshot action captures the final state only)

---

## User stories

**As a user** scraping a site with a GDPR cookie banner, I want to add a `click` action to dismiss the banner before content extraction so my output doesn't include banner text.

**As a user** scraping an infinite-scroll feed, I want to add `scroll` actions to load multiple pages of content before extraction.

**As a user** who needs to trigger a JavaScript-rendered component, I want to execute a JS snippet that opens a dropdown or tab before extraction.

**As a user**, I want these actions to execute on every scheduled run of the job — not just once.

---

## Requirements

### Actions schema

`actions` is an ordered array of action objects on the job config:

```json
[
  {"type": "wait", "milliseconds": 2000},
  {"type": "click", "selector": "#accept-cookies"},
  {"type": "wait_for_selector", "selector": ".content-loaded", "timeout": 5000},
  {"type": "scroll", "direction": "down", "amount": 3},
  {"type": "type", "selector": "input[name=search]", "text": "example query"},
  {"type": "press", "key": "Enter"},
  {"type": "execute_js", "script": "document.querySelector('.load-more').click()"},
  {"type": "screenshot"}
]
```

### Action types and parameters

| Type | Required params | Optional params | Notes |
|------|----------------|-----------------|-------|
| `wait` | `milliseconds: int` | — | Hard sleep; 1–10000ms |
| `wait_for_selector` | `selector: str` | `timeout: int` (default 5000ms) | Fails action sequence if selector not found within timeout |
| `click` | `selector: str` | `timeout: int` | Fails if element not found |
| `type` | `selector: str`, `text: str` | — | Types into an input element |
| `press` | `key: str` | — | Keyboard key name (e.g. "Enter", "Tab", "Escape") |
| `scroll` | `direction: "up"\|"down"` | `amount: int` (default 1, in viewport heights) | Scrolls the page |
| `execute_js` | `script: str` | — | Arbitrary JS; return value is discarded |
| `screenshot` | — | — | Captures page screenshot at this point; stored in MinIO alongside the job result |

### Validation at job creation

- Maximum 20 actions per job
- `selector` values are CSS selectors; basic format validation (must be non-empty string)
- `execute_js` scripts: no length limit in Phase 3 (operator-level concern); flagged for Phase 4 sandboxing
- `milliseconds` on `wait`: capped at 10000ms per action
- `type` on unsupported engine: 422 error — "actions require engine: playwright"

### Playwright worker execution

Actions execute sequentially after `page.goto(url)` resolves (DOMContentLoaded equivalent), before extraction:

1. Execute each action in order
2. If any action fails (selector not found within timeout, JS exception): log the failure, continue with the next action (partial failure is better than abandoning the whole scrape)
3. After all actions complete: proceed with content extraction as normal
4. If `screenshot` action is in the list: store screenshot to MinIO and include the path in the result

### Storage

`actions` stored as JSONB on the `jobs` table. Included in the NATS fat message so the worker executes without DB access.

### Applying to engine: http

If `actions` is set and `engine: http`, the API returns 422: `"actions are only supported with engine: playwright"`.

---

## Success criteria

- [ ] A `click` action on a cookie banner results in extraction content without the banner text
- [ ] A `wait_for_selector` that times out logs the failure but the scrape proceeds with available content
- [ ] A `scroll` action followed by extraction captures content loaded by infinite scroll
- [ ] An `execute_js` action can trigger JS-driven UI changes visible in the extracted content
- [ ] A `screenshot` action stores a PNG in MinIO and the path is returned in the job result
- [ ] `actions` set on an `engine: http` job returns 422 at job creation
- [ ] 21 actions in the array returns 422 at job creation
- [ ] Scheduled jobs execute the same action sequence on each run

---

## Open questions for Architect

1. The current Playwright worker is written in Python (playwright-python). Actions like `click`, `wait_for_selector`, and `execute_js` map directly to playwright-python's API. Is the existing worker structure (single async function per job) easy to extend with an action loop, or does it need refactoring first?
2. Should action failures (selector timeout, JS exception) produce a warning in the job result, or should they be completely silent? A `warnings: []` field on the job run result may be useful for debugging.
3. `execute_js` allows arbitrary code execution inside the browser context. For a multi-tenant platform, is sandboxing a Phase 3 concern or acceptable to defer? (Note: JS runs in the browser's sandbox, not on the server — but network requests from that JS could be a concern if proxy or auth contexts are active.)
