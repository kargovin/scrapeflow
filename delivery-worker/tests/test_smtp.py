"""M.14 — email: the pinned SMTP session, send_email, test_smtp and the classifier (ADR-015 §2d–§7)."""

import asyncio
import email
import email.policy
import json
import socket
import ssl

import aiohttp
import aiosmtplib
import pytest
from cryptography.fernet import Fernet
from miniopy_async.error import S3Error
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests.smtp_server import PASSWORD, USER, smtp_server
from tests.test_bucket import FakeSource
from worker import checks, egress, pinned_tcp, smtp
from worker.config import settings
from worker.source import source_error
from worker.contracts import DestinationTestInput, SendEmailInput

HOST = "mail.example.com"
MESSAGE_ID = "<0b1f.notify.failed@scrapeflow.example.org>"


def _secret(user: str = USER, password: str = PASSWORD) -> str:
    token = Fernet(settings.destination_encryption_key).encrypt(
        json.dumps({"user": user, "password": password}).encode()
    )
    return token.decode()


def _address(server, *, mode: str = "starttls", ca: str | None = "server", host: str = HOST) -> dict:
    return {
        "host": host,
        "port": server.port if server else 587,
        "tls_mode": mode,
        "from": "alerts@example.org",
        "to": ["ops@example.org", "oncall@example.org"],
        "ca_certificate": (server.ca_pem if server else None) if ca == "server" else ca,
    }


def _send_input(server, body: str = "the run failed", run_id: str | None = "0b1f", **kw):
    return SendEmailInput(
        address=_address(server, **kw),
        encrypted_secret=_secret(),
        message_id=MESSAGE_ID,
        subject="[ScrapeFlow] prices: pipeline.failed",
        body=body,
        run_id=run_id,
    )


async def _send(inp: SendEmailInput, source=None):
    return await ActivityEnvironment().run(smtp.EmailActivities(source).send_email, inp)


async def _send_error(inp: SendEmailInput, source=None) -> ApplicationError:
    with pytest.raises(ApplicationError) as exc:
        await _send(inp, source)
    return exc.value


@pytest.fixture
def lookups(monkeypatch):
    """The checked addresses: 127.0.0.1 stands for a public answer; records each lookup."""
    seen: list[tuple[str, int]] = []
    answer = ["127.0.0.1"]

    async def vetted(host, port):
        seen.append((host, port))
        return list(answer)

    monkeypatch.setattr(egress, "vetted_addresses", vetted)
    return seen, answer


