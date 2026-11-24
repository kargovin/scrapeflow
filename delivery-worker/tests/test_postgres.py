"""O.1 — the Postgres sink without a database: the pinned TLS login, the classifier, NOT_WRITTEN, the
content cap. The statements against a real server: ``tests/test_postgres_db.py``."""

import json
import ssl

import asyncpg
import pytest
from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests.pg_server import pg_server
from tests.test_bucket import FakeSource
from worker import checks, egress, postgres
from worker.config import settings
from worker.contracts import (
    NOT_WRITTEN,
    DeliverPostgresInput,
    DestinationTestInput,
    PostgresLocation,
    UndoPostgresInput,
)

HOST = "db.example.com"
PIPELINE, RUN = "4c1f0e7a-0000-4000-8000-000000000001", "9d2e1b3c-0000-4000-8000-000000000002"
DOC = b'{"price": 12.5, "title": "Lamp"}'


def _secret(user: str = "writer", password: str = "pa55word") -> str:
    return (
        Fernet(settings.destination_encryption_key)
        .encrypt(json.dumps({"user": user, "password": password}).encode())
        .decode()
    )


def _address(server=None, ca="server", host=HOST, port=None) -> dict:
    return {
        "host": host,
        "port": port or (server.port if server else 5432),
        "database": "shop",
        "schema": "sink",
        "table": "Rows",
        "ca_certificate": (server.ca_pem if server else None) if ca == "server" else ca,
    }


def _deliver_input(server=None, **address) -> DeliverPostgresInput:
    return DeliverPostgresInput(
        address=_address(server, **address),
        encrypted_secret=_secret(),
        source_path="scrapeflow-results/pipelines/x/llm.json",
        pipeline_id=PIPELINE,
        run_id=RUN,
        block_id="db",
    )


async def _deliver(inp: DeliverPostgresInput, source=None):
    activities = postgres.PostgresActivities(source or FakeSource(DOC))
    return await ActivityEnvironment().run(activities.deliver_postgres, inp)


async def _deliver_error(inp: DeliverPostgresInput, source=None) -> ApplicationError:
    with pytest.raises(ApplicationError) as exc:
        await _deliver(inp, source)
    return exc.value


@pytest.fixture
def lookups(monkeypatch):
    """The checked addresses: loopback stands for a public answer; records each lookup."""
    seen: list[tuple[str, int]] = []
    answer = ["127.0.0.1"]

    async def vetted(host, port):
        seen.append((host, port))
        return list(answer)

    monkeypatch.setattr(egress, "vetted_addresses", vetted)
    return seen, answer


# --- the pinned TLS login (ADR-015 §3) -----------------------------------------------------------


async def test_the_login_goes_to_the_checked_address_and_tls_verifies_the_host_name(lookups):
    seen, _ = lookups
    async with pg_server(HOST) as server:
        err = await _deliver_error(_deliver_input(server, host="DB.Example.com."))
    # The name is looked up once and presented over TLS; the connection went to the vetted IP.
    assert seen == [(HOST, server.port)]
    assert server.sni == [HOST]
    assert server.logins == 1
    assert (err.type, err.non_retryable, err.details) == ("DestinationRefused", True, (NOT_WRITTEN,))
    assert err.message == f"{HOST} refused the login (InvalidPasswordError, SQLSTATE 28P01)"


async def test_a_certificate_for_another_name_is_final_and_nothing_is_sent(lookups):
    async with pg_server("other.example.com") as server:
        err = await _deliver_error(_deliver_input(server))
    assert (err.type, err.non_retryable, err.details) == ("CertificateMismatch", True, (NOT_WRITTEN,))
    assert err.message == f"certificate not valid for {HOST}"
    assert server.logins == 0


async def test_without_the_destinations_ca_the_systems_trust_refuses_a_private_ca(lookups):
    async with pg_server(HOST) as server:
        err = await _deliver_error(_deliver_input(server, ca=None))
    assert (err.type, err.non_retryable) == ("CertificateMismatch", True)
    assert server.logins == 0


