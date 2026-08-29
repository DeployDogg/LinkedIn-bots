from __future__ import annotations

import asyncio
import errno
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CDP_PROXY_PATH = ROOT / "services" / "LinkedInBrowser" / "scripts" / "cdp_proxy.py"


def import_cdp_proxy():
    sys.modules.pop("cdp_proxy", None)
    spec = importlib.util.spec_from_file_location("cdp_proxy", CDP_PROXY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["cdp_proxy"] = module
    spec.loader.exec_module(module)
    return module


async def read_http(reader: asyncio.StreamReader) -> tuple[list[bytes], bytes]:
    header = await reader.readuntil(b"\r\n\r\n")
    lines = header.removesuffix(b"\r\n\r\n").split(b"\r\n")
    length = 0
    for line in lines:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = await reader.readexactly(length) if length else b""
    return lines, body


class FakeReader:
    def __init__(
        self,
        *,
        read_chunks: list[bytes] | None = None,
        readuntil_chunks: list[bytes] | None = None,
        read_exc: BaseException | None = None,
    ) -> None:
        self.read_chunks = list(read_chunks or [])
        self.readuntil_chunks = list(readuntil_chunks or [])
        self.read_exc = read_exc

    async def read(self, n: int = -1) -> bytes:
        if self.read_exc is not None:
            raise self.read_exc
        if self.read_chunks:
            return self.read_chunks.pop(0)
        return b""

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        if self.readuntil_chunks:
            return self.readuntil_chunks.pop(0)
        return b""


class FakeWriter:
    def __init__(
        self,
        *,
        drain_exc: BaseException | None = None,
        wait_closed_exc: BaseException | None = None,
    ) -> None:
        self.data = b""
        self.closed = False
        self.drain_exc = drain_exc
        self.wait_closed_exc = wait_closed_exc

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        if self.drain_exc is not None:
            raise self.drain_exc

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.wait_closed_exc is not None:
            raise self.wait_closed_exc


class CdpProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.proxy = import_cdp_proxy()

    async def start_server(self, handler):
        active_handlers: set[asyncio.Task] = set()

        async def tracked_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            active_handlers.add(task)
            try:
                await handler(reader, writer)
            finally:
                active_handlers.discard(task)

        server = await asyncio.start_server(tracked_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def cleanup() -> None:
            server.close()
            await server.wait_closed()
            if active_handlers:
                await asyncio.wait_for(
                    asyncio.gather(*list(active_handlers), return_exceptions=True),
                    timeout=2.0,
                )

        self.addAsyncCleanup(cleanup)
        return server, port

    async def test_json_version_rewrites_host_body_content_length_and_single_header_terminator(self) -> None:
        seen_request: dict[str, bytes] = {}

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            seen_request["header"] = header
            body = json.dumps(
                {
                    "Browser": "Chrome/test",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test-id",
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: keep-alive\r\n"
                b"\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        _, upstream_port = await self.start_server(upstream)
        self.proxy.UPSTREAM_HOST = "127.0.0.1"
        self.proxy.UPSTREAM_PORT = upstream_port
        proxy_errors: list[str] = []

        async def proxy_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await self.proxy.handle_client(reader, writer)
            except Exception as exc:  # pragma: no cover - diagnostic assertion path
                proxy_errors.append(repr(exc))
                raise

        _, proxy_port = await self.start_server(proxy_handler)

        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            b"GET /json/version HTTP/1.1\r\n"
            b"Host: linkedin-browser:9222\r\n"
            b"Accept-Encoding: gzip\r\n"
            b"\r\n"
        )
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()

        self.assertEqual([], proxy_errors)
        self.assertIn(f"Host: 127.0.0.1:{upstream_port}".encode(), seen_request["header"])
        self.assertIn(b"Accept-Encoding: identity", seen_request["header"])
        self.assertIn(b"Connection: close", seen_request["header"])
        self.assertEqual(1, raw.count(b"\r\n\r\n"), raw)
        header, body = raw.split(b"\r\n\r\n", 1)
        lines = header.split(b"\r\n")
        content_length = [line for line in lines if line.lower().startswith(b"content-length:")][0]
        self.assertEqual(int(content_length.split(b":", 1)[1].strip()), len(body))
        payload = json.loads(body)
        self.assertEqual("ws://linkedin-browser:9222/devtools/browser/test-id", payload["webSocketDebuggerUrl"])
        self.assertNotIn(b"transfer-encoding", raw.lower())

    async def test_websocket_upgrade_handshake_and_bytes_pass_through(self) -> None:
        seen_header: dict[str, bytes] = {}

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readuntil(b"\r\n\r\n")
            seen_header["header"] = header
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            )
            await writer.drain()
            data = await reader.readexactly(4)
            writer.write(data[::-1])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        _, upstream_port = await self.start_server(upstream)
        self.proxy.UPSTREAM_HOST = "127.0.0.1"
        self.proxy.UPSTREAM_PORT = upstream_port
        _, proxy_port = await self.start_server(self.proxy.handle_client)

        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            b"GET /devtools/browser/test HTTP/1.1\r\n"
            b"Host: linkedin-browser:9222\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"\r\n"
        )
        await writer.drain()
        handshake = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"abcd")
        await writer.drain()
        echoed = await reader.readexactly(4)
        writer.close()
        await writer.wait_closed()

        self.assertIn(b"101 Switching Protocols", handshake)
        self.assertIn(f"Host: 127.0.0.1:{upstream_port}".encode(), seen_header["header"])
        self.assertEqual(b"dcba", echoed)

    async def test_pipe_treats_expected_disconnects_as_clean_shutdown(self) -> None:
        expected_errors = [
            ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
            BrokenPipeError(errno.EPIPE, "Broken pipe"),
            ConnectionAbortedError(errno.ECONNABORTED, "Software caused connection abort"),
            OSError(errno.ECONNRESET, "Connection reset by peer"),
            OSError(errno.EPIPE, "Broken pipe"),
            OSError(errno.ECONNABORTED, "Software caused connection abort"),
        ]

        for exc in expected_errors:
            with self.subTest(exc=repr(exc)):
                reader = FakeReader(read_exc=exc)
                writer = FakeWriter()
                await self.proxy.pipe(reader, writer)
                self.assertTrue(writer.closed)

        for exc in expected_errors:
            with self.subTest(drain_exc=repr(exc)):
                reader = FakeReader(read_chunks=[b"payload", b""])
                writer = FakeWriter(drain_exc=exc)
                await self.proxy.pipe(reader, writer)
                self.assertTrue(writer.closed)

        for exc in expected_errors:
            with self.subTest(wait_closed_exc=repr(exc)):
                reader = FakeReader(read_chunks=[b""])
                writer = FakeWriter(wait_closed_exc=exc)
                await self.proxy.pipe(reader, writer)
                self.assertTrue(writer.closed)

    async def test_pipe_does_not_swallow_unrelated_errors(self) -> None:
        for exc in [RuntimeError("bug"), OSError(errno.EINVAL, "not a disconnect")]:
            with self.subTest(exc=repr(exc)):
                reader = FakeReader(read_exc=exc)
                writer = FakeWriter()
                with self.assertRaises(type(exc)):
                    await self.proxy.pipe(reader, writer)
                self.assertTrue(writer.closed)

    async def test_websocket_client_disconnect_does_not_escape_handle_client(self) -> None:
        client_reader = FakeReader(
            readuntil_chunks=[
                b"GET /devtools/browser/test HTTP/1.1\r\n"
                b"Host: linkedin-browser:9222\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            ],
            read_exc=ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        )
        client_writer = FakeWriter()
        upstream_reader = FakeReader(
            readuntil_chunks=[
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            ],
            read_exc=ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        )
        upstream_writer = FakeWriter(wait_closed_exc=BrokenPipeError(errno.EPIPE, "Broken pipe"))

        async def fake_open_connection(host: str, port: int):
            return upstream_reader, upstream_writer

        original_open_connection = asyncio.open_connection
        asyncio.open_connection = fake_open_connection
        try:
            await self.proxy.handle_client(client_reader, client_writer)
        finally:
            asyncio.open_connection = original_open_connection

        self.assertTrue(client_writer.closed)
        self.assertTrue(upstream_writer.closed)

    async def test_websocket_unrelated_proxy_error_still_escapes_handle_client(self) -> None:
        client_reader = FakeReader(
            readuntil_chunks=[
                b"GET /devtools/browser/test HTTP/1.1\r\n"
                b"Host: linkedin-browser:9222\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            ],
            read_exc=RuntimeError("proxy programming bug"),
        )
        client_writer = FakeWriter()
        upstream_reader = FakeReader(
            readuntil_chunks=[
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            ],
            read_chunks=[b""],
        )
        upstream_writer = FakeWriter()

        async def fake_open_connection(host: str, port: int):
            return upstream_reader, upstream_writer

        original_open_connection = asyncio.open_connection
        asyncio.open_connection = fake_open_connection
        try:
            with self.assertRaisesRegex(RuntimeError, "proxy programming bug"):
                await self.proxy.handle_client(client_reader, client_writer)
        finally:
            asyncio.open_connection = original_open_connection

        self.assertTrue(client_writer.closed)
        self.assertTrue(upstream_writer.closed)

    def test_proxy_source_does_not_log_or_reference_cookies(self) -> None:
        text = CDP_PROXY_PATH.read_text(encoding="utf-8")
        self.assertNotIn("cookie", text.lower())
        self.assertNotIn("print(", text)
        self.assertNotIn("logging", text)


if __name__ == "__main__":
    unittest.main()