def _parsed(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


# --- sending -----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["starttls", "implicit"])
async def test_the_message_arrives_over_tls_with_its_message_id(lookups, mode):
    async with smtp_server(HOST, mode=mode) as server:
        out = await _send(_send_input(server, mode=mode))

    assert out.refused == []
    assert lookups[0] == [(HOST, server.port)]
    assert server.sni == [HOST]
    [raw] = server.messages
    msg = _parsed(raw)
    assert msg["Message-ID"] == MESSAGE_ID
    assert msg["From"] == "alerts@example.org"
    assert msg["To"] == "ops@example.org, oncall@example.org"
    assert msg["Subject"] == "[ScrapeFlow] prices: pipeline.failed"
    assert msg.get_content().strip() == "the run failed"
    rcpts = [line for line, _ in server.commands if line.upper().startswith("RCPT")]
    assert rcpts == ["RCPT TO:<ops@example.org>", "RCPT TO:<oncall@example.org>"]


async def test_auth_and_everything_after_it_go_over_tls_only(lookups):
    async with smtp_server(HOST) as server:
        await _send(_send_input(server))

    plain = [line.split()[0].upper() for line, tls in server.commands if not tls]
    assert plain == ["EHLO", "STARTTLS"]
    assert any(line.upper().startswith("AUTH") and tls for line, tls in server.commands)


async def test_a_resend_repeats_the_message_id(lookups):
    async with smtp_server(HOST) as server:
        await _send(_send_input(server))
        await _send(_send_input(server))

    assert [_parsed(m)["Message-ID"] for m in server.messages] == [MESSAGE_ID, MESSAGE_ID]
    assert lookups[0] == [(HOST, server.port)] * 2


async def test_a_body_over_the_inline_cap_is_replaced_by_the_note(lookups):
    body = "x" * (smtp.INLINE_MAX_BYTES + 1)
    async with smtp_server(HOST) as server:
        await _send(_send_input(server, body=body, run_id="0b1f"))

    content = _parsed(server.messages[0]).get_content().strip()
    assert content == "result too large to include (256 KiB); fetch it with run 0b1f"


def test_the_inline_cap_is_inclusive_and_a_runless_note_names_no_run():
    at_cap = "y" * smtp.INLINE_MAX_BYTES
    assert smtp.body_text(at_cap, "r") == at_cap
    assert smtp.body_text("é" * smtp.INLINE_MAX_BYTES, None) == "result too large to include (512 KiB)"


# --- an Email block's content (P.1; ADR-015 §7) --------------------------------------------------

SOURCE = "scrapeflow-results/history/0b1f/llm.json"


def _text(raw: bytes) -> str:
    return _parsed(raw).get_content().replace("\r\n", "\n")


def _block_input(server) -> SendEmailInput:
    inp = _send_input(server, body="pipeline.result — run 0b1f\n\n")
    return inp.model_copy(update={"source_path": SOURCE})


async def test_an_email_blocks_content_follows_the_body_read_from_scrapeflows_copy(lookups):
    source = FakeSource(b'{"price": 8}')
    async with smtp_server(HOST) as server:
        await _send(_block_input(server), source)

    assert source.paths == [SOURCE]
    content = _text(server.messages[0])
    assert content == 'pipeline.result — run 0b1f\n\n{"price": 8}\n'


async def test_content_at_the_cap_is_inline_and_over_it_is_the_note_unread(lookups):
    at_cap = b"a" * smtp.INLINE_MAX_BYTES
    over = FakeSource(at_cap + b"b", fail_mid=True)
    async with smtp_server(HOST) as server:
        await _send(_block_input(server), FakeSource(at_cap))
        await _send(_block_input(server), over)

    inline, noted = (_text(m) for m in server.messages)
    assert inline.endswith(at_cap.decode() + "\n")
    # Not read: a read would have failed mid-stream.
    assert noted == (
        "pipeline.result — run 0b1f\n\nresult too large to include (256 KiB); fetch it with run 0b1f\n"
    )


async def test_content_that_is_not_utf_8_is_sent_with_replacement_characters(lookups):
    async with smtp_server(HOST) as server:
        await _send(_block_input(server), FakeSource(b"caf\xe9"))

    assert _text(server.messages[0]).endswith("caf\ufffd\n")


async def test_a_message_without_a_source_reads_nothing(lookups):
    source = FakeSource()
    async with smtp_server(HOST) as server:
        await _send(_send_input(server), source)

    assert source.paths == []


@pytest.mark.parametrize(
    "source, type_, final",
    [
        (lambda: FakeSource(error=source_error(aiohttp.ClientConnectionError())), "SourceUnavailable", False),
        (lambda: FakeSource(error=source_error(S3Error("NoSuchKey", "m", "/k", None, None, None))), "SourceFailed", True),
        (lambda: FakeSource(b"x" * 200_000, fail_mid=True), "SourceUnavailable", False),
    ],
)
async def test_a_source_that_cannot_be_read_is_classified_and_nothing_is_sent(
    lookups, source, type_, final
):
    async with smtp_server(HOST) as server:
        err = await _send_error(_block_input(server), source())

    assert (err.type, err.non_retryable) == (type_, final)
    assert server.messages == []
    assert lookups[0] == []


async def test_recipients_refused_while_another_is_accepted_are_reported(lookups):
    async with smtp_server(HOST, refuse_rcpt={"oncall@example.org": 550}) as server:
        out = await _send(_send_input(server))
    assert out.refused == ["oncall@example.org"]
    assert len(server.messages) == 1


async def test_one_recipient_deferred_and_one_refused_is_final(lookups):
    refuse = {"ops@example.org": 450, "oncall@example.org": 550}
    async with smtp_server(HOST, refuse_rcpt=refuse) as server:
        err = await _send_error(_send_input(server))
    assert (err.type, err.non_retryable, err.message) == (
        "DestinationRefused", True, f"{HOST} refused every recipient (450, 550)"
    )


# --- refusals: what is final and what is transient ---------------------------------------------


async def test_a_server_without_starttls_gets_no_login_and_no_mail(lookups):
    async with smtp_server(HOST, offer_starttls=False) as server:
        err = await _send_error(_send_input(server))

    assert (err.type, err.non_retryable) == ("TlsFailed", True)
    assert err.message == f"{HOST} does not offer STARTTLS; nothing is sent in plain text"
    assert "AUTH" not in server.verbs() and "MAIL" not in server.verbs()


async def test_a_certificate_for_another_name_is_final(lookups):
    async with smtp_server("other.example.net") as server:
        err = await _send_error(_send_input(server))
    assert (err.type, err.non_retryable) == ("CertificateMismatch", True)
    assert err.message == f"certificate not valid for {HOST}"
    assert "AUTH" not in server.verbs()


@pytest.mark.parametrize("mode", ["starttls", "implicit"])
async def test_a_certificate_the_system_does_not_trust_is_final_without_its_ca(lookups, mode):
    async with smtp_server(HOST, mode=mode) as server:
        err = await _send_error(_send_input(server, mode=mode, ca=None))
    assert (err.type, err.non_retryable) == ("CertificateMismatch", True)


async def test_an_unreadable_ca_certificate_is_final_before_any_lookup(lookups):
    inp = _send_input(None, ca="-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n")
    err = await _send_error(inp)
    assert (err.type, err.non_retryable) == ("DestinationAddress", True)
    assert err.message == "smtp address: CA certificate could not be read"
    assert lookups[0] == []


@pytest.mark.parametrize(
    ("replies", "type_", "final", "message"),
    [
        ({"BANNER": (421, "busy")}, "DestinationUnavailable", False, f"{HOST} deferred the connection (421)"),
        ({"BANNER": (554, "go away")}, "DestinationRefused", True, f"{HOST} refused the connection (554)"),
        ({"MAIL": (451, "try later")}, "DestinationUnavailable", False, f"{HOST} deferred the sender (451)"),
        ({"MAIL": (550, "no")}, "DestinationRefused", True, f"{HOST} refused the sender (550)"),
        ({"RCPT": (450, "greylisted")}, "DestinationUnavailable", False, f"{HOST} deferred every recipient (450)"),
        ({"RCPT": (550, "no such user")}, "DestinationRefused", True, f"{HOST} refused every recipient (550)"),
        ({"DATA": (452, "full")}, "DestinationUnavailable", False, f"{HOST} deferred the message (452)"),
        ({"DATA": (554, "spam")}, "DestinationRefused", True, f"{HOST} refused the message (554)"),
        ({"AUTH": (454, "temporary")}, "DestinationUnavailable", False, f"{HOST} deferred the login (454)"),
    ],
)
async def test_reply_codes(lookups, replies, type_, final, message):
    async with smtp_server(HOST, replies=replies) as server:
        err = await _send_error(_send_input(server))
    assert (err.type, err.non_retryable, err.message) == (type_, final, message)


async def test_a_refused_login_is_final_and_never_carries_the_server_s_text_or_the_secret(lookups):
    async with smtp_server(HOST) as server:
        inp = _send_input(server)
        inp.encrypted_secret = _secret(password="wrong-s3cret")
        err = await _send_error(inp)
    assert (err.type, err.non_retryable) == ("DestinationRefused", True)
    assert err.message == f"{HOST} refused the login (535)"
    assert "wrong-s3cret" not in repr(err) and "authentication failed" not in repr(err)
    assert "MAIL" not in server.verbs()


async def test_a_server_offering_no_login_over_tls_is_final(lookups):
    async with smtp_server(HOST, offer_auth=False) as server:
        err = await _send_error(_send_input(server))
    assert (err.type, err.non_retryable) == ("DestinationRefused", True)
    assert "MAIL" not in server.verbs()


async def test_a_dropped_connection_is_transient(lookups):
    async with smtp_server(HOST, replies={"MAIL": (421, "closing")}) as server:
        err = await _send_error(_send_input(server))
    assert (err.type, err.non_retryable) == ("DestinationUnavailable", False)


async def test_a_server_that_stops_answering_is_transient(lookups, monkeypatch):
    monkeypatch.setattr(smtp, "EMAIL_SEND_ATTEMPT_SECONDS", smtp.ATTEMPT_MARGIN_SECONDS + 0.5)
    async with smtp_server(HOST, hang_after="DATA") as server:
        err = await _send_error(_send_input(server))
    assert (err.type, err.non_retryable) == ("DestinationUnreachable", False)


async def test_nothing_listening_is_transient(lookups):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    inp = _send_input(None)
    inp.address["port"] = port
    err = await _send_error(inp)
    assert (err.type, err.non_retryable) == ("DestinationUnreachable", False)
    assert err.message == f"could not reach {HOST}:{port} (ConnectionRefusedError)"


# --- the pin -----------------------------------------------------------------------------------


async def test_a_private_mail_host_is_refused_at_every_attempt(monkeypatch):
    def getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    opened: list[str] = []

    async def _open(ip, port, timeout):
        opened.append(ip)
        raise AssertionError("a refused address is never dialled")

    monkeypatch.setattr(egress.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(pinned_tcp, "_open", _open)
    for _ in range(3):
        err = await _send_error(_send_input(None, ca=None))
        assert (err.type, err.non_retryable) == ("EgressBlocked", True)
        assert err.message == "URL resolves to a private address: 10.0.0.7"
    assert opened == []


async def test_a_refused_address_moves_on_to_the_next(lookups):
    _, answer = lookups
    answer[:] = ["127.0.0.2", "127.0.0.1"]
    async with smtp_server(HOST) as server:
        await _send(_send_input(server))
    assert len(server.messages) == 1


async def test_a_tls_failure_tries_no_other_address(lookups):
    _, answer = lookups
    answer[:] = ["127.0.0.1", "127.0.0.1"]
    async with smtp_server("other.example.net") as server:
        await _send_error(_send_input(server))
    assert server.connections == 1


async def test_the_session_is_pinned_to_the_checked_address_with_the_name(lookups, monkeypatch):
    dialled: list[tuple[str, int]] = []
    real_open = pinned_tcp._open

    async def _open(ip, port, timeout):
        dialled.append((ip, port))
        return await real_open(ip, port, timeout)

    monkeypatch.setattr(pinned_tcp, "_open", _open)
    async with smtp_server(HOST, mode="implicit") as server:
        await _send(_send_input(server, mode="implicit"))
    assert dialled == [("127.0.0.1", server.port)]
    assert server.sni == [HOST]


def _open_transports() -> list:
    return [t for t in asyncio.get_running_loop()._transports.values() if not t.is_closing()]


@pytest.mark.parametrize("mode", ["starttls", "implicit"])
async def test_a_session_leaves_no_transport_on_its_closed_socket(lookups, mode):
    """A transport still open on a socket pinned_tcp closed holds that fd number: the next socket
    given the same number fails to connect (RuntimeError) in a long-running worker."""
    # The server closes after the client, as one across a network does.
    async with smtp_server(HOST, mode=mode, close_delay=0.2) as server:
        for _ in range(3):
            await _send(_send_input(server, mode=mode))
            assert await _test(_test_input(server, mode=mode)) == [
                ("address", "passed", None), ("tls", "passed", None), ("login", "passed", None)
            ]
    assert len(server.messages) == 3
    assert _open_transports() == []


# --- the destination test (ADR-015 §2d): EHLO, AUTH, QUIT; no mail ----------------------------


def _test_input(server, **kw) -> DestinationTestInput:
    return DestinationTestInput(kind="smtp", address=_address(server, **kw), encrypted_secret=_secret())


async def _test(inp: DestinationTestInput):
    out = await ActivityEnvironment().run(checks.test_smtp, inp)
    return [(c.check, c.outcome, c.message) for c in out.checks]


@pytest.mark.parametrize("mode", ["starttls", "implicit"])
async def test_the_test_logs_in_and_quits_without_sending(lookups, mode):
    async with smtp_server(HOST, mode=mode) as server:
        result = await _test(_test_input(server, mode=mode))
    assert result == [("address", "passed", None), ("tls", "passed", None), ("login", "passed", None)]
    verbs = server.verbs()
    assert "AUTH" in verbs and verbs[-1] == "QUIT"
    assert not {"MAIL", "RCPT", "DATA"} & set(verbs)
    assert server.messages == []


async def test_a_refused_login_fails_the_login_check(lookups):
    async with smtp_server(HOST) as server:
        inp = _test_input(server)
        inp.encrypted_secret = _secret(password="typo")
        result = await _test(inp)
    assert result == [
        ("address", "passed", None),
        ("tls", "passed", None),
        ("login", "failed", f"{HOST} refused the login (535)"),
    ]


async def test_no_starttls_fails_the_tls_check(lookups):
    async with smtp_server(HOST, offer_starttls=False) as server:
        result = await _test(_test_input(server))
    assert result == [
        ("address", "passed", None),
        ("tls", "failed", f"{HOST} does not offer STARTTLS; nothing is sent in plain text"),
    ]
    assert "AUTH" not in server.verbs()


async def test_a_certificate_for_another_name_fails_the_tls_check(lookups):
    async with smtp_server("other.example.net") as server:
        result = await _test(_test_input(server))
    assert result == [
        ("address", "passed", None),
        ("tls", "failed", f"certificate not valid for {HOST}"),
    ]


async def test_a_private_mail_host_fails_the_address_check(monkeypatch):
    async def vetted(host, port):
        raise egress.EgressBlocked("URL resolves to a private address: 192.168.1.5")

    monkeypatch.setattr(egress, "vetted_addresses", vetted)
    result = await _test(_test_input(None, ca=None))
    assert result == [("address", "failed", "URL resolves to a private address: 192.168.1.5")]


async def test_an_unreadable_ca_fails_the_address_check(lookups):
    result = await _test(_test_input(None, ca="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----"))
    assert result == [("address", "failed", "CA certificate could not be read")]
    assert lookups[0] == []


async def test_the_test_refuses_another_kind():
    inp = DestinationTestInput(kind="webhook", address={}, encrypted_secret=_secret())
    with pytest.raises(ApplicationError) as exc:
        await ActivityEnvironment().run(checks.test_smtp, inp)
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationKind", True)


# --- the classifier ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "type_", "final"),
    [
        (egress.EgressBlocked("x"), "EgressBlocked", True),
        (egress.CertificateMismatch("x"), "CertificateMismatch", True),
        (ssl.SSLError("handshake"), "TlsFailed", True),
        (smtp.NoStartTls(), "TlsFailed", True),
        (smtp.NoAuth(), "DestinationRefused", True),
        (aiosmtplib.SMTPAuthenticationError(535, "no"), "DestinationRefused", True),
        (aiosmtplib.SMTPAuthenticationError(454, "later"), "DestinationUnavailable", False),
        (aiosmtplib.SMTPResponseException(503, "bad sequence"), "DestinationRefused", True),
        (aiosmtplib.SMTPServerDisconnected("gone"), "DestinationUnreachable", False),
        (aiosmtplib.SMTPConnectError("refused"), "DestinationUnreachable", False),
        (aiosmtplib.SMTPReadTimeoutError("slow"), "DestinationUnreachable", False),
        (ConnectionRefusedError(111, "refused"), "DestinationUnreachable", False),
        (TimeoutError(), "DestinationUnreachable", False),
        (aiosmtplib.SMTPNotSupported("x"), "DeliveryFailed", True),
        (ValueError("x"), "DeliveryFailed", True),
    ],
)
def test_the_classifier(exc, type_, final):
    got_type, _, got_final = smtp.describe(exc, HOST, 587, "send")
    assert (got_type, got_final) == (type_, final)