async def test_a_server_without_tls_is_refused_and_nothing_goes_in_plain_text(lookups):
    async with pg_server(HOST, tls=False) as server:
        err = await _deliver_error(_deliver_input(server))
    assert (err.type, err.non_retryable, err.details) == ("TlsFailed", True, (NOT_WRITTEN,))
    assert err.message == f"{HOST} does not offer TLS; nothing is sent in plain text"
    assert (server.connections, server.logins) == (1, 0)


async def test_a_private_address_is_refused_before_any_connection():
    err = await _deliver_error(_deliver_input(host="localhost", port=5432))
    assert (err.type, err.non_retryable, err.details) == ("EgressBlocked", True, (NOT_WRITTEN,))
    assert "private address" in err.message


async def test_a_refused_connect_moves_on_to_the_next_checked_address(lookups):
    _, answer = lookups
    async with pg_server(HOST, bind="127.0.0.2") as server:
        answer[:] = ["127.0.0.1", "127.0.0.2"]
        err = await _deliver_error(_deliver_input(server))
    assert server.logins == 1
    assert err.type == "DestinationRefused"


async def test_a_tls_failure_does_not_move_on(lookups):
    _, answer = lookups
    async with pg_server("other.example.com") as first:
        async with pg_server(HOST, bind="127.0.0.2", port=first.port) as second:
            answer[:] = ["127.0.0.1", "127.0.0.2"]
            err = await _deliver_error(_deliver_input(second))
    assert err.type == "CertificateMismatch"
    assert (first.connections, second.connections) == (1, 0)


@pytest.mark.parametrize("sqlstate", ["53300", "57P03", "08006"])
async def test_a_login_the_server_cannot_take_now_is_transient_and_not_written(lookups, sqlstate):
    async with pg_server(HOST, sqlstate=sqlstate) as server:
        err = await _deliver_error(_deliver_input(server))
    assert (err.type, err.non_retryable, err.details) == (
        "DestinationUnavailable",
        False,
        (NOT_WRITTEN,),
    )
    assert f"SQLSTATE {sqlstate}" in err.message


# --- the classifier (ADR-015 §6) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "final"),
    [
        (asyncpg.exceptions.ConnectionFailureError("x"), False),  # 08006
        (asyncpg.exceptions.ClientCannotConnectError("x"), False),  # 08001
        (asyncpg.exceptions.TooManyConnectionsError("x"), False),  # 53300
        (asyncpg.exceptions.DiskFullError("x"), False),  # 53100
        (asyncpg.exceptions.OutOfMemoryError("x"), False),  # 53200
        (asyncpg.exceptions.AdminShutdownError("x"), False),  # 57P01
        (asyncpg.exceptions.CrashShutdownError("x"), False),  # 57P02
        (asyncpg.exceptions.CannotConnectNowError("x"), False),  # 57P03
        (asyncpg.exceptions.SerializationError("x"), False),  # 40001
        (asyncpg.exceptions.DeadlockDetectedError("x"), False),  # 40P01
        (asyncpg.exceptions.ConnectionDoesNotExistError("x"), False),
        (TimeoutError(), False),
        (ConnectionRefusedError(), False),
        (asyncpg.exceptions.InvalidPasswordError("x"), True),  # 28P01
        (asyncpg.exceptions.InvalidAuthorizationSpecificationError("x"), True),  # 28000
        (asyncpg.exceptions.InsufficientPrivilegeError("x"), True),  # 42501
        (asyncpg.exceptions.UndefinedTableError("x"), True),  # 42P01
        (asyncpg.exceptions.InvalidColumnReferenceError("x"), True),  # 42P10: no unique key
        (asyncpg.exceptions.UniqueViolationError("x"), True),  # 23505
        (asyncpg.exceptions.QueryCanceledError("x"), True),  # 57014
        (asyncpg.exceptions.TransactionRollbackError("x"), True),  # 40000: the class, not a code
        (asyncpg.exceptions.InvalidCatalogNameError("x"), True),  # 3D000
        (asyncpg.exceptions.DataError("x"), True),  # 22000
        (ValueError("x"), True),
    ],
)
def test_known_sqlstates_and_connection_errors_are_transient_everything_else_final(exc, final):
    _type, _message, is_final = postgres.describe(exc, HOST, 5432, "write")
    assert is_final is final


