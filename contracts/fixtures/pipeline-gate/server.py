"""The pipeline gate's fixture (J.10): a shop, a stand-in LLM and a webhook receiver, one per host name.

    shop.pipelinefixture.com   GET  /item?name=&price=&stock=in|out   a product page; price=null → none
                               GET  /broken                           503 (a Scrape that fails)
    llm.pipelinefixture.com    GET  /v1/models                        the LLM worker's warm-up probe
                               POST /v1/chat/completions              {"price", "in_stock", "name"} read off
                                                                      the page; "SLOW" in it → 8 s first
                               GET  /_calls                           every completion asked for, in order
    hooks.pipelinefixture.com  POST /ok → 204 · /down → 503 · /slow → 8 s, then 204
                               GET  /_log                             every POST: path, headers, body

Standard library only; served by the compose service ``pipeline-fixture`` (profile ``gate``).
"""

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOCK = threading.Lock()
CALLS: list[dict] = []
POSTS: list[dict] = []


def page(name: str, price: str, stock: str) -> bytes:
    shown = "unknown" if price == "null" else f"£{price}"
    availability = "In stock" if stock == "in" else "Out of stock"
    return (
        f"<html><head><title>{name}</title></head><body><h1>{name}</h1>"
        f"<p>Price: {shown}</p><p>{availability}</p></body></html>"
    ).encode()


def extraction(prompt: str) -> dict:
    price = re.search(r"Price: £(\d+(?:\.\d+)?)", prompt)
    name = re.search(r"<h1>([^<]+)</h1>|^# (.+)$", prompt, re.M)
    return {
        "price": float(price.group(1)) if price else None,
        "in_stock": "In stock" in prompt,
        "name": next((g for g in name.groups() if g), None) if name else None,
    }


class Handler(BaseHTTPRequestHandler):
    def _host(self) -> str:
        return (self.headers.get("Host") or "").split(":")[0]

    def _send(self, status: int, body: bytes = b"", kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value).encode())

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        host = self._host()
        if host.startswith("shop."):
            if url.path == "/broken":
                return self._send(503, b"down", "text/plain")
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = page(q.get("name", "Blue Kettle"), q.get("price", "80"), q.get("stock", "in"))
            return self._send(200, body, "text/html; charset=utf-8")
        if host.startswith("llm.") and url.path == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": "fixture", "object": "model"}]})
        if host.startswith("llm.") and url.path == "/_calls":
            with LOCK:
                return self._json(200, CALLS)
        if host.startswith("hooks.") and url.path == "/_log":
            with LOCK:
                return self._json(200, POSTS)
        self._send(404)

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        host = self._host()
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if host.startswith("llm.") and url.path == "/v1/chat/completions":
            prompt = json.loads(body)["messages"][-1]["content"]
            if "SLOW" in prompt:
                time.sleep(8)
            answer = extraction(prompt)
            with LOCK:
                CALLS.append(answer)
            return self._json(
                200,
                {
                    "id": f"fixture-{len(CALLS)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(answer)},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        if host.startswith("hooks."):
            with LOCK:
                POSTS.append(
                    {
                        "path": url.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body.decode(),
                    }
                )
            if url.path == "/slow":
                time.sleep(8)
            return self._send(503 if url.path == "/down" else 204)
        self._send(404)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self._host()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
