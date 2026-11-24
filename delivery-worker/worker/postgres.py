"""The Postgres sink (ADR-015 §2d, §3, §5–§7): one statement per attempt on a connection to a checked
address, TLS verified against the destination's host name.

asyncpg dials the vetted IP; ``NamedContext`` hands TLS the host name instead of that IP, so SNI and the
certificate check use the name. One row per (run_id, block_id), never DDL; the client never retries:
the workflow is the one retry layer.
"""

import asyncio
import json
import ssl
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

import asyncpg
import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import egress
from .contracts import (
    DELIVER_POSTGRES_ACTIVITY,
    NOT_WRITTEN,
    POSTGRES_DELIVER_ATTEMPT_SECONDS,
    POSTGRES_UNDO_ATTEMPT_SECONDS,
    UNDO_POSTGRES_ACTIVITY,
    DeliverPostgresInput,
    DeliverPostgresOutput,
    LoginSecret,
    PostgresAddress,
    UndoPostgresInput,
    UndoPostgresOutput,
    row_location,
)
from .destinations import final_error, parse
from .source import Source, source_error

log = structlog.get_logger()

T = TypeVar("T")

# Room between this worker giving up and the workflow's start_to_close firing.
ATTEMPT_MARGIN_SECONDS = 5
# One JSON value in one parameter, held in memory (ADR-015 §7).
CONTENT_MAX_BYTES = 10 * 1024 * 1024

# SQLSTATE that a later try can get past (ADR-015 §6); every other code is final.
_TRANSIENT_CLASSES = ("08", "53")
_TRANSIENT_CODES = frozenset({"57P01", "57P02", "57P03", "40001", "40P01"})


class NamedContext(ssl.SSLContext):
    """Presents and verifies ``server_name`` whatever address the connection was opened to."""

    server_name: str

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        return super().wrap_bio(incoming, outgoing, server_side, self.server_name, session)


class NoTls(Exception):
    """The server answered the TLS request with "no": nothing is sent in plain text."""


def tls_context(host: str, ca_certificate: str | None) -> NamedContext:
    """The system's trust, or only the destination's CA when it brings one. Raises ValueError."""
    context = NamedContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_certificate is None:
        context.load_default_certs()
    else:
        try:
            context.load_verify_locations(cadata=ca_certificate)
        except (ssl.SSLError, ValueError):
            raise ValueError("CA certificate could not be read") from None
    context.server_name = host
    return context


def host_of(address: PostgresAddress) -> str:
    return address.host.rstrip(".").lower()


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table(address: PostgresAddress) -> str:
    return f"{_ident(address.db_schema)}.{_ident(address.table)}"


def upsert_sql(address: PostgresAddress) -> str:
    """One row per (run_id, block_id): a retry updates it (ADR-015 §5)."""
    return (
        f"INSERT INTO {_table(address)} (run_id, pipeline_id, block_id, delivered_at, content) "
        "VALUES ($1, $2, $3, now(), $4) "
        "ON CONFLICT (run_id, block_id) DO UPDATE "
        "SET content = EXCLUDED.content, delivered_at = EXCLUDED.delivered_at"
    )


def delete_sql(address: PostgresAddress) -> str:
    return f"DELETE FROM {_table(address)} WHERE run_id = $1 AND block_id = $2"


async def _connect(
    host: str, address: PostgresAddress, secret: LoginSecret, context: NamedContext
) -> asyncpg.Connection:
    """A login at a vetted address of ``host``: the next address only after a refused or timed-out
    connect, never after a TLS failure or the server's answer."""
    addresses = await egress.vetted_addresses(host, address.port)
    for i, ip in enumerate(addresses):
        try:
            return await asyncpg.connect(
                host=ip,
                port=address.port,
                user=secret.user,
                password=secret.password,
                database=address.database,
                ssl=context,
                direct_tls=False,
                timeout=egress.CONNECT_TIMEOUT_SECONDS,
                server_settings={"application_name": "scrapeflow"},
            )
        except ssl.SSLCertVerificationError:
            raise egress.CertificateMismatch(f"certificate not valid for {host}") from None
        except ssl.SSLError:
            raise
        except (OSError, TimeoutError) as exc:
            # ssl.SSLError is an OSError: caught above. asyncpg says "no TLS" with a ConnectionError.
            if "rejected SSL upgrade" in str(exc):
                raise NoTls() from None
            if i == len(addresses) - 1:
                raise
    raise AssertionError("resolve_public returned no address")


