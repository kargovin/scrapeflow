"""O.1 — the Postgres sink against a real server: the gate fixture's Postgres behind its TLS front.

Each test module run makes its own schema, tables and roles as the fixture's superuser and drops them
after. Skipped unless ``PG_TEST_HOST`` is set. From the repo root, with the gate fixtures up
(``docker compose --profile gate up -d --wait delivery-postgres delivery-fixture`` in ./docker):

    docker run --rm --network docker_crawl-fixture -v "$PWD/delivery-worker:/app" -w /app \\
        -e PG_TEST_HOST=pg.deliveryfixture.com -e PG_TEST_SUPERUSER_PASSWORD=delivery-gate \\
        --entrypoint python delivery-worker-test -m pytest -q -p no:cacheprovider tests/test_postgres_db.py
"""

import json
import os
import urllib.request
import uuid

import asyncpg
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests.test_bucket import FakeSource
from worker import checks, postgres
from worker.config import settings
from worker.contracts import (
    NOT_WRITTEN,
    DeliverPostgresInput,
    DestinationTestInput,
    PostgresLocation,
    UndoPostgresInput,
)

HOST = os.environ.get("PG_TEST_HOST")
pytestmark = [
    pytest.mark.skipif(not HOST, reason="PG_TEST_HOST not set"),
    pytest.mark.asyncio(loop_scope="module"),
]

PIPELINE = "4c1f0e7a-0000-4000-8000-000000000001"
SCHEMA = f"o1_{uuid.uuid4().hex[:8]}"
PASSWORD = uuid.uuid4().hex
DOC = {"price": 12.5, "title": "Lamp"}

# The documented row (PRD-017 R1); every other table is a way to get it wrong.
TABLES = {
    "Rows": "PRIMARY KEY (run_id, block_id)",
    "reversed": "UNIQUE (block_id, run_id)",
    "no_key": None,
    "run_only": "UNIQUE (run_id)",
    "wider": "UNIQUE (run_id, block_id, pipeline_id)",
    "deferred": "UNIQUE (run_id, block_id) DEFERRABLE INITIALLY DEFERRED",
}


def _ca() -> str:
    with urllib.request.urlopen(f"http://{HOST}/_ca", timeout=10) as r:
        return r.read().decode()


async def _connect_super(ca: str) -> asyncpg.Connection:
    context = postgres.tls_context(HOST, ca)
    ip = (await postgres.egress.vetted_addresses(HOST, 5432))[0]
    return await asyncpg.connect(
        host=ip,
        port=5432,
        user="postgres",
        password=os.environ.get("PG_TEST_SUPERUSER_PASSWORD", "delivery-gate"),
        database="postgres",
        ssl=context,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    """(superuser connection, CA). Two roles: ``{schema}_writer`` may delete, ``_nodelete`` not."""
    ca = _ca()
    conn = await _connect_super(ca)
    await conn.execute(f"CREATE SCHEMA {SCHEMA}")
    for role, grants in (("writer", "SELECT, INSERT, UPDATE, DELETE"), ("nodelete", "SELECT, INSERT, UPDATE")):
        await conn.execute(f"CREATE ROLE {SCHEMA}_{role} LOGIN PASSWORD '{PASSWORD}'")
        await conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {SCHEMA}_{role}")
    for table, key in TABLES.items():
        constraint = f", {key}" if key else ""
        await conn.execute(
            f'CREATE TABLE {SCHEMA}."{table}" (run_id UUID NOT NULL, pipeline_id UUID NOT NULL, '
            f"block_id TEXT NOT NULL, delivered_at TIMESTAMPTZ NOT NULL, content JSONB NOT NULL"
            f"{constraint})"
        )
        await conn.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON {SCHEMA}."{table}" TO {SCHEMA}_writer'
        )
        await conn.execute(f'GRANT SELECT, INSERT, UPDATE ON {SCHEMA}."{table}" TO {SCHEMA}_nodelete')
    await conn.execute(
        f'CREATE UNIQUE INDEX partial_key ON {SCHEMA}."no_key" (run_id, block_id) '
        "WHERE block_id <> ''"
    )
    try:
        yield conn, ca
    finally:
        await conn.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
        for role in ("writer", "nodelete"):
            await conn.execute(f"DROP ROLE {SCHEMA}_{role}")
        await conn.close()


