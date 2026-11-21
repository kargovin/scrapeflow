"""The delivery gate's fixture (M.12): a TLS front for a bucket and a webhook receiver, an SMTP sink,
and the CA that signed their certificate.

    s3.deliveryfixture.com     :443  TLS, then MinIO (minio:9000). Three faults, each on the FIRST request
                                     whose object path holds the word:
                                       PUT    "waitput"    closed unanswered (nothing written)
                                       PUT    "lostreply"  written by MinIO, the reply dropped
                                       DELETE "waitundo"   closed unanswered (nothing deleted)
    hooks.deliveryfixture.com  :443  TLS, then the pipeline fixture's receiver (pipeline-fixture:80)
    mail.deliveryfixture.com   :587  SMTP: STARTTLS, then AUTH PLAIN; a password starting "wrong" → 535
    any of the three           :80   GET /_ca  the CA (PEM) · /_mail  sessions and messages · /_s3  requests

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
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PKI = Path("/pki")
SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
NAMES = ["s3.deliveryfixture.com", "hooks.deliveryfixture.com", "mail.deliveryfixture.com"]
UPSTREAMS = {"s3.": ("minio", 9000), "hooks.": ("pipeline-fixture", 80)}
FAULTS = {(b"PUT", b"waitput"), (b"PUT", b"lostreply"), (b"DELETE", b"waitundo")}

SEEN: dict[tuple[bytes, bytes], int] = {}
S3_LOG: list[dict] = []
SESSIONS: list[dict] = []
MESSAGES: list[dict] = []


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_pki() -> None:
    if all((PKI / f).exists() for f in ("ca.pem", "server.pem", "server.key", "bundle.pem")):
        return
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
                session["messages"] += 1
                MESSAGES.append(
                    {
                        "message_id": str(message["Message-ID"]),
                        "subject": str(message["Subject"]),
                        "from": str(message["From"]),
                        "to": str(message["To"]),
                        "user": session["user"],
                        "password_sha256": session["password_sha256"],
                        "session": len(SESSIONS) - 1,
                        "body": (message.get_body(("plain",)) or message).get_content(),
                    }
                )
                print("mail", message["Message-ID"], flush=True)
                send("250 queued")
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


# --- control --------------------------------------------------------------------------------


async def control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        path = head.split(b" ")[1].decode()
        if path == "/_ca":
            status, body, kind = 200, (PKI / "ca.pem").read_bytes(), "application/x-pem-file"
        elif path == "/_mail":
            status, kind = 200, "application/json"
            body = json.dumps({"sessions": SESSIONS, "messages": MESSAGES}).encode()
        elif path == "/_s3":
            status, body, kind = 200, json.dumps(S3_LOG).encode(), "application/json"
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
    ]
    print("delivery fixture ready", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
