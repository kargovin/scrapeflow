"""The bucket sink (ADR-015 §3, §5–§7): one S3 PUT or DELETE per attempt, pinned to a checked address.

Requests are path-style (``https://{endpoint}/{bucket}/{key}``), SigV4-signed with an unsigned
payload (https only), never redirected, never retried here: the workflow is the one retry layer.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import aiohttp
import structlog
import yarl
from miniopy_async.credentials import Credentials
from miniopy_async.signer import sign_v4_s3
from miniopy_async.time import to_amz_date, utcnow
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import egress, pinned_http
from .contracts import (
    BUCKET_DELIVER_ATTEMPT_SECONDS,
    BUCKET_UNDO_ATTEMPT_SECONDS,
    DELIVER_BUCKET_ACTIVITY,
    NOT_WRITTEN,
    UNDO_BUCKET_ACTIVITY,
    BucketAddress,
    BucketLocation,
    BucketSecret,
    DeliverBucketInput,
    DeliverBucketOutput,
    UndoBucketInput,
    UndoBucketOutput,
    object_key,
)
from .destinations import final_error, parse
from .source import Source, source_error

log = structlog.get_logger()

# Room between this worker giving up and the workflow's start_to_close firing.
ATTEMPT_MARGIN_SECONDS = 5

DEFAULT_REGION = "us-east-1"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
CONTENT_TYPES = {"html": "text/html", "md": "text/markdown; charset=utf-8", "json": "application/json"}

# Read from the server's reply into an error message: tokens only, never its free text.
_CODE = re.compile(r"<Code>([A-Za-z0-9.]{1,64})</Code>")
_REGION = re.compile(r"^[a-z0-9-]{1,32}$")
_ERROR_BODY_BYTES = 16 * 1024

_TRANSIENT_STATUS = frozenset({408, 429})
# S3 answers RequestTimeout with a 400.
_TRANSIENT_CODES = frozenset({"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"})


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    netloc: str


def endpoint(url: str) -> Endpoint:
    """An https endpoint's host and port. Raises ValueError."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("must be an https endpoint")
    try:
        port = parts.port or 443
    except ValueError:
        raise ValueError("endpoint has an invalid port") from None
    host = parts.hostname.rstrip(".").lower()
    shown = f"[{host}]" if ":" in host else host
    return Endpoint(host, port, shown if port == 443 else f"{shown}:{port}")


@dataclass(frozen=True)
class Reply:
    status: int
    code: str | None = None
    region: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def missing_key(self) -> bool:
        return self.status == 404 and self.code == "NoSuchKey"


class Refused(Exception):
    def __init__(self, reply: Reply) -> None:
        super().__init__(reply.status)
        self.reply = reply


async def _reply(response: aiohttp.ClientResponse) -> Reply:
    if 200 <= response.status < 300:
        await response.read()
        return Reply(response.status)
    body = await response.content.read(_ERROR_BODY_BYTES)
    match = _CODE.search(body.decode("utf-8", errors="replace"))
    region = response.headers.get("x-amz-bucket-region")
    return Reply(
        response.status,
        match.group(1) if match else None,
        region if region and _REGION.match(region) else None,
    )


class BucketClient:
    def __init__(
        self, where: Endpoint, bucket: str, region: str | None, secret: BucketSecret
    ) -> None:
        self._where = where
        self._bucket = bucket
        self._region = region or DEFAULT_REGION
        self._credentials = Credentials(secret.access_key, secret.secret_key)

    def _request(self, method: str, key: str) -> tuple[yarl.URL, dict[str, str]]:
        path = "/" + quote(self._bucket, safe="") + "/" + quote(key, safe="/-_.~")
        url = f"https://{self._where.netloc}{path}"
        date = utcnow()
        headers = {
            "Host": self._where.netloc,
            "x-amz-content-sha256": UNSIGNED_PAYLOAD,
            "x-amz-date": to_amz_date(date),
        }
        signed = sign_v4_s3(
            method, urlsplit(url), self._region, headers, self._credentials, UNSIGNED_PAYLOAD, date
        )
        return yarl.URL(url, encoded=True), dict(signed)

    async def put(
        self, key: str, body: AsyncIterator[bytes] | bytes, length: int, content_type: str
    ) -> Reply:
        url, headers = self._request("PUT", key)
        # A known length: S3 refuses a chunked body.
        headers["Content-Length"] = str(length)
        headers["Content-Type"] = content_type

        async def attempt(session: aiohttp.ClientSession) -> Reply:
            # 100-continue: a refusal arrives before the body is sent, not as a dropped connection.
            async with session.put(
                url, data=body, headers=headers, allow_redirects=False, expect100=True
            ) as r:
                return await _reply(r)

        return await pinned_http.pinned(self._where.host, self._where.port, attempt)

    async def delete(self, key: str) -> Reply:
        url, headers = self._request("DELETE", key)

        async def attempt(session: aiohttp.ClientSession) -> Reply:
            async with session.delete(url, headers=headers, allow_redirects=False) as r:
                return await _reply(r)

        return await pinned_http.pinned(self._where.host, self._where.port, attempt)


# --- the classifier (ADR-015 §6): final unless known transient ------------------------------


