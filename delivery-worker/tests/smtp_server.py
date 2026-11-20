"""A scripted local SMTP server: STARTTLS or implicit TLS with a certificate for a chosen name,
AUTH PLAIN offered only over TLS, replies overridable per command. Records what arrives."""

import asyncio
import base64
import ssl
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tests.tls import make_pki

USER, PASSWORD = "mailer", "pa55word"


@dataclass
class Smtp:
    port: int
    ca_pem: str
    # Every command line as received, with whether it arrived over TLS.
    commands: list[tuple[str, bool]] = field(default_factory=list)
    messages: list[bytes] = field(default_factory=list)
    sni: list[str | None] = field(default_factory=list)
    connections: int = 0

    def verbs(self) -> list[str]:
        return [line.split(" ", 1)[0].split(":", 1)[0].upper() for line, _ in self.commands]


@asynccontextmanager
async def smtp_server(
    name: str = "mail.example.com",
    *,
    mode: str = "starttls",
    offer_starttls: bool = True,
    offer_auth: bool = True,
    replies: dict[str, tuple[int, str]] | None = None,
    hang_after: str | None = None,
    refuse_rcpt: dict[str, int] | None = None,
    close_delay: float = 0,
):
    """Yields an ``Smtp`` record. ``replies`` overrides a verb's answer ("BANNER" for the greeting);
    ``hang_after`` stops answering once that verb arrives; ``refuse_rcpt`` answers one recipient
    with a code; ``close_delay`` holds the connection open that long after QUIT's reply."""
    ca_pem, cert_pem, key_pem = make_pki(name)
    with tempfile.TemporaryDirectory() as tmp:
        cert_file, key_file = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
        cert_file.write_bytes(cert_pem)
        key_file.write_bytes(key_pem)
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ctx.load_cert_chain(cert_file, key_file)
    replies = replies or {}
    record = Smtp(port=0, ca_pem=ca_pem.decode())
    server_ctx.sni_callback = lambda sock, sni, ctx: record.sni.append(sni)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        record.connections += 1
        tls = mode == "implicit"

        async def send(code: int, text: str) -> None:
            writer.write(f"{code} {text}\r\n".encode())
            await writer.drain()

        try:
            await send(*replies.get("BANNER", (220, f"{name} ready")))
            while line := await reader.readline():
                text = line.decode().rstrip("\r\n")
                record.commands.append((text, tls))
                verb = text.split(" ", 1)[0].split(":", 1)[0].upper()
                if hang_after == verb:
                    await asyncio.sleep(3600)
                if verb in replies:
                    await send(*replies[verb])
                    if verb == "QUIT" or replies[verb][0] == 421:
                        break
                    continue
                if verb in ("EHLO", "HELO"):
                    lines = [name]
                    if mode == "starttls" and offer_starttls and not tls:
                        lines.append("STARTTLS")
                    if offer_auth and tls:
                        lines.append("AUTH PLAIN")
                    lines.append("8BITMIME")
                    for i, ext in enumerate(lines):
                        sep = " " if i == len(lines) - 1 else "-"
                        writer.write(f"250{sep}{ext}\r\n".encode())
                    await writer.drain()
                elif verb == "STARTTLS":
                    await send(220, "go ahead")
                    await writer.start_tls(server_ctx)
                    tls = True
                elif verb == "AUTH":
                    parts = text.split()
                    token = parts[2] if len(parts) > 2 else ""
                    if not token:
                        await send(334, "")
                        token = (await reader.readline()).decode().strip()
                    _, user, password = base64.b64decode(token).decode().split("\0")
                    if (user, password) == (USER, PASSWORD):
                        await send(235, "ok")
                    else:
                        await send(535, "authentication failed for " + user)
                elif verb == "RCPT" and (refused := next(
                    (c for a, c in (refuse_rcpt or {}).items() if f"<{a}>" in text), None
                )):
                    await send(refused, "recipient refused")
                elif verb in ("MAIL", "RCPT", "RSET", "NOOP"):
                    await send(250, "ok")
                elif verb == "DATA":
                    await send(354, "go on")
                    data = bytearray()
                    while (chunk := await reader.readline()) not in (b".\r\n", b""):
                        data += chunk[1:] if chunk.startswith(b"..") else chunk
                    record.messages.append(bytes(data))
                    await send(250, "queued")
                elif verb == "QUIT":
                    await send(221, "bye")
                    await asyncio.sleep(close_delay)
                    break
                else:
                    await send(502, "unknown")
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(
        handle, "127.0.0.1", 0, ssl=server_ctx if mode == "implicit" else None
    )
    record.port = server.sockets[0].getsockname()[1]
    async with server:
        yield record