async def session(
    address: PostgresAddress,
    secret: LoginSecret,
    work: Callable[[asyncpg.Connection], Awaitable[T]],
) -> T:
    """Runs ``work(connection)`` on one pinned, TLS-verified login; the connection is closed after."""
    host = host_of(address)
    context = tls_context(host, address.ca_certificate)
    conn = await _connect(host, address, secret, context)
    try:
        result = await work(conn)
    except BaseException:
        conn.terminate()
        raise
    try:
        await conn.close(timeout=egress.CONNECT_TIMEOUT_SECONDS)
    except Exception:
        conn.terminate()
    return result


# --- the classifier (ADR-015 §6): known SQLSTATE and connection errors transient, unknown final --


def transient_sqlstate(code: str | None) -> bool:
    return code is not None and (code[:2] in _TRANSIENT_CLASSES or code in _TRANSIENT_CODES)


def describe(exc: BaseException, host: str, port: int, action: str) -> tuple[str, str, bool]:
    """(type, message, final). Built from the class and SQLSTATE, never the server's text."""
    if isinstance(exc, egress.EgressBlocked):
        return "EgressBlocked", str(exc), True
    if isinstance(exc, egress.CertificateMismatch):
        return "CertificateMismatch", str(exc), True
    if isinstance(exc, NoTls):
        return "TlsFailed", f"{host} does not offer TLS; nothing is sent in plain text", True
    if isinstance(exc, ssl.SSLError):
        return "TlsFailed", f"TLS handshake with {host} failed", True
    if isinstance(exc, asyncpg.exceptions.ConnectionDoesNotExistError):
        return "DestinationUnreachable", f"{host}:{port} closed the connection", False
    if isinstance(exc, asyncpg.PostgresError):
        code = getattr(exc, "sqlstate", None)
        what = f"{type(exc).__name__}, SQLSTATE {code}"
        if transient_sqlstate(code):
            return "DestinationUnavailable", f"{host} could not take the {action} now ({what})", False
        if code is not None and code.startswith("28"):
            return "DestinationRefused", f"{host} refused the login ({what})", True
        return "DestinationRefused", f"{host} refused the {action} ({what})", True
    if isinstance(exc, TimeoutError):
        return "DestinationUnreachable", f"{host}:{port} did not answer in time", False
    if isinstance(exc, OSError):
        return "DestinationUnreachable", f"could not reach {host}:{port} ({type(exc).__name__})", False
    return "DeliveryFailed", f"the {action} failed ({type(exc).__name__})", True


def answered(exc: BaseException) -> bool:
    """The server answered the statement with an error: one statement in autocommit did not commit.

    Class 08 is asyncpg's own for a lost connection (08003), not an answer."""
    code = getattr(exc, "sqlstate", None)
    return isinstance(exc, asyncpg.PostgresError) and code is not None and code[:2] != "08"


def classified(exc: BaseException, host: str, port: int, action: str) -> ApplicationError:
    type_, message, final = describe(exc, host, port, action)
    return ApplicationError(message, type=type_, non_retryable=final)


def _not_written(err: ApplicationError) -> ApplicationError:
    return ApplicationError(err.message, NOT_WRITTEN, type=err.type, non_retryable=err.non_retryable)


def parsed(address: dict, encrypted_secret: str) -> tuple[PostgresAddress, LoginSecret]:
    address_, secret = parse("postgres", address, encrypted_secret)
    assert isinstance(address_, PostgresAddress) and isinstance(secret, LoginSecret)
    try:
        tls_context(address_.host, address_.ca_certificate)
    except ValueError as exc:
        raise final_error("DestinationAddress", f"postgres address: {exc}") from None
    return address_, secret


async def read_json(source: Source, path: str) -> str:
    """ScrapeFlow's copy, whole, as one JSON text: at most CONTENT_MAX_BYTES. Raises ApplicationError."""
    async with source.open(path) as obj:
        if obj.length > CONTENT_MAX_BYTES:
            raise final_error(
                "ContentTooLarge",
                f"the input is {obj.length / 1024 / 1024:.1f} MiB; a postgres block takes at most "
                f"{CONTENT_MAX_BYTES // 1024 // 1024} MiB",
            )
        data = bytearray()
        try:
            async for chunk in obj.chunks():
                data += chunk
        except Exception as exc:
            raise source_error(obj.failure or exc) from None
    try:
        text = data.decode()
        json.loads(text)
    except ValueError:
        raise final_error("ContentNotJson", "the input is not a JSON document") from None
    return text


