"""PRD-016 R6 — the acceptance gate: today's job recipe vs the same recipe as a pipeline.

Runs anywhere with python3 (stdlib only), against the API over HTTPS, as the key's owner:

    SCRAPEFLOW_API_KEY=sf_... python3 api/scripts/r6_gate.py --llm-key-id <id> --model <model> \\
        (--webhook-url https://your.receiver/path | --webhook-site)

Creates one v1 job (scrape -> LLM -> webhook) and one pipeline with the same three steps, on the same
URL, schema, LLM key and model; runs the job once and the pipeline twice (the second run measures the
repeat cost). Judged on structure and mechanics, not byte equality (R6). ``--webhook-site`` creates a
webhook.site inbox — a third party that receives both payloads (ids and storage paths, no content) —
and reads them back to compare shape and verify signatures; with ``--webhook-url`` check them yourself.
Leaves the job and pipeline in place; their ids are printed.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["title", "summary"],
}
PIPELINE_SECRET = "r6-gate-secret"
TERMINAL = {"completed", "failed", "cancelled"}
# PRD-016 R6's two payload exceptions (decided at C.9).
JOB_ONLY = {"job_id"}
PIPELINE_ONLY = {"pipeline_id", "pipeline_run_id"}


def call(method: str, url: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


class Api:
    def __init__(self, base: str, key: str):
        self.base, self.headers = base.rstrip("/"), {"X-API-Key": key}

    def __call__(self, method: str, path: str, body=None, ok=(200, 201, 202)):
        status, data = call(method, self.base + path, body, self.headers)
        if status not in ok:
            sys.exit(f"{method} {path} -> {status}: {data}")
        return data


def wait(fetch, label: str, limit_s: int = 900):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        item = fetch()
        if item["status"] in TERMINAL:
            print(f"  {label}: {item['status']} after {time.monotonic() - t0:.0f}s")
            return item
        time.sleep(5)
    sys.exit(f"{label} did not finish in {limit_s}s")


def json_keys(content: str) -> set[str]:
    try:
        value = json.loads(content)
    except ValueError:
        return set()
    return (
        {k for k, v in value.items() if v not in (None, "", [], {})}
        if isinstance(value, dict)
        else set()
    )


def webhook_site_inbox() -> tuple[str, str]:
    status, data = call("POST", "https://webhook.site/token", {})
    if status not in (200, 201):
        sys.exit(f"webhook.site token -> {status}")
    return data["uuid"], f"https://webhook.site/{data['uuid']}"


def webhook_site_requests(token: str) -> list[dict]:
    status, data = call(
        "GET", f"https://webhook.site/token/{token}/requests?sorting=oldest&per_page=50"
    )
    if status != 200:
        sys.exit(f"webhook.site requests -> {status}")
    return data.get("data", [])


def header(request: dict, name: str) -> str | None:
    value = {k.lower(): v for k, v in (request.get("headers") or {}).items()}.get(name.lower())
    return value[0] if isinstance(value, list) else value


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--api", default=os.environ.get("SCRAPEFLOW_API", "https://scrapeflow.govindappa.com")
    )
    parser.add_argument("--url", default="https://example.com/")
    parser.add_argument("--llm-key-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--schema", default=json.dumps(DEFAULT_SCHEMA), help="output schema as JSON"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--webhook-url")
    target.add_argument("--webhook-site", action="store_true")
    args = parser.parse_args()
    key = os.environ.get("SCRAPEFLOW_API_KEY") or sys.exit("set SCRAPEFLOW_API_KEY")
    api = Api(args.api, key)
    schema = json.loads(args.schema)

    token, hook = webhook_site_inbox() if args.webhook_site else (None, args.webhook_url)
    print(f"R6 gate against {args.api} — url {args.url}, model {args.model}, webhook {hook}")

    job = api(
        "POST",
        "/jobs",
        {
            "url": args.url,
            "output_format": "html",
            "llm_config": {
                "llm_key_id": args.llm_key_id,
                "model": args.model,
                "output_schema": schema,
            },
            "webhook_url": hook,
        },
    )
    pipeline = api(
        "POST",
        "/pipelines",
        {
            "name": f"r6-gate-{int(time.time())}",
            "definition": {
                "blocks": [
                    {
                        "id": "fetch",
                        "type": "scrape",
                        "config": {"url": args.url, "output_format": "html"},
                    },
                    {
                        "id": "extract",
                        "type": "llm_extract",
                        "input": "fetch",
                        "config": {
                            "llm_key_id": args.llm_key_id,
                            "model": args.model,
                            "output_schema": schema,
                        },
                    },
                    {
                        "id": "notify",
                        "type": "webhook",
                        "input": "extract",
                        "config": {"url": hook, "secret": PIPELINE_SECRET},
                    },
                ]
            },
        },
    )
    pid = pipeline["id"]
    print(f"  job {job['id']} · pipeline {pid}")

    runs = []
    first = api("POST", f"/pipelines/{pid}/runs", {})
    job = wait(lambda: api("GET", f"/jobs/{job['id']}"), "job")
    runs.append(wait(lambda: api("GET", f"/pipelines/{pid}/runs/{first['id']}"), "pipeline run 1"))
    second = api("POST", f"/pipelines/{pid}/runs", {})
    runs.append(
        wait(lambda: api("GET", f"/pipelines/{pid}/runs/{second['id']}"), "pipeline run 2 (repeat)")
    )

    checks: list[tuple[str, bool, str]] = []
    run = runs[0]
    blocks = [(b["block_id"], b["status"]) for b in run["blocks"]]
    checks.append(
        (
            "same blocks, in order, all completed",
            blocks == [("fetch", "completed"), ("extract", "completed"), ("notify", "completed")],
            str(blocks),
        )
    )
    checks.append(
        ("job completed", job["status"] == "completed", f"{job['status']} {job.get('error') or ''}")
    )

    job_result = api("GET", f"/jobs/{job['id']}/result", ok=(200, 404))
    run_result = api("GET", f"/pipelines/{pid}/runs/{run['id']}/result", ok=(200, 404))
    job_keys = (
        json_keys(job_result.get("content", ""))
        if job_result and "content" in job_result
        else set()
    )
    run_keys = (
        json_keys(run_result.get("content", ""))
        if run_result and "content" in run_result
        else set()
    )
    wanted = set(schema.get("properties", {}))
    checks.append(
        (
            "artifact retrievable on both lanes",
            bool(job_result and "content" in job_result and run_result and "content" in run_result),
            f"job {job.get('result_path')} · pipeline {run.get('result_path')}",
        )
    )
    checks.append(
        (
            "same schema fields populated",
            bool(job_keys) and job_keys == run_keys and job_keys <= wanted,
            f"job {sorted(job_keys)} · pipeline {sorted(run_keys)}",
        )
    )

    if token:
        time.sleep(10)  # the job lane's webhook loop polls every 15 s
        requests = webhook_site_requests(token)
        parsed = [(r, json.loads(r.get("content") or "{}")) for r in requests]
        job_hooks = [(r, p) for r, p in parsed if p.get("job_id") == job["id"]]
        run_hooks = [(r, p) for r, p in parsed if p.get("pipeline_run_id") == run["id"]]
        if job_hooks and run_hooks:
            (jr, jp), (rr, rp) = job_hooks[0], run_hooks[0]
            only_job, only_run = set(jp) - set(rp), set(rp) - set(jp)
            checks.append(
                (
                    "payload shape equal but for the two recorded exceptions",
                    only_job == JOB_ONLY and only_run == PIPELINE_ONLY,
                    f"job-only {sorted(only_job)} · pipeline-only {sorted(only_run)}",
                )
            )
            sig_ok = []
            for request, secret in ((jr, job["webhook_secret"] or ""), (rr, PIPELINE_SECRET)):
                body = (request.get("content") or "").encode()
                expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                sig_ok.append(header(request, "X-ScrapeFlow-Signature") == expected)
            checks.append(
                ("both payloads signed as the wire contract says", all(sig_ok), str(sig_ok))
            )
        else:
            checks.append(
                (
                    "both webhooks received",
                    False,
                    f"job {len(job_hooks)} · pipeline {len(run_hooks)} of {len(requests)} requests",
                )
            )
    else:
        print(
            f"  check the two payloads at your receiver: job {job['id']}, pipeline run {run['id']}"
        )

    repeat = runs[1]
    llm_calls = sum(
        1 for b in repeat["blocks"] if b["type"] == "llm_extract" and b["status"] == "completed"
    )
    stored = sum(1 for b in repeat["blocks"] if b["output_state"] == "stored")

    print("\nR6 — structure and mechanics")
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    print("\nRepeat-run cost (pipeline run 2 on an unchanged page)")
    print(
        f"  {llm_calls} LLM call(s), {stored} newly stored object(s); the job lane's content-hash dedup costs 0 and 0"
    )
    print(
        "\nKnown exclusions (not judged): content-hash gate, reporting diff, webhook failure fails the run,"
        " no notification when a run fails before its Webhook block"
    )
    print(f"\nverdict: {'PASS' if all(ok for _, ok, _ in checks) else 'FAIL'}")
    print(json.dumps({"job_id": job["id"], "pipeline_id": pid, "runs": [r["id"] for r in runs]}))


if __name__ == "__main__":
    main()
