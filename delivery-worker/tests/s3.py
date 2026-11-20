"""A local https S3 stand-in: records each request, stores PUTs, and plays scripted answers."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiohttp import web

from tests.tls import contexts


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Play:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


def error_xml(code: str, message: str = "a message from the server") -> bytes:
    return (
        f"<?xml version='1.0' encoding='UTF-8'?><Error><Code>{code}</Code>"
        f"<Message>{message}</Message></Error>"
    ).encode()


@dataclass
class S3:
    port: int
    client_context: object
    sni: list
    requests: list[Request] = field(default_factory=list)
    objects: dict[str, bytes] = field(default_factory=dict)
    # Answers to play before the default behaviour, per method.
    plays: dict[str, list[Play]] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"https://s3.test:{self.port}/"


@asynccontextmanager
async def s3_server(certificate_name: str = "s3.test"):
    server_ctx, client_ctx, seen = contexts(certificate_name)
    state: S3 | None = None

    async def handle(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        state.requests.append(
            Request(request.method, request.raw_path, dict(request.headers), body)
        )
        queue = state.plays.get(request.method) or []
        if queue:
            play = queue.pop(0)
            if play.delay:
                await asyncio.sleep(play.delay)
            return web.Response(status=play.status, body=play.body, headers=play.headers)
        if request.method == "PUT":
            state.objects[request.raw_path] = body
            return web.Response(status=200)
        if request.method == "DELETE":
            state.objects.pop(request.raw_path, None)
            return web.Response(status=204)
        return web.Response(status=405)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state = S3(port=port, client_context=client_ctx, sni=seen)
    try:
        yield state
    finally:
        await runner.cleanup()
