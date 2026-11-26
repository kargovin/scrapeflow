"""worker/egress.py: the job proxy handed to Chrome as a checked address (BUG-010 part 2).
The address rule itself is pinned by contracts/test_egress.py, against every copy."""

import socket

import pytest

from worker import egress
from worker.egress import AddressRefused, pinned_proxy_server


@pytest.mark.parametrize(
    ("proxy_url", "answers", "server"),
    [
        (
            "http://u:p@proxy.example.com:8080",
            ["93.184.216.34"],
            "http://93.184.216.34:8080",
        ),
        (
            "socks5://proxy.example.com:1080",
            ["93.184.216.34"],
            "socks5://93.184.216.34:1080",
        ),
        ("http://proxy.example.com", ["93.184.216.34"], "http://93.184.216.34"),
        (
            "http://proxy.example.com:3128",
            ["2606:4700::1111"],
            "http://[2606:4700::1111]:3128",
        ),
        # TLS to the proxy checks the certificate against its name: checked, not pinned.
        (
            "https://proxy.example.com:443",
            ["93.184.216.34"],
            "https://proxy.example.com:443",
        ),
    ],
)
async def test_the_server_chrome_gets(resolve, proxy_url, answers, server):
    resolve["proxy.example.com"] = answers
    assert await pinned_proxy_server(proxy_url) == server


@pytest.mark.parametrize(
    "answers",
    [["10.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["100.64.0.1"], ["64:ff9b::a00:1"]],
)
async def test_any_non_public_answer_refuses(resolve, answers):
    resolve["proxy.example.com"] = answers
    with pytest.raises(AddressRefused) as excinfo:
        await pinned_proxy_server("https://proxy.example.com:8080")
    assert (
        str(excinfo.value)
        == "address refused: proxy.example.com is not a public address"
    )


async def test_an_unresolvable_proxy_is_an_ordinary_failure(monkeypatch):
    async def lookup(host, port):
        raise socket.gaierror("name not known")

    monkeypatch.setattr(egress, "_lookup", lookup)
    with pytest.raises(ValueError, match="could not be resolved"):
        await pinned_proxy_server("http://nowhere.invalid:8080")


@pytest.mark.parametrize(
    "value", ["", "egress:4750", "https://egress:4750", "http://egress", "http://:4750"]
)
def test_egress_proxy_url_must_be_http_host_port(monkeypatch, value):
    from pydantic import ValidationError

    from worker.config import Settings

    monkeypatch.setenv("EGRESS_PROXY_URL", value)
    with pytest.raises(ValidationError, match="EGRESS_PROXY_URL"):
        Settings()
