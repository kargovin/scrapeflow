"""The address table of test_egress.py, through the live egress proxy (BUG-010 part 2).

The scrape workers do not carry the address check: they send every fetch of a user-chosen address
through Smokescreen (docker/egress/smokescreen.yaml). This runs the same table as a CONNECT per
address — what Chrome, Go and httpx send for https — and expects the proxy to refuse exactly what
``is_public_address`` refuses, except where Smokescreen is stricter (STRICTER below).

Needs the compose ``egress`` service; skipped without EGRESS_PROXY_URL:

    docker run --rm --network docker_default -e EGRESS_PROXY_URL=http://egress:4750 \
        -v "$PWD:/repo" -w /repo docker-api /app/.venv/bin/python -m pytest contracts/test_egress_proxy.py -q
"""

import asyncio
import ipaddress
import os
from urllib.parse import urlsplit

import pytest
from test_egress import ADMITTED, REFUSED

PROXY = os.environ.get("EGRESS_PROXY_URL")

pytestmark = pytest.mark.skipif(not PROXY, reason="EGRESS_PROXY_URL not set (needs the compose egress service)")

# Smokescreen refuses every IPv4-embedding IPv6 address; is_public_address judges NAT64 as its IPv4.
STRICTER = {"64:ff9b::808:808"}


async def _connect(address: str) -> tuple[int, str]:
    """(status, X-Smokescreen-Error or "") of one CONNECT to address:443."""
    proxy = urlsplit(PROXY)
    reader, writer = await asyncio.open_connection(proxy.hostname, proxy.port)
    try:
        ip = ipaddress.ip_address(address)
        target = f"[{ip}]:443" if ip.version == 6 else f"{ip}:443"
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        await writer.drain()
        head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 20)).decode("latin-1")
    finally:
        writer.close()
    lines = head.split("\r\n")
    error = next((line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("x-smokescreen-error:")), "")
    return int(lines[0].split()[1]), error


def _refused(status: int, error: str) -> bool:
    # A dial failure (an admitted address with nothing listening) is also a non-200; only a policy
    # refusal names a rule or an invalid destination.
    return status == 407 and ("denied by rule" in error or "invalid domain" in error)


@pytest.fixture(scope="module")
def answers():
    async def all_of_them():
        addresses = REFUSED + ADMITTED
        results = await asyncio.gather(*(_connect(a) for a in addresses))
        return dict(zip(addresses, results, strict=True))

    return asyncio.run(all_of_them())


@pytest.mark.parametrize("address", REFUSED)
def test_the_proxy_refuses_what_the_rule_refuses(answers, address):
    status, error = answers[address]
    assert _refused(status, error), (status, error)


@pytest.mark.parametrize("address", [a for a in ADMITTED if a not in STRICTER])
def test_the_proxy_admits_what_the_rule_admits(answers, address):
    status, error = answers[address]
    assert not _refused(status, error), (status, error)


@pytest.mark.parametrize("address", sorted(STRICTER))
def test_the_proxy_is_stricter_on_ipv4_embedded_in_ipv6(answers, address):
    status, error = answers[address]
    assert _refused(status, error), (status, error)