def _secret(role="writer", password=None) -> str:
    login = {"user": f"{SCHEMA}_{role}", "password": password or PASSWORD}
    return Fernet(settings.destination_encryption_key).encrypt(json.dumps(login).encode()).decode()


def _address(ca: str, table="Rows") -> dict:
    return {
        "host": HOST,
        "port": 5432,
        "database": "postgres",
        "schema": SCHEMA,
        "table": table,
        "ca_certificate": ca,
    }


async def _rows(conn, table="Rows", run_id=None) -> list:
    rows = await conn.fetch(
        f'SELECT run_id::text, pipeline_id::text, block_id, delivered_at, content::text '
        f'FROM {SCHEMA}."{table}" ORDER BY delivered_at'
    )
    return [r for r in rows if run_id is None or r["run_id"] == run_id]


async def _deliver(ca, run_id, block_id="db", doc=DOC, table="Rows", role="writer"):
    inp = DeliverPostgresInput(
        address=_address(ca, table),
        encrypted_secret=_secret(role),
        source_path="scrapeflow-results/pipelines/x/llm.json",
        pipeline_id=PIPELINE,
        run_id=run_id,
        block_id=block_id,
    )
    activities = postgres.PostgresActivities(FakeSource(json.dumps(doc).encode()))
    return await ActivityEnvironment().run(activities.deliver_postgres, inp)


async def _undo(ca, location: PostgresLocation, password=None):
    inp = UndoPostgresInput(location=location, encrypted_secret=_secret(password=password), ca_certificate=ca)
    activities = postgres.PostgresActivities(FakeSource())
    return await ActivityEnvironment().run(activities.undo_postgres, inp)


async def _test(ca, table="Rows", role="writer", password=None) -> list[tuple[str, str]]:
    inp = DestinationTestInput(
        kind="postgres", address=_address(ca, table), encrypted_secret=_secret(role, password)
    )
    out = await ActivityEnvironment().run(checks.test_postgres, inp)
    return [(c.check, c.outcome) for c in out.checks]


# --- deliver and undo -------------------------------------------------------------------------


async def test_a_delivery_writes_the_documented_row(db):
    conn, ca = db
    run_id = str(uuid.uuid4())
    out = await _deliver(ca, run_id)
    [row] = await _rows(conn, run_id=run_id)
    assert (row["run_id"], row["pipeline_id"], row["block_id"]) == (run_id, PIPELINE, "db")
    assert json.loads(row["content"]) == DOC
    assert out.location == postgres.row_location(_address(ca), run_id, "db")


async def test_what_the_content_check_lets_through_jsonb_holds(db):
    conn, ca = db
    run_id = str(uuid.uuid4())
    doc = {"pair": "\U0001f600", "backslash": "\\u0000", "n": -2.5, "z": None}
    await _deliver(ca, run_id, doc=doc)
    [row] = await _rows(conn, run_id=run_id)
    assert json.loads(row["content"]) == doc


async def test_a_second_try_updates_the_same_row(db):
    conn, ca = db
    run_id = str(uuid.uuid4())
    await _deliver(ca, run_id, doc={"v": 1})
    [first] = await _rows(conn, run_id=run_id)
    await _deliver(ca, run_id, doc={"v": 2})
    [row] = await _rows(conn, run_id=run_id)
    assert json.loads(row["content"]) == {"v": 2}
    assert row["delivered_at"] > first["delivered_at"]
    # Another block of the same run is another row.
    await _deliver(ca, run_id, block_id="db2")
    assert len(await _rows(conn, run_id=run_id)) == 2