def test_an_error_message_never_carries_the_servers_text():
    exc = asyncpg.exceptions.UniqueViolationError('Key (run_id)=(secret-value) already exists')
    _, message, _ = postgres.describe(exc, HOST, 5432, "write")
    assert "secret-value" not in message
    assert message == f"{HOST} refused the write (UniqueViolationError, SQLSTATE 23505)"


# --- NOT_WRITTEN: only where the statement cannot have committed ---------------------------------


class FakeStatement:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def fetch(self, *args):
        self.conn.args = args
        if self.conn.on_fetch is not None:
            await self.conn.on_fetch()
        return []


class FakeConnection:
    def __init__(self, on_prepare=None, on_fetch=None) -> None:
        self.on_prepare, self.on_fetch = on_prepare, on_fetch
        self.sql: str | None = None
        self.args: tuple | None = None

    async def prepare(self, sql: str):
        self.sql = sql
        if self.on_prepare is not None:
            await self.on_prepare()
        return FakeStatement(self)


@pytest.fixture
def connection(monkeypatch):
    """postgres.session on a scripted connection."""
    state: dict = {"conn": FakeConnection(), "login": None}

    async def session(address, secret, work):
        if state["login"] is not None:
            raise state["login"]
        return await work(state["conn"])

    monkeypatch.setattr(postgres, "session", session)
    return state


def _raise(exc):
    async def go():
        raise exc

    return go


async def test_the_row_is_the_runs_and_the_blocks_and_its_upsert_keys_on_both(connection):
    out = await _deliver(_deliver_input())
    conn = connection["conn"]
    assert conn.sql == (
        'INSERT INTO "sink"."Rows" (run_id, pipeline_id, block_id, delivered_at, content) '
        "VALUES ($1, $2, $3, now(), $4) "
        "ON CONFLICT (run_id, block_id) DO UPDATE "
        "SET content = EXCLUDED.content, delivered_at = EXCLUDED.delivered_at"
    )
    assert conn.args == (RUN, PIPELINE, "db", DOC.decode())
    assert out.location == PostgresLocation(
        host=HOST, port=5432, database="shop", schema="sink", table="Rows", run_id=RUN, block_id="db"
    )
    assert out.size == len(DOC)


def test_identifiers_are_quoted_whatever_they_hold():
    address = postgres.PostgresAddress.model_validate(
        {**_address(), "schema": 'we"ird', "table": "a; DROP TABLE x"}
    )
    assert postgres.delete_sql(address) == (
        'DELETE FROM "we""ird"."a; DROP TABLE x" WHERE run_id = $1 AND block_id = $2'
    )


@pytest.mark.parametrize(
    ("where", "exc", "written"),
    [
        ("login", ConnectionRefusedError(), False),
        ("login", asyncpg.exceptions.InvalidPasswordError("x"), False),
        ("prepare", asyncpg.exceptions.UndefinedTableError("x"), False),
        ("prepare", asyncpg.exceptions.ConnectionDoesNotExistError("x"), False),
        # Sent: an answer that is an error means it did not commit; no answer may mean it did.
        ("fetch", asyncpg.exceptions.SerializationError("x"), False),
        ("fetch", asyncpg.exceptions.UniqueViolationError("x"), False),
        ("fetch", asyncpg.exceptions.ConnectionDoesNotExistError("x"), True),
        ("fetch", ConnectionResetError(), True),
    ],
)
async def test_not_written_only_where_the_insert_cannot_have_committed(
    connection, where, exc, written
):
    if where == "login":
        connection["login"] = exc
    else:
        connection["conn"] = FakeConnection(**{f"on_{where}": _raise(exc)})
    err = await _deliver_error(_deliver_input())
    assert (NOT_WRITTEN in err.details) is not written


async def test_an_insert_that_outlives_the_attempt_may_have_committed(connection, monkeypatch):
    import asyncio

    monkeypatch.setattr(postgres, "POSTGRES_DELIVER_ATTEMPT_SECONDS", 5.05)
    connection["conn"] = FakeConnection(on_fetch=lambda: asyncio.sleep(5))
    err = await _deliver_error(_deliver_input())
    assert (err.type, err.non_retryable, err.details) == ("DestinationUnreachable", False, ())


