"""The delivery gate's fixture (M.12, O.1): a TLS front for a bucket, a webhook receiver and a
Postgres server, an SMTP sink, and the CA that signed their certificate.

    s3.deliveryfixture.com     :443  TLS, then MinIO (minio:9000). Three faults, each on the FIRST request
                                     whose object path holds the word:
                                       PUT    "waitput"    closed unanswered (nothing written)
                                       PUT    "lostreply"  written by MinIO, the reply dropped
                                       DELETE "waitundo"   closed unanswered (nothing deleted)
    hooks.deliveryfixture.com  :443  TLS, then the pipeline fixture's receiver (pipeline-fixture:80)
    mail.deliveryfixture.com   :587  SMTP: STARTTLS, then AUTH PLAIN; a password starting "wrong" → 535;
                                     a recipient starting "defer-once" → 451 at the end of DATA, the
                                     first time each Message-ID arrives (listed under "deferred");
                                     a recipient starting "defer-rcpt" → 451 to its RCPT, the first time
                                     each address arrives; each message lists its envelope ("rcpt")
    pg.deliveryfixture.com     :5432 Postgres: TLS (the client's SSLRequest), then delivery-postgres:5432
                                     in plain text. Two faults, each on the FIRST statement of its kind
                                     for a run whose parameters hold the word:
                                       INSERT "lostreply"  committed, the reply dropped
                                       DELETE "waitundo"   closed before the statement (nothing deleted)
                               :5433 answers an SSLRequest with "N": a server without TLS
    any of the four            :80   GET /_ca  the CA (PEM) · /_mail  sessions and messages · /_s3  requests
                                     · /_pg  INSERT and DELETE statements, their parameters and outcome

The CA, the certificate (all three names) and ``bundle.pem`` (the image's trust store plus the CA) are
written to /pki on first start and kept: the workers read the bundle once. Run by the compose service
``delivery-fixture`` (profile ``gate``); ``docker/delivery-gate.yml`` hands /pki/bundle.pem to the
delivery and workflow workers as SSL_CERT_FILE. Needs ``cryptography`` (the delivery worker's image).
"""

import asyncio
import base64
import datetime
import email
import email.policy
import hashlib
import json
import ssl
import struct
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PKI = Path("/pki")
SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
NAMES = [
    "s3.deliveryfixture.com",
    "hooks.deliveryfixture.com",
    "mail.deliveryfixture.com",
    "pg.deliveryfixture.com",
]
UPSTREAMS = {"s3.": ("minio", 9000), "hooks.": ("pipeline-fixture", 80)}
FAULTS = {(b"PUT", b"waitput"), (b"PUT", b"lostreply"), (b"DELETE", b"waitundo")}
PG_UPSTREAM = ("delivery-postgres", 5432)
PG_FAULTS = {("INSERT", "lostreply"), ("DELETE", "waitundo")}
SSL_REQUEST = 80877103

