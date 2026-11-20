"""Email (ADR-015 §2d, §3–§7): one SMTP session per attempt, pinned to a checked address.

TLS before anything else: STARTTLS (refused if the server does not offer it) or implicit TLS, with
the configured host as the server name; AUTH only on the encrypted connection. The client never
retries: the workflow is the one retry layer.
"""

import asyncio
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate

import aiosmtplib
import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import egress, pinned_tcp
from .contracts import (
    EMAIL_SEND_ATTEMPT_SECONDS,
    SEND_EMAIL_ACTIVITY,
    LoginSecret,
    SendEmailInput,
    SendEmailOutput,
    SmtpAddress,
)
from .destinations import final_error, parse

log = structlog.get_logger()

# Room between this worker giving up and the workflow's start_to_close firing.
ATTEMPT_MARGIN_SECONDS = 5
# Inline cap on the body (ADR-015 §7, F3).
INLINE_MAX_BYTES = 256 * 1024


class NoStartTls(Exception):
    """The server did not offer STARTTLS: nothing is sent in plain text."""


class NoAuth(Exception):
    """The server offered no AUTH over TLS."""


def tls_context(ca_certificate: str | None) -> ssl.SSLContext:
    """The system's trust, or only the destination's CA when it brings one. Raises ValueError."""
    if ca_certificate is None:
        return egress.tls_context()
    try:
        return ssl.create_default_context(cadata=ca_certificate)
    except (ssl.SSLError, ValueError):
        raise ValueError("CA certificate could not be read") from None


def body_text(body: str, run_id: str | None) -> str:
    size = len(body.encode())
    if size <= INLINE_MAX_BYTES:
        return body
    note = f"result too large to include ({size // 1024} KiB)"
    return f"{note}; fetch it with run {run_id}" if run_id else note


def message(address: SmtpAddress, inp: SendEmailInput) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = address.sender
    msg["To"] = ", ".join(address.to)
    msg["Subject"] = inp.subject
    msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = inp.message_id
    msg.set_content(body_text(inp.body, inp.run_id))
    return msg


class Session:
    """One connection: TLS on connect, then the login; QUIT on close."""

    def __init__(
        self, sock: socket.socket, address: SmtpAddress, secret: LoginSecret, timeout: float
    ) -> None:
        self._implicit = address.tls_mode == "implicit"
        self._secret = secret
        self.client = aiosmtplib.SMTP(
            hostname=address.host,
            sock=sock,
            use_tls=self._implicit,
            start_tls=False,
            tls_context=tls_context(address.ca_certificate),
            timeout=timeout,
        )

    async def connect(self) -> None:
        try:
            await self.client.connect()
        except aiosmtplib.SMTPConnectError as exc:
            # Implicit TLS: the handshake error arrives wrapped.
            if isinstance(exc.__cause__, ssl.SSLError):
                raise exc.__cause__ from None
            raise
        if not self._implicit:
            await self.client.ehlo()
            if not self.client.supports_extension("starttls"):
                raise NoStartTls()
            await self.client.starttls()

    async def login(self) -> None:
        if self.client.get_transport_info("sslcontext") is None:
            raise NoStartTls()
        await self.client.ehlo()
        if not self.client.supports_extension("auth"):
            raise NoAuth()
        await self.client.login(self._secret.user, self._secret.password)

    async def close(self) -> None:
        transport = self.client.transport
        try:
            if self.client.is_connected:
                await self.client.quit()
        except Exception:
            pass
        finally:
            self.client.close()
            # A TLS close waits for the server's close_notify, but pinned_tcp closes the socket
            # next: a transport left open would hold its fd number for the next connection.
            if transport is not None:
                transport.abort()


async def _send(sock: socket.socket, address: SmtpAddress, secret: LoginSecret, msg: EmailMessage,
                timeout: float) -> list[str]:
    session = Session(sock, address, secret, timeout)
    try:
        await session.connect()
        await session.login()
        errors, _ = await session.client.send_message(
            msg, sender=address.sender, recipients=list(address.to)
        )
        return sorted(errors)
    finally:
        await session.close()