# --- the content (ADR-015 §7) --------------------------------------------------------------------


async def test_content_over_10_mib_is_final_and_nothing_is_sent(lookups):
    seen, _ = lookups
    big = b'"' + b"x" * (10 * 1024 * 1024) + b'"'
    err = await _deliver_error(_deliver_input(), FakeSource(big))
    assert (err.type, err.non_retryable, err.details) == ("ContentTooLarge", True, (NOT_WRITTEN,))
    assert err.message.startswith("the input is 10.0 MiB; a postgres block takes at most 10 MiB")
    assert seen == []


async def test_content_at_10_mib_is_read_whole(connection):
    doc = b'"' + b"x" * (10 * 1024 * 1024 - 2) + b'"'
    out = await _deliver(_deliver_input(), FakeSource(doc))
    assert out.size == len(doc) and len(connection["conn"].args[3]) == len(doc)


async def test_content_that_is_not_json_is_final(lookups):
    err = await _deliver_error(_deliver_input(), FakeSource(b"<html></html>"))
    assert (err.type, err.non_retryable, err.details) == ("ContentNotJson", True, (NOT_WRITTEN,))


@pytest.mark.parametrize(
    ("doc", "has"),
    [
        (b'{"price": NaN}', "NaN"),
        (b"[1, Infinity]", "Infinity"),
        (b'{"low": -Infinity}', "-Infinity"),
    ],
)
async def test_nan_and_infinity_are_final_and_name_the_content(lookups, doc, has):
    seen, _ = lookups
    err = await _deliver_error(_deliver_input(), FakeSource(doc))
    assert (err.type, err.non_retryable, err.details) == ("ContentNotJson", True, (NOT_WRITTEN,))
    assert err.message == f"the input has {has}, which JSON in Postgres cannot hold"
    assert seen == []


@pytest.mark.parametrize(
    "doc",
    [
        rb'"a\u0000b"',
        rb'{"k": ["x", {"y": "\u0000"}]}',
        rb'{"\u0000key": 1}',
        rb'{"outer": {"in\u0000ner": true}}',
        rb'["\ud800"]',
        rb'{"t": "x\udc00"}',
    ],
)
async def test_a_nul_or_an_unpaired_surrogate_anywhere_is_final_and_names_the_content(lookups, doc):
    seen, _ = lookups
    err = await _deliver_error(_deliver_input(), FakeSource(doc))
    assert (err.type, err.non_retryable, err.details) == ("ContentNotJson", True, (NOT_WRITTEN,))
    assert err.message.startswith("the input has a NUL character")
    assert seen == []


@pytest.mark.parametrize(
    "doc", [rb'"\\u0000"', rb'{"e": "\ud83d\ude00", "n": -1.5e300, "z": null}']
)
async def test_an_escaped_backslash_a_surrogate_pair_and_finite_numbers_are_written(connection, doc):
    await _deliver(_deliver_input(), FakeSource(doc))
    assert connection["conn"].args[3] == doc.decode()


async def test_nesting_deeper_than_the_parser_is_final(lookups):
    err = await _deliver_error(_deliver_input(), FakeSource(b"[" * 200_000))
    assert (err.type, err.non_retryable, err.details) == ("ContentNotJson", True, (NOT_WRITTEN,))


async def test_a_source_dropped_mid_read_is_transient_and_not_written(lookups):
    err = await _deliver_error(_deliver_input(), FakeSource(b"[" + b"1," * 100_000 + b"1]", fail_mid=True))
    assert (err.type, err.non_retryable, err.details) == ("SourceUnavailable", False, (NOT_WRITTEN,))


# --- the undo and the test -----------------------------------------------------------------------


def _undo_input(server, password="pa55word") -> UndoPostgresInput:
    return UndoPostgresInput(
        location=PostgresLocation(
            host=HOST, port=server.port, database="shop", schema="sink", table="Rows",
            run_id=RUN, block_id="db",
        ),
        encrypted_secret=_secret(password=password),
        ca_certificate=server.ca_pem,
    )