SEEN: dict[tuple[bytes, bytes], int] = {}
S3_LOG: list[dict] = []
SESSIONS: list[dict] = []
MESSAGES: list[dict] = []
DEFERRED: list[str] = []
DEFERRED_RCPT: set[str] = set()
PG_SEEN: set[tuple[str, str]] = set()
PG_LOG: list[dict] = []


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _covers_names() -> bool:
    cert = x509.load_pem_x509_certificate((PKI / "server.pem").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(san.get_values_for_type(x509.DNSName)) == set(NAMES)


def make_pki() -> None:
    files = ("ca.pem", "server.pem", "server.key", "bundle.pem")
    if all((PKI / f).exists() for f in files) and _covers_names():
        return
    # The healthcheck must not see the old bundle while the new one is made.
    (PKI / "bundle.pem").unlink(missing_ok=True)
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("ScrapeFlow delivery gate CA"))
        .issuer_name(_name("ScrapeFlow delivery gate CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(NAMES[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in NAMES]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    PKI.mkdir(exist_ok=True)
    (PKI / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (PKI / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (PKI / "ca.pem").write_bytes(ca_pem)
    # Last: its presence is the healthcheck.
    (PKI / "bundle.pem").write_bytes(SYSTEM_BUNDLE.read_bytes() + b"\n" + ca_pem)


def server_context() -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(PKI / "server.pem", PKI / "server.key")
    return context


# --- the TLS front --------------------------------------------------------------------------


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


def header(head: bytes, name: bytes) -> bytes:
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        if key.strip().lower() == name:
            return value.strip()
    return b""


def one_request(head: bytes, drop=(b"connection",)) -> bytes:
    """The head with ``Connection: close``: every request gets its own connection, so each meets the faults."""
    lines = [
        line
        for line in head.rstrip(b"\r\n").split(b"\r\n")
        if line.partition(b":")[0].strip().lower() not in drop
    ]
    return b"\r\n".join([*lines, b"Connection: close"]) + b"\r\n\r\n"


async def front(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        writer.close()
        return
    method, path = (head.split(b"\r\n", 1)[0].split(b" ") + [b"", b""])[:2]
    host = header(head, b"host").decode().split(":")[0]
    upstream = next((u for prefix, u in UPSTREAMS.items() if host.startswith(prefix)), None)
    if upstream is None:
        writer.close()
        return
    if host.startswith("s3."):
        key = path.split(b"?")[0]
        fault = next((word for m, word in FAULTS if m == method and word in key), None)
        n = SEEN[(method, key)] = SEEN.get((method, key), 0) + 1
        outcome = "passed"
        if fault is not None and n == 1:
            outcome = {b"waitput": "closed", b"waitundo": "closed", b"lostreply": "reply dropped"}[fault]
        S3_LOG.append({"method": method.decode(), "path": key.decode(), "outcome": outcome})
        print("s3", method.decode(), key.decode(), outcome, flush=True)
        if outcome == "closed":
            writer.close()
            return
        if outcome == "reply dropped":
            if b"100-continue" in header(head, b"expect").lower():
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            body = await reader.readexactly(int(header(head, b"content-length") or 0))
            up_reader, up_writer = await asyncio.open_connection(*upstream)
            up_writer.write(one_request(head, drop=(b"connection", b"expect")) + body)
            await up_writer.drain()
            await up_reader.readline()
            up_writer.close()
            writer.close()
            return
    up_reader, up_writer = await asyncio.open_connection(*upstream)
    up_writer.write(one_request(head))
    await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


# --- the SMTP sink --------------------------------------------------------------------------


async def smtp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    session = {"tls": False, "user": None, "password_sha256": None, "auth": None, "messages": 0}
    SESSIONS.append(session)
    recipients: list[str] = []

    def send(line: str) -> None:
        writer.write((line + "\r\n").encode())

    def login(raw: str) -> None:
        _, user, password = base64.b64decode(raw).decode().split("\0")
        session["user"] = user
        session["password_sha256"] = hashlib.sha256(password.encode()).hexdigest()
        session["auth"] = "refused" if password.startswith("wrong") else "accepted"
        send("535 authentication failed" if session["auth"] == "refused" else "235 accepted")

    send(f"220 {NAMES[2]}")
    await writer.drain()
    try:
        while line := await reader.readline():
            text = line.decode().strip()
            verb = text.split(" ")[0].split(":")[0].upper()
            if verb == "EHLO":
                extensions = [NAMES[2], "AUTH PLAIN" if session["tls"] else "STARTTLS"]
                for i, extension in enumerate(extensions):
                    send(("250 " if i == len(extensions) - 1 else "250-") + extension)
            elif verb == "STARTTLS":
                send("220 go ahead")
                await writer.drain()
                await writer.start_tls(server_context())
                session["tls"] = True
                continue
            elif verb == "AUTH":
                parts = text.split()
                if len(parts) > 2:
                    login(parts[2])
                else:
                    send("334 ")
                    await writer.drain()
                    login((await reader.readline()).decode().strip())
            elif verb == "DATA":
                send("354 go ahead")
                await writer.drain()
                data = bytearray()
                while (chunk := await reader.readline()) not in (b".\r\n", b""):
                    data += chunk
                message = email.message_from_bytes(bytes(data), policy=email.policy.default)
                message_id = str(message["Message-ID"])
                if any(r.startswith("defer-once") for r in recipients) and message_id not in DEFERRED:
                    DEFERRED.append(message_id)
                    print("mail deferred", message_id, flush=True)
                    send("451 try again later")
                    await writer.drain()
                    continue
                session["messages"] += 1
                MESSAGES.append(
                    {
                        "message_id": message_id,
                        "subject": str(message["Subject"]),
                        "from": str(message["From"]),
                        "to": str(message["To"]),
                        "user": session["user"],
                        "password_sha256": session["password_sha256"],
                        "session": len(SESSIONS) - 1,
                        "rcpt": list(recipients),
                        "body": (message.get_body(("plain",)) or message).get_content(),
                    }
                )
                print("mail", message["Message-ID"], flush=True)
                send("250 queued")
            elif verb == "MAIL":
                recipients.clear()
                send("250 ok")
            elif verb == "RCPT":
                rcpt = text.split(":", 1)[1].strip().strip("<>").lower()
                if rcpt.startswith("defer-rcpt") and rcpt not in DEFERRED_RCPT:
                    DEFERRED_RCPT.add(rcpt)
                    send("451 try again later")
                else:
                    recipients.append(rcpt)
                    send("250 ok")
            elif verb == "QUIT":
                send("221 bye")
                await writer.drain()
                break
            else:
                send("250 ok")
            await writer.drain()
    except Exception as exc:
        print("smtp session ended:", type(exc).__name__, flush=True)
    writer.close()


# --- the Postgres front ---------------------------------------------------------------------


async def _message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    kind = await reader.readexactly(1)
    (length,) = struct.unpack("!i", await reader.readexactly(4))
    return kind, await reader.readexactly(length - 4)


def _frame(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("!i", len(body) + 4) + body


def _bind_params(body: bytes) -> tuple[bytes, list[str]]:
    """(statement name, parameters as text: a 16-byte value read as a UUID)."""
    _portal, statement, rest = body.split(b"\0", 2)
    (formats,) = struct.unpack("!h", rest[:2])
    at = 2 + 2 * formats
    (count,) = struct.unpack("!h", rest[at : at + 2])
    at += 2
    params = []
    for _ in range(count):
        (size,) = struct.unpack("!i", rest[at : at + 4])
        at += 4
        value = b"" if size < 0 else rest[at : at + size]
        at += max(size, 0)
        params.append(
            str(uuid.UUID(bytes=value))
            if size == 16
            else value.lstrip(b"\x01").decode("utf-8", "replace")[:200]
        )
    return statement, params


async def pg_client(
    reader: asyncio.StreamReader, up: asyncio.StreamWriter, down: asyncio.StreamWriter, conn: dict
) -> None:
    try:
        (length,) = struct.unpack("!i", await reader.readexactly(4))
        up.write(struct.pack("!i", length) + await reader.readexactly(length - 4))
        while True:
            kind, body = await _message(reader)
            if kind == b"P":
                name, query, _ = body.split(b"\0", 2)
                words = query.split(None, 1)
                conn["statements"][name] = words[0].upper().decode() if words else ""
            elif kind == b"B":
                name, params = _bind_params(body)
                statement = conn["statements"].get(name, "")
                if statement in ("INSERT", "DELETE"):
                    entry = {"statement": statement, "params": params, "outcome": "passed", "tag": None}
                    word = next((w for s, w in PG_FAULTS if s == statement and any(w in p for p in params)), None)
                    key = (statement, params[0] if params else "")
                    if word is not None and key not in PG_SEEN:
                        PG_SEEN.add(key)
                        entry["outcome"] = "closed" if statement == "DELETE" else "reply dropped"
                    PG_LOG.append(entry)
                    conn["entry"] = entry
                    print("pg", statement, params[:3], entry["outcome"], flush=True)
                    if entry["outcome"] == "closed":
                        down.close()
                        break
            up.write(_frame(kind, body))
            await up.drain()
    except Exception:
        pass
    finally:
        up.close()


async def pg_server(
    reader: asyncio.StreamReader, down: asyncio.StreamWriter, conn: dict
) -> None:
    try:
        while True:
            kind, body = await _message(reader)
            entry = conn.get("entry")
            if kind == b"C" and entry is not None:
                entry["tag"] = body.rstrip(b"\0").decode()
                conn["entry"] = None
                if entry["outcome"] == "reply dropped":
                    conn["dropping"] = True
            if conn.get("dropping"):
                # Past ReadyForQuery the statement has committed.
                if kind == b"Z":
                    break
                continue
            down.write(_frame(kind, body))
            await down.drain()
    except Exception:
        pass
    finally:
        down.close()


async def pg_front(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        length, code = struct.unpack("!ii", await reader.readexactly(8))
        if (length, code) != (8, SSL_REQUEST):
            writer.close()
            return
        writer.write(b"S")
        await writer.drain()
        await writer.start_tls(server_context())
        up_reader, up_writer = await asyncio.open_connection(*PG_UPSTREAM)
    except Exception:
        writer.close()
        return
    conn: dict = {"statements": {}, "entry": None}
    await asyncio.gather(pg_client(reader, up_writer, writer, conn), pg_server(up_reader, writer, conn))


async def pg_without_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.readexactly(8)
        writer.write(b"N")
        await writer.drain()
        await reader.read(1)
    except Exception:
        pass
    writer.close()


# --- control --------------------------------------------------------------------------------


async def control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        path = head.split(b" ")[1].decode()
        if path == "/_ca":
            status, body, kind = 200, (PKI / "ca.pem").read_bytes(), "application/x-pem-file"
        elif path == "/_mail":
            status, kind = 200, "application/json"
            body = json.dumps(
                {"sessions": SESSIONS, "messages": MESSAGES, "deferred": DEFERRED}
            ).encode()
        elif path == "/_s3":
            status, body, kind = 200, json.dumps(S3_LOG).encode(), "application/json"
        elif path == "/_pg":
            status, body, kind = 200, json.dumps(PG_LOG).encode(), "application/json"
        else:
            status, body, kind = 404, b"", "text/plain"
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
    except Exception:
        pass
    writer.close()


async def main() -> None:
    make_pki()
    servers = [
        await asyncio.start_server(front, "0.0.0.0", 443, ssl=server_context()),
        await asyncio.start_server(smtp, "0.0.0.0", 587),
        await asyncio.start_server(control, "0.0.0.0", 80),
        await asyncio.start_server(pg_front, "0.0.0.0", 5432),
        await asyncio.start_server(pg_without_tls, "0.0.0.0", 5433),
    ]
    print("delivery fixture ready", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
