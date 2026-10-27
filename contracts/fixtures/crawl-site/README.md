# Crawl gate fixture site (E.6)

No v1 crawl ever completed (ADR-009 13a), so the crawl lane has no reference run to diff
against. This site is the reference instead: a known link graph, a sitemap index, a
robots.txt, and every admission case ADR-009 13d and ADR-010 §2 name. `expected.json`
is the page set, outcome and byte count the crawl must produce exactly.

| Case | Where | Expected |
|---|---|---|
| Link graph, two routes to one page | `/` → `a`, `b` → `c` | `c` fetched once, depth 2 |
| Fragment | `/b.html#section` | admitted as `/b.html` |
| Off-origin link, `mailto:` | `/` | never reach admission |
| robots.txt `Disallow` | `/private/secret.html` | page `failed`, `robots_txt_disallowed`, nothing charged |
| Sitemap index | `sitemap.xml` → `sitemap-pages.xml` | read as an index, not as pages |
| Sitemap-only page | `d.html` | admitted at depth 1 |
| Subdomain of the seed's eTLD+1 | `docs.crawlfixture.com/guide.html` | admitted |
| Unrelated domain, suffix trick | `unrelated-fixture.com`, `crawlfixture.com.evil.net` | `out_of_scope` × 2 |
| In-scope host that resolves privately | `internal.crawlfixture.com/secret.html` | `ssrf`, never fetched |
| SSRF-bait sitemap *file* | `Sitemap: http://internal.crawlfixture.com/bait-sitemap.xml` | `sitemap_ssrf`, never fetched |
| Metadata-address sitemap file | `Sitemap: http://169.254.169.254/...` | `sitemap_out_of_scope` |

## How it is served (compose)

`crawl-fixture` (profile `gate`) is nginx on two networks:

- `crawl-fixture` — subnet `11.222.33.0/24`, outside every range the SSRF check refuses, so
  the platform sees `www.` and `docs.crawlfixture.com` as public sites with the check
  unchanged. The api, workflow-worker and both Temporal scrape workers join it.
- the default network, where `internal.crawlfixture.com` resolves to the container's
  private address — the in-scope-but-internal case.

## Run

```bash
# from ./docker
docker compose --profile gate up -d crawl-fixture
docker compose exec -T api uv run python -m scripts.crawl_gate < ../contracts/fixtures/crawl-site/expected.json
docker compose logs crawl-fixture | grep -E 'bait-sitemap|GET /secret' && echo "BAIT FETCHED" || echo "bait never fetched"
```

`contracts/test_crawl_fixture.py` keeps `expected.json`'s byte counts tied to the files.
