"""The start of a Postgres server: answers the TLS request (yes, with a certificate for a chosen name, or
no), then answers the login with an error of a chosen SQLSTATE. Records what arrives."""

import asyncio
import ssl
import struct
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tests.tls import make_pki

SSL_REQUEST = 80877103


@dataclass
class Pg:
    port: int
    ca_pem: str
    sni: list[str | None] = field(default_factory=list)
    connections: int = 0
    # Startup messages read after TLS: the login reached the server.
    logins: int = 0


def _error(sqlstate: str) -> bytes:
    fields = b"SFATAL\0VFATAL\0C" + sqlstate.encode() + b"\0Mscripted\0\0"
    return b"E" + struct.pack("!i", len(fields) + 4) + fields


@asynccontextmanager
async def pg_server(
    name: str = "db.example.com",
    *,
    tls: bool = True,
    sqlstate: str = "28P01",
    bind: str = "127.0.0.1",
    port: int = 0,
):
    ca_pem, cert_pem, key_pem = make_pki(name)
    with tempfile.TemporaryDirectory() as tmp:
        cert_file, key_file = Path(tmp, "cert.pem"), Path(tmp, "key.pem")
        cert_file.write_bytes(cert_pem)
        key_file.write_bytes(key_pem)
        server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_ctx.load_cert_chain(cert_file, key_file)
    pg = Pg(port=0, ca_pem=ca_pem.decode())
    server_ctx.sni_callback = lambda sock, sni, ctx: pg.sni.append(sni)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pg.connections += 1
        try:
            length, code = struct.unpack("!ii", await reader.readexactly(8))
            assert (length, code) == (8, SSL_REQUEST)
            writer.write(b"S" if tls else b"N")
            await writer.drain()
            if not tls:
                await reader.read(1)
                return
            await writer.start_tls(server_ctx)
            (size,) = struct.unpack("!i", await reader.readexactly(4))
            await reader.readexactly(size - 4)
            pg.logins += 1
            writer.write(_error(sqlstate))
            await writer.drain()
            await reader.read(1)
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, bind, port)
    pg.port = server.sockets[0].getsockname()[1]
    async with server:
        yield pg