async def test_an_undo_whose_login_is_refused_is_final(lookups):
    async with pg_server(HOST) as server:
        activities = postgres.PostgresActivities(FakeSource())
        with pytest.raises(ApplicationError) as exc:
            await ActivityEnvironment().run(activities.undo_postgres, _undo_input(server))
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationRefused", True)
    assert exc.value.message == f"{HOST} refused the login (InvalidPasswordError, SQLSTATE 28P01)"
    assert server.sni == [HOST]


async def _test(address: dict) -> list[tuple[str, str, str | None]]:
    inp = DestinationTestInput(kind="postgres", address=address, encrypted_secret=_secret())
    out = await ActivityEnvironment().run(checks.test_postgres, inp)
    return [(c.check, c.outcome, c.message) for c in out.checks]


async def test_the_test_reports_a_refused_login_after_tls(lookups):
    async with pg_server(HOST) as server:
        found = await _test(_address(server))
    assert found == [
        ("address", "passed", None),
        ("tls", "passed", None),
        ("login", "failed", f"{HOST} refused the login (InvalidPasswordError, SQLSTATE 28P01)"),
    ]


async def test_the_test_reports_a_server_without_tls(lookups):
    async with pg_server(HOST, tls=False) as server:
        found = await _test(_address(server))
    assert found == [
        ("address", "passed", None),
        ("tls", "failed", f"{HOST} does not offer TLS; nothing is sent in plain text"),
    ]


async def test_the_test_reports_a_private_address():
    found = await _test(_address(host="localhost"))
    assert [(c, o) for c, o, _ in found] == [("address", "failed")]


async def test_an_unreadable_ca_is_refused_before_any_lookup(lookups):
    seen, _ = lookups
    found = await _test(_address(ca="not a certificate"))
    assert found == [("address", "failed", "CA certificate could not be read")]
    assert seen == []


async def test_the_test_reports_a_refused_connect_as_the_address(lookups):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    found = await _test(_address(port=port))
    assert found == [
        ("address", "failed", f"could not reach {HOST}:{port} (ConnectionRefusedError)")
    ]


async def test_the_test_reports_a_certificate_for_another_name_as_tls(lookups):
    async with pg_server("other.example.com") as server:
        found = await _test(_address(server))
    assert found == [("address", "passed", None), ("tls", "failed", f"certificate not valid for {HOST}")]


class FakeTransaction:
    def __init__(self, conn) -> None:
        self.conn = conn

    async def start(self):
        self.conn.calls.append("begin")

    async def rollback(self):
        self.conn.calls.append("rollback")
        await self.conn.step("rollback")


class FakeTestConnection:
    """Answers each statement of the test; ``fail`` maps a step to the exception it raises."""

    def __init__(self, fail: dict) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def step(self, name: str):
        exc = self.fail.get(name)
        if callable(exc) and not isinstance(exc, BaseException):
            await exc()
        elif exc is not None:
            raise exc

    def transaction(self):
        return FakeTransaction(self)

    async def fetchval(self, sql, *args):
        await self.step("table")
        return 42

    async def execute(self, sql, *args):
        name = "write" if sql.startswith("INSERT") else "delete"
        self.calls.append(name)
        await self.step(name)
        return "INSERT 0 1" if name == "write" else "DELETE 1"


@pytest.fixture
def test_session(monkeypatch):
    """postgres.session for the destination test: a scripted login, then a scripted connection."""
    state: dict = {"login": None, "fail": {}, "conn": None}

    async def session(address, secret, work):
        if state["login"] is not None:
            raise state["login"]
        state["conn"] = FakeTestConnection(state["fail"])
        return await work(state["conn"])

    async def has_row_key(conn, oid):
        await conn.step("unique key")
        return True

    monkeypatch.setattr(postgres, "session", session)
    monkeypatch.setattr(postgres, "has_row_key", has_row_key)
    return state


LOGGED_IN = [("address", "passed", None), ("tls", "passed", None), ("login", "passed", None)]
GONE = asyncpg.exceptions.ConnectionDoesNotExistError("x")