def describe(exc: BaseException, where: Endpoint, action: str) -> tuple[str, str, bool]:
    """(type, message, final). Built from the class and non-secret fields, never the driver's text."""
    host = where.host
    if isinstance(exc, Refused):
        reply = exc.reply
        code = f"{reply.code}, HTTP {reply.status}" if reply.code else f"HTTP {reply.status}"
        hint = f"; the bucket is in region {reply.region}" if reply.region else ""
        if 300 <= reply.status < 400:
            return "DestinationRefused", f"{host} redirected the {action} ({code}){hint}", True
        transient = (
            reply.status >= 500
            or reply.status in _TRANSIENT_STATUS
            or reply.code in _TRANSIENT_CODES
        )
        if transient:
            return "DestinationUnavailable", f"{host} failed the {action} ({code})", False
        return "DestinationRefused", f"{host} refused the {action} ({code}){hint}", True
    if isinstance(exc, egress.EgressBlocked):
        return "EgressBlocked", str(exc), True
    if isinstance(exc, egress.CertificateMismatch):
        return "CertificateMismatch", str(exc), True
    if isinstance(exc, aiohttp.ClientSSLError):
        return "TlsFailed", f"TLS handshake with {host} failed", True
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return "EgressBlocked", f"{host} is not the checked host", True
    if isinstance(exc, aiohttp.ClientConnectionError | aiohttp.ClientPayloadError):
        return (
            "DestinationUnreachable",
            f"could not reach {host}:{where.port} ({type(exc).__name__})",
            False,
        )
    if isinstance(exc, TimeoutError):
        return "DestinationUnreachable", f"{host}:{where.port} did not answer in time", False
    return "DeliveryFailed", f"the {action} failed ({type(exc).__name__})", True


def classified(exc: BaseException, where: Endpoint, action: str) -> ApplicationError:
    type_, message, final = describe(exc, where, action)
    return ApplicationError(message, type=type_, non_retryable=final)


def _not_written(err: ApplicationError) -> ApplicationError:
    return ApplicationError(err.message, NOT_WRITTEN, type=err.type, non_retryable=err.non_retryable)


def _never_written(exc: BaseException) -> bool:
    """The bucket answered, or the request was never sent: the object cannot be there."""
    return isinstance(
        exc,
        Refused
        | egress.EgressBlocked
        | egress.CertificateMismatch
        | aiohttp.ClientConnectorError
        | aiohttp.ConnectionTimeoutError,
    )


def _endpoint_or_final(url: str) -> Endpoint:
    try:
        return endpoint(url)
    except ValueError as exc:
        raise final_error("DestinationAddress", f"bucket address: {exc}") from None


class BucketActivities:
    def __init__(self, source: Source) -> None:
        self._source = source

    @activity.defn(name=DELIVER_BUCKET_ACTIVITY)
    async def deliver_bucket(self, inp: DeliverBucketInput) -> DeliverBucketOutput:
        """PUT the input at its run-keyed key, streamed from ScrapeFlow's copy with its length.

        A failure carries ``NOT_WRITTEN`` unless the PUT may have stored the object."""
        try:
            address, secret = parse("bucket", inp.address, inp.encrypted_secret)
            where = _endpoint_or_final(address.endpoint)
        except ApplicationError as exc:
            raise _not_written(exc) from None
        assert isinstance(address, BucketAddress) and isinstance(secret, BucketSecret)
        key = object_key(address.prefix, inp.pipeline_id, inp.run_id, inp.block_id, inp.ext)
        client = BucketClient(where, address.bucket, address.region, secret)

        opened = False
        try:
            async with self._source.open(inp.source_path) as obj:
                opened = True
                try:
                    async with asyncio.timeout(
                        BUCKET_DELIVER_ATTEMPT_SECONDS - ATTEMPT_MARGIN_SECONDS
                    ):
                        reply = await client.put(
                            key, obj.chunks(), obj.length, CONTENT_TYPES[inp.ext]
                        )
                    if not reply.ok:
                        raise Refused(reply)
                except Exception as exc:
                    # A body cut short is refused for its length: nothing is stored.
                    if obj.failure is not None:
                        raise _not_written(source_error(obj.failure)) from None
                    error = classified(exc, where, "write")
                    raise (_not_written(error) if _never_written(exc) else error) from None
        except ApplicationError as exc:
            if opened:
                raise
            raise _not_written(exc) from None

        log.info(
            "bucket_delivered",
            host=where.host, bucket=address.bucket, key=key, size=obj.length, run_id=inp.run_id,
        )
        return DeliverBucketOutput(
            location=BucketLocation(
                endpoint=address.endpoint, bucket=address.bucket, region=address.region, key=key
            ),
            size=obj.length,
        )

    @activity.defn(name=UNDO_BUCKET_ACTIVITY)
    async def undo_bucket(self, inp: UndoBucketInput) -> UndoBucketOutput:
        """DELETE the recorded key at the recorded endpoint and bucket; a missing object is gone."""
        loc = inp.location
        _, secret = parse(
            "bucket",
            {"endpoint": loc.endpoint, "bucket": loc.bucket, "region": loc.region},
            inp.encrypted_secret,
        )
        assert isinstance(secret, BucketSecret)
        where = _endpoint_or_final(loc.endpoint)
        client = BucketClient(where, loc.bucket, loc.region, secret)
        try:
            async with asyncio.timeout(BUCKET_UNDO_ATTEMPT_SECONDS - ATTEMPT_MARGIN_SECONDS):
                reply = await client.delete(loc.key)
            if not (reply.ok or reply.missing_key):
                raise Refused(reply)
        except Exception as exc:
            raise classified(exc, where, "delete") from None
        log.info(
            "bucket_undone",
            host=where.host, bucket=loc.bucket, key=loc.key, already_gone=reply.missing_key,
        )
        return UndoBucketOutput(already_gone=reply.missing_key)