class PostgresActivities:
    def __init__(self, source: Source) -> None:
        self._source = source

    @activity.defn(name=DELIVER_POSTGRES_ACTIVITY)
    async def deliver_postgres(self, inp: DeliverPostgresInput) -> DeliverPostgresOutput:
        """INSERT the input as the run's and block's row, or update that row (ADR-015 §5).

        A failure carries ``NOT_WRITTEN`` unless the statement may have committed: it was sent and
        no answer from the server came back."""
        try:
            address, secret = parsed(inp.address, inp.encrypted_secret)
            location = row_location(inp.address, inp.run_id, inp.block_id)
            content = await read_json(self._source, inp.source_path)
        except ApplicationError as exc:
            raise _not_written(exc) from None

        sent = False

        async def write(conn: asyncpg.Connection) -> None:
            nonlocal sent
            statement = await conn.prepare(upsert_sql(address))
            sent = True
            await statement.fetch(inp.run_id, inp.pipeline_id, inp.block_id, content)

        try:
            async with asyncio.timeout(POSTGRES_DELIVER_ATTEMPT_SECONDS - ATTEMPT_MARGIN_SECONDS):
                await session(address, secret, write)
        except Exception as exc:
            error = classified(exc, host_of(address), address.port, "write")
            never = not sent or answered(exc)
            raise (_not_written(error) if never else error) from None

        size = len(content.encode())
        log.info(
            "postgres_delivered",
            host=address.host, table=f"{address.db_schema}.{address.table}", size=size,
            run_id=inp.run_id, block_id=inp.block_id,
        )
        return DeliverPostgresOutput(location=location, size=size)

    @activity.defn(name=UNDO_POSTGRES_ACTIVITY)
    async def undo_postgres(self, inp: UndoPostgresInput) -> UndoPostgresOutput:
        """DELETE the recorded row at the recorded host, database and table; no row is undone."""
        loc = inp.location
        address, secret = parsed(
            {
                "host": loc.host,
                "port": loc.port,
                "database": loc.database,
                "schema": loc.db_schema,
                "table": loc.table,
                "ca_certificate": inp.ca_certificate,
            },
            inp.encrypted_secret,
        )

        async def remove(conn: asyncpg.Connection) -> str:
            return await conn.execute(delete_sql(address), loc.run_id, loc.block_id)

        try:
            async with asyncio.timeout(POSTGRES_UNDO_ATTEMPT_SECONDS - ATTEMPT_MARGIN_SECONDS):
                status = await session(address, secret, remove)
        except Exception as exc:
            raise classified(exc, host_of(address), address.port, "delete") from None
        gone = status == "DELETE 0"
        log.info(
            "postgres_undone",
            host=address.host, table=f"{address.db_schema}.{address.table}",
            run_id=loc.run_id, block_id=loc.block_id, already_gone=gone,
        )
        return UndoPostgresOutput(already_gone=gone)


# --- the destination test's statements (ADR-015 §2d), one transaction, rolled back --------------

TABLE_SQL = (
    "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = $1 AND c.relname = $2 AND c.relkind IN ('r', 'p')"
)
_COLUMNS_SQL = (
    "SELECT attname, attnum FROM pg_attribute "
    "WHERE attrelid = $1 AND attnum > 0 AND NOT attisdropped AND attname = ANY($2::text[])"
)
# ON CONFLICT can use only a unique index that is valid, not deferred, not partial, on plain columns.
_UNIQUE_SQL = (
    "SELECT indkey::int2[] AS key, indnkeyatts AS keys FROM pg_index "
    "WHERE indrelid = $1 AND indisunique AND indisvalid AND indimmediate "
    "AND indpred IS NULL AND indexprs IS NULL"
)
ROW_KEY = ("run_id", "block_id")
PROBE_BLOCK_ID = ".scrapeflow-test"


async def has_row_key(conn: asyncpg.Connection, table_oid: int) -> bool:
    """A unique index on exactly (run_id, block_id), in either order."""
    columns = dict(await conn.fetch(_COLUMNS_SQL, table_oid, list(ROW_KEY)))
    if set(columns) != set(ROW_KEY):
        return False
    wanted = set(columns.values())
    for index in await conn.fetch(_UNIQUE_SQL, table_oid):
        key = list(index["key"])[: index["keys"]]
        if len(key) == len(wanted) and set(key) == wanted:
            return True
    return False


def probe_values() -> tuple[str, str, str, str]:
    """A row no run can have: run_id, pipeline_id, block_id, content."""
    return str(uuid.uuid4()), str(uuid.uuid4()), PROBE_BLOCK_ID, "{}"
