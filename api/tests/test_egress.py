"""J.8 — BUG-030: one lookup, every answer checked, the POST pinned to a checked address (ADR-015 §3)."""

import hashlib
import hmac
import json
import re
import socket
import ssl
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core import egress
from app.core.db import AsyncSessionLocal
from app.models.job import Job
from app.models.job_runs import JobRun
from app.models.webhook_delivery import WebhookDelivery
from app.settings import settings
from app.workflows.activities.webhook import build_payload
from app.workflows.activities.webhook_deliveries import attempt_webhook_delivery
from app.workflows.webhook_delivery import DeliveryRef
from tests.test_webhook_deliveries import Receiver
from tests.test_workflows_webhook import _attempt, _input

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"


def _answers(*ips):
    return [
        (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in ips
    ]


class Lookups:
    """getaddrinfo that answers each call with the next list in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, host, port, *args, **kwargs):
        self.calls.append((host, port))
        return _answers(*self.answers.pop(0))


@pytest.fixture
def receiver(monkeypatch):
    r = Receiver()
    monkeypatch.setattr(egress, "_post", r)
    return r


# --- the address rule -------------------------------------------------------------------------


def test_resolve_public_returns_each_address_once_in_order(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2, PUBLIC]))
    assert egress.resolve_public("hooks.example.com", 443) == [PUBLIC, PUBLIC_2]


@pytest.mark.parametrize("private", ["10.0.0.5", "100.64.0.1", "127.0.0.1", "64:ff9b::a00:5"])
def test_one_private_answer_refuses_the_whole_name(monkeypatch, private):
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, private]))
    with pytest.raises(ValueError, match=re.escape(f"private address: {private}")):
        egress.resolve_public("hooks.example.com", 443)


def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(egress.socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="could not be resolved"):
        egress.resolve_public("nowhere.example", 443)


# --- the pin ----------------------------------------------------------------------------------


async def test_the_post_goes_to_the_checked_address_presenting_the_name(monkeypatch, receiver):
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC]))
    status = await egress.send_signed_post("https://hooks.example.com/in?x=1", b"{}", b"k")

    assert status == 200
    [(url, body, headers)] = receiver.posts
    assert (url, body) == (f"https://{PUBLIC}/in?x=1", b"{}")
    assert receiver.extensions == [{"sni_hostname": "hooks.example.com"}]
    assert headers == {
        "Content-Type": "application/json",
        "X-ScrapeFlow-Signature": "sha256=" + hmac.new(b"k", b"{}", hashlib.sha256).hexdigest(),
        "Host": "hooks.example.com",
    }


async def test_a_port_an_ipv6_answer_and_userinfo_are_kept_where_they_belong(monkeypatch, receiver):
    lookups = Lookups(["2606:2800:220:1:248:1893:25c8:1946"])
    monkeypatch.setattr(egress.socket, "getaddrinfo", lookups)
    await egress.send_signed_post("http://u:p@hooks.example.com:8443/in", b"{}", b"")

    [(url, _, headers)] = receiver.posts
    assert url == "http://u:p@[2606:2800:220:1:248:1893:25c8:1946]:8443/in"
    assert headers["Host"] == "hooks.example.com:8443"
    assert receiver.extensions == [{}]  # plain http: no TLS name to send
    assert lookups.calls == [("hooks.example.com", 8443)]


async def test_a_name_that_rebinds_to_a_private_address_is_never_reached(monkeypatch, receiver):
    # Public on the first lookup, private on any later one: there is no later one.
    lookups = Lookups([PUBLIC], ["10.0.0.5"])
    monkeypatch.setattr(egress.socket, "getaddrinfo", lookups)
    await egress.send_signed_post("https://rebind.example/in", b"{}", b"")

    assert len(lookups.calls) == 1
    [(url, _, _)] = receiver.posts
    assert url == f"https://{PUBLIC}/in"


async def test_a_private_address_is_refused_before_anything_is_sent(monkeypatch, receiver):
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups(["10.0.0.5"]))
    with pytest.raises(egress.EgressBlocked, match=re.escape("private address: 10.0.0.5")):
        await egress.send_signed_post("https://hooks.example.com/in", b"{}", b"")
    assert receiver.posts == []


# --- several addresses, and TLS ----------------------------------------------------------------


def _refused():
    return httpx.ConnectError("[Errno 111] Connection refused")


def _tls(cause: ssl.SSLError):
    try:
        raise httpx.ConnectError("tls") from cause
    except httpx.ConnectError as exc:
        return exc


@pytest.mark.parametrize("first", [_refused(), httpx.ConnectTimeout("timed out")])
async def test_a_refused_or_timed_out_address_moves_on_to_the_next(monkeypatch, first):
    tried: list[str] = []

    async def post(url, body, headers, extensions):
        tried.append(url)
        if len(tried) == 1:
            raise first
        return 204

    monkeypatch.setattr(egress, "_post", post)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    assert await egress.send_signed_post("https://hooks.example.com/in", b"{}", b"") == 204
    assert tried == [f"https://{PUBLIC}/in", f"https://{PUBLIC_2}/in"]


async def test_the_last_address_refusing_is_the_attempts_error(monkeypatch):
    async def post(url, body, headers, extensions):
        raise _refused()

    monkeypatch.setattr(egress, "_post", post)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    with pytest.raises(httpx.ConnectError):
        await egress.send_signed_post("https://hooks.example.com/in", b"{}", b"")


async def test_a_certificate_for_another_name_is_final_and_tries_no_other_address(monkeypatch):
    tried: list[str] = []

    async def post(url, body, headers, extensions):
        tried.append(url)
        raise _tls(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))

    monkeypatch.setattr(egress, "_post", post)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    with pytest.raises(
        egress.CertificateMismatch, match=re.escape("certificate not valid for hooks.example.com")
    ):
        await egress.send_signed_post("https://hooks.example.com/in", b"{}", b"")
    assert len(tried) == 1


async def test_another_tls_failure_tries_no_other_address(monkeypatch):
    tried: list[str] = []

    async def post(url, body, headers, extensions):
        tried.append(url)
        raise _tls(ssl.SSLError("handshake failure"))

    monkeypatch.setattr(egress, "_post", post)
    monkeypatch.setattr(egress.socket, "getaddrinfo", Lookups([PUBLIC, PUBLIC_2]))
    with pytest.raises(httpx.ConnectError):
        await egress.send_signed_post("https://hooks.example.com/in", b"{}", b"")
    assert len(tried) == 1


# --- both senders send the same thing ---------------------------------------------------------


async def test_both_webhook_senders_send_the_same_bytes_and_headers(db_user, monkeypatch, receiver):
    monkeypatch.setattr(egress, "resolve_public", lambda host, port: [PUBLIC])
    secret = "shared-secret"

    # The pipeline Webhook block (attempt 1 writes no row).
    block_input = _input(uuid.uuid4(), "https://hooks.example.com/in", secret=secret)
    await _attempt(block_input)

    # The job lane's delivery of a row carrying the same payload.
    async with AsyncSessionLocal() as db:
        job = Job(
            user_id=db_user.id,
            url="https://example.com",
            webhook_url="https://hooks.example.com/in",
            webhook_secret=Fernet(settings.llm_key_encryption_key)
            .encrypt(secret.encode())
            .decode(),
        )
        db.add(job)
        await db.flush()
        run = JobRun(job_id=job.id, status="completed", lane="v2")
        db.add(run)
        await db.flush()
        row = WebhookDelivery(
            job_id=job.id,
            run_id=run.id,
            event="pipeline.result",
            webhook_url="https://hooks.example.com/in",
            payload=build_payload(block_input),
        )
        db.add(row)
        await db.commit()
    await attempt_webhook_delivery(DeliveryRef(delivery_id=str(row.id)))

    (url_1, body_1, headers_1), (url_2, body_2, headers_2) = receiver.posts
    assert url_1 == url_2
    assert receiver.extensions[0] == receiver.extensions[1]
    # JSONB reorders the stored payload's keys, so the bytes are compared as what they say...
    assert json.loads(body_1) == json.loads(body_2)
    # ...and each signature over its own bytes, with the one secret.
    for body, headers in ((body_1, headers_1), (body_2, headers_2)):
        mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert headers == {
            "Content-Type": "application/json",
            "X-ScrapeFlow-Signature": f"sha256={mac}",
            "Host": "hooks.example.com",
        }


async def _mismatch(url, body, secret):
    raise egress.CertificateMismatch("certificate not valid for hooks.example.com")


async def test_the_webhook_block_fails_for_good_on_a_certificate_mismatch(monkeypatch):
    from temporalio.exceptions import ApplicationError

    monkeypatch.setattr(egress, "send_signed_post", _mismatch)
    with pytest.raises(ApplicationError) as exc:
        await _attempt(_input(uuid.uuid4(), "https://hooks.example.com/in"))
    assert (exc.value.type, exc.value.non_retryable, exc.value.message) == (
        "WebhookCertificate",
        True,
        "certificate not valid for hooks.example.com",
    )


async def test_a_delivery_row_is_exhausted_on_a_certificate_mismatch(db_user, monkeypatch):
    from tests.test_webhook_deliveries import _delivery, _get

    async with AsyncSessionLocal() as db:
        job = Job(user_id=db_user.id, url="https://example.com", webhook_url="https://h.example")
        db.add(job)
        await db.commit()
    monkeypatch.setattr(egress, "send_signed_post", _mismatch)
    d = await _delivery(job)

    outcome = await attempt_webhook_delivery(DeliveryRef(delivery_id=str(d.id)))
    row = await _get(d.id)
    assert outcome.status == "exhausted"
    assert (row.status, row.attempts, row.last_error) == (
        "exhausted",
        1,
        "certificate not valid for hooks.example.com",
    )
