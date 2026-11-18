import ipaddress
from unittest.mock import patch

import pytest

from app.core.security import is_public_address, validate_no_ssrf_core

REFUSED = [
    "100.64.0.1",  # BUG-024: shared address space (carrier-grade NAT, Tailscale)
    "100.127.255.254",
    "100.100.100.200",
    "224.0.0.1",  # BUG-024: multicast
    "239.1.1.1",
    "ff0e::1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.20",
    "127.0.0.1",
    "169.254.169.254",
    "0.0.0.0",
    "255.255.255.255",
    "198.18.0.1",
    "203.0.113.5",
    "::1",
    "fd00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:100.64.0.1",
    "::ffff:224.0.0.1",
    "64:ff9b::a00:1",  # NAT64 of 10.0.0.1: is_global alone calls it public
    "64:ff9b::6440:1",  # NAT64 of 100.64.0.1
    "64:ff9b::e000:1",  # NAT64 of 224.0.0.1
    "2002:a00:1::",
]

ADMITTED = [
    "93.184.216.34",
    "8.8.8.8",
    "11.222.33.10",  # E.6's compose fixture subnet
    "2606:4700::1111",
    "::ffff:8.8.8.8",
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
]


@pytest.mark.parametrize("address", REFUSED)
def test_non_public_addresses_are_not_public(address):
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ADMITTED)
def test_public_addresses_are_public(address):
    assert is_public_address(ipaddress.ip_address(address))


def _resolving_to(*addresses):
    return patch(
        "app.core.security.socket.getaddrinfo",
        return_value=[(None, None, None, None, (a, 0)) for a in addresses],
    )


@pytest.mark.parametrize("address", ["100.64.0.1", "224.0.0.1", "64:ff9b::a00:1"])
def test_the_check_refuses_a_url_resolving_to_a_bug_024_address(address):
    with _resolving_to(address), pytest.raises(ValueError, match="private address"):
        validate_no_ssrf_core("https://example.com/")


def test_one_non_public_answer_among_public_ones_refuses():
    with _resolving_to("93.184.216.34", "100.64.0.1"), pytest.raises(ValueError):
        validate_no_ssrf_core("https://example.com/")


def test_a_public_answer_passes():
    with _resolving_to("93.184.216.34", "2606:4700::1111"):
        validate_no_ssrf_core("https://example.com/")