@pytest.mark.parametrize(
    ("exc", "found"),
    [
        (ConnectionRefusedError(), [("address", "failed", f"could not reach {HOST}:5432 (ConnectionRefusedError)")]),
        (TimeoutError(), [("address", "failed", f"{HOST}:5432 did not answer in time")]),
        (
            ssl.SSLError("x"),
            [("address", "passed", None), ("tls", "failed", f"TLS handshake with {HOST} failed")],
        ),
        (
            postgres.NoTls(),
            [
                ("address", "passed", None),
                ("tls", "failed", f"{HOST} does not offer TLS; nothing is sent in plain text"),
            ],
        ),
        (
            asyncpg.exceptions.InvalidCatalogNameError("x"),
            [
                ("address", "passed", None),
                ("tls", "passed", None),
                ("login", "failed", f"{HOST} refused the login (InvalidCatalogNameError, SQLSTATE 3D000)"),
            ],
        ),
        (
            asyncpg.exceptions.TooManyConnectionsError("x"),
            [
                ("address", "passed", None),
                ("tls", "passed", None),
                ("login", "failed", f"{HOST} could not take the login now (TooManyConnectionsError, SQLSTATE 53300)"),
            ],
        ),
    ],
)
async def test_a_connect_that_never_logs_in_fails_the_check_it_stopped_at(test_session, exc, found):
    test_session["login"] = exc
    assert await _test(_address()) == found


@pytest.mark.parametrize(
    ("step", "exc", "found"),
    [
        (
            "table",
            asyncpg.exceptions.InsufficientPrivilegeError("x"),
            [("table", "failed", f"{HOST} refused the table lookup (InsufficientPrivilegeError, SQLSTATE 42501)")],
        ),
        (
            "unique key",
            GONE,
            [("table", "passed", None), ("unique key", "failed", f"{HOST}:5432 closed the connection")],
        ),
        (
            "write",
            GONE,
            [
                ("table", "passed", None),
                ("unique key", "passed", None),
                ("write", "failed", f"{HOST}:5432 closed the connection"),
            ],
        ),
        (
            "delete",
            GONE,
            [
                ("table", "passed", None),
                ("unique key", "passed", None),
                ("write", "passed", None),
                ("delete", "failed", f"{HOST}:5432 closed the connection"),
            ],
        ),
        (
            "rollback",
            GONE,
            [
                ("table", "passed", None),
                ("unique key", "passed", None),
                ("write", "passed", None),
                ("delete", "passed", None),
                ("rollback", "failed", f"{HOST}:5432 closed the connection"),
            ],
        ),
    ],
)
async def test_an_error_after_the_login_fails_the_check_that_was_running(test_session, step, exc, found):
    test_session["fail"] = {step: exc}
    assert await _test(_address()) == [*LOGGED_IN, *found]


async def test_the_test_deadline_fails_the_check_that_was_running(test_session, monkeypatch):
    import asyncio

    monkeypatch.setattr(checks, "TEST_SECONDS", 0.05)
    test_session["fail"] = {"write": lambda: asyncio.sleep(1)}
    found = await _test(_address())
    assert found[-1] == ("write", "failed", f"{HOST}:5432 did not answer in time")
    assert found[:-1] == [*LOGGED_IN, ("table", "passed", None), ("unique key", "passed", None)]


async def test_a_write_or_delete_the_server_answered_is_that_checks_outcome_and_rolled_back(test_session):
    refused = asyncpg.exceptions.InsufficientPrivilegeError("x")
    test_session["fail"] = {"delete": refused}
    found = await _test(_address())
    assert found[-1] == (
        "delete",
        "warning",
        f"delete refused — rollback to this table will fail ({HOST} refused the delete "
        "(InsufficientPrivilegeError, SQLSTATE 42501))",
    )
    assert test_session["conn"].calls == ["begin", "write", "delete", "rollback"]
    test_session["fail"] = {"write": refused}
    found = await _test(_address())
    assert found[-1] == (
        "write", "failed", f"{HOST} refused the write (InsufficientPrivilegeError, SQLSTATE 42501)"
    )
    assert test_session["conn"].calls == ["begin", "write", "rollback"]