# --- the classifier (ADR-015 §6): SMTP 4xx and connection errors transient, unknown final ----


def describe(exc: BaseException, host: str, port: int, action: str) -> tuple[str, str, bool]:
    """(type, message, final). Built from the class and reply codes, never the server's text."""
    if isinstance(exc, egress.EgressBlocked):
        return "EgressBlocked", str(exc), True
    if isinstance(exc, egress.CertificateMismatch):
        return "CertificateMismatch", str(exc), True
    if isinstance(exc, ssl.SSLError):
        return "TlsFailed", f"TLS handshake with {host} failed", True
    if isinstance(exc, NoStartTls):
        return "TlsFailed", f"{host} does not offer STARTTLS; nothing is sent in plain text", True
    if isinstance(exc, NoAuth):
        return "DestinationRefused", f"{host} offers no login over TLS", True
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        codes = sorted({r.code for r in exc.recipients})
        shown = ", ".join(str(c) for c in codes)
        if all(400 <= c < 500 for c in codes):
            return "DestinationUnavailable", f"{host} deferred every recipient ({shown})", False
        return "DestinationRefused", f"{host} refused every recipient ({shown})", True
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        what = {
            aiosmtplib.SMTPAuthenticationError: "the login",
            aiosmtplib.SMTPSenderRefused: "the sender",
            aiosmtplib.SMTPDataError: "the message",
            aiosmtplib.SMTPConnectResponseError: "the connection",
            aiosmtplib.SMTPHeloError: "the greeting",
        }.get(type(exc), f"the {action}")
        if 400 <= exc.code < 500:
            return "DestinationUnavailable", f"{host} deferred {what} ({exc.code})", False
        return "DestinationRefused", f"{host} refused {what} ({exc.code})", True
    if isinstance(exc, TimeoutError):
        return "DestinationUnreachable", f"{host}:{port} did not answer in time", False
    if isinstance(exc, OSError):
        # aiosmtplib's disconnect and connect errors are ConnectionErrors.
        return "DestinationUnreachable", f"could not reach {host}:{port} ({type(exc).__name__})", False
    return "DeliveryFailed", f"the {action} failed ({type(exc).__name__})", True


def classified(exc: BaseException, host: str, port: int, action: str) -> ApplicationError:
    type_, message_, final = describe(exc, host, port, action)
    return ApplicationError(message_, type=type_, non_retryable=final)


def _parsed(address: dict, encrypted_secret: str) -> tuple[SmtpAddress, LoginSecret]:
    parsed, secret = parse("smtp", address, encrypted_secret)
    assert isinstance(parsed, SmtpAddress) and isinstance(secret, LoginSecret)
    try:
        tls_context(parsed.ca_certificate)
    except ValueError as exc:
        raise final_error("DestinationAddress", f"smtp address: {exc}") from None
    return parsed, secret


@activity.defn(name=SEND_EMAIL_ACTIVITY)
async def send_email(inp: SendEmailInput) -> SendEmailOutput:
    """One message to the destination's recipients. A resend carries the same Message-ID."""
    address, secret = _parsed(inp.address, inp.encrypted_secret)
    try:
        msg = message(address, inp)
    except ValueError:
        raise final_error("DeliveryFailed", "the message has an invalid header") from None
    timeout = EMAIL_SEND_ATTEMPT_SECONDS - ATTEMPT_MARGIN_SECONDS
    try:
        async with asyncio.timeout(timeout):
            refused = await pinned_tcp.pinned(
                address.host, address.port, lambda sock: _send(sock, address, secret, msg, timeout)
            )
    except Exception as exc:
        raise classified(exc, address.host, address.port, "send") from None
    log.info(
        "email_sent",
        host=address.host, message_id=inp.message_id, recipients=len(address.to), refused=refused,
    )
    return SendEmailOutput(refused=refused)