async def test_a_reply_lost_after_the_commit_is_maybe_written_and_the_retry_makes_one_row(db):
    conn, ca = db
    run_id = str(uuid.uuid4())
    with pytest.raises(ApplicationError) as exc:
        await _deliver(ca, run_id, block_id="db_lostreply")
    assert (exc.value.non_retryable, exc.value.details) == (False, ())
    assert len(await _rows(conn, run_id=run_id)) == 1
    await _deliver(ca, run_id, block_id="db_lostreply")
    assert len(await _rows(conn, run_id=run_id)) == 1


async def test_a_table_without_the_key_refuses_the_write_not_written(db):
    _, ca = db
    with pytest.raises(ApplicationError) as exc:
        await _deliver(ca, str(uuid.uuid4()), table="run_only")
    assert (exc.value.type, exc.value.non_retryable, exc.value.details) == (
        "DestinationRefused",
        True,
        (NOT_WRITTEN,),
    )
    assert "SQLSTATE 42P10" in exc.value.message


async def test_the_undo_deletes_the_row_and_a_second_undo_finds_none(db):
    conn, ca = db
    run_id = str(uuid.uuid4())
    out = await _deliver(ca, run_id)
    await _deliver(ca, run_id, block_id="other")
    assert (await _undo(ca, out.location)).already_gone is False
    assert [r["block_id"] for r in await _rows(conn, run_id=run_id)] == ["other"]
    assert (await _undo(ca, out.location)).already_gone is True


async def test_an_undo_whose_login_is_refused_fails_and_leaves_the_row(db):
    conn, ca = db
    run_id = str(uuid.uuid4())
    out = await _deliver(ca, run_id)
    with pytest.raises(ApplicationError) as exc:
        await _undo(ca, out.location, password="wrong")
    assert (exc.value.type, exc.value.non_retryable) == ("DestinationRefused", True)
    assert "refused the login" in exc.value.message
    assert len(await _rows(conn, run_id=run_id)) == 1


# --- the destination test (ADR-015 §2d) ---------------------------------------------------------

PASSED = [("address", "passed"), ("tls", "passed"), ("login", "passed")]


@pytest.mark.parametrize("table", ["Rows", "reversed"])
async def test_the_test_passes_a_table_with_the_row_key_and_leaves_nothing(db, table):
    conn, ca = db
    before = len(await _rows(conn, table))
    assert await _test(ca, table) == [
        *PASSED,
        ("table", "passed"),
        ("unique key", "passed"),
        ("write", "passed"),
        ("delete", "passed"),
    ]
    assert len(await _rows(conn, table)) == before


@pytest.mark.parametrize("table", ["no_key", "run_only", "wider", "deferred"])
async def test_the_test_refuses_a_table_without_a_unique_index_on_exactly_the_row_key(db, table):
    _, ca = db
    assert await _test(ca, table) == [*PASSED, ("table", "passed"), ("unique key", "failed")]


async def test_the_test_refuses_a_missing_table(db):
    _, ca = db
    assert await _test(ca, "rows") == [*PASSED, ("table", "failed")]


async def test_the_test_warns_when_the_login_cannot_delete(db):
    conn, ca = db
    found = await _test(ca, role="nodelete")
    assert found[-1] == ("delete", "warning")
    assert found[:-1] == [*PASSED, ("table", "passed"), ("unique key", "passed"), ("write", "passed")]
    assert await conn.fetchval(f'SELECT count(*) FROM {SCHEMA}."Rows" WHERE block_id = $1', postgres.PROBE_BLOCK_ID) == 0


async def test_the_test_reports_a_wrong_password_as_the_login(db):
    _, ca = db
    assert await _test(ca, password="wrong") == [
        ("address", "passed"),
        ("tls", "passed"),
        ("login", "failed"),
    ]
