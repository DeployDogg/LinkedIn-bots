#!/usr/bin/env python3
"""Tiny CDP proxy for Docker peers.

Chromium in Debian can listen on 127.0.0.1 for DevTools and rejects non-local
Host headers. This proxy listens only on the container IP and forwards to
Chromium on loopback while rewriting Host and /json/version websocket URLs.
It never inspects or logs page/session data.
"""

from __future__ import annotations

import asyncio
import errno
import os

LISTEN_HOST = os.environ.get("CDP_PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("CDP_PROXY_LISTEN_PORT", "9222"))
UPSTREAM_HOST = os.environ.get("CDP_PROXY_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("CDP_PROXY_UPSTREAM_PORT", "9222"))
BUFFER_SIZE = 65536
HEADER_TERMINATOR = b"\r\n\r\n"
EXPECTED_DISCONNECT_ERRNOS = {
    errno.ECONNRESET,
    errno.EPIPE,
    errno.ECONNABORTED,
}


def is_expected_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in EXPECTED_DISCONNECT_ERRNOS


async def guarded_close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await writer.wait_closed()
    except Exception as exc:
        if not is_expected_disconnect(exc):
            raise


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            try:
                data = await reader.read(BUFFER_SIZE)
            except Exception as exc:
                if is_expected_disconnect(exc):
                    break
                raise
            if not data:
                break
            writer.write(data)
            try:
                await writer.drain()
            except Exception as exc:
                if is_expected_disconnect(exc):
                    break
                raise
    finally:
        await guarded_close(writer)


async def websocket_proxy(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    tasks = [
        asyncio.create_task(pipe(client_reader, upstream_writer)),
        asyncio.create_task(pipe(upstream_reader, client_writer)),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not is_expected_disconnect(result):
            raise result


def split_header_block(header_block: bytes) -> list[bytes]:
    return header_block.removesuffix(HEADER_TERMINATOR).split(b"\r\n")


def rewrite_request_header(header_block: bytes) -> tuple[bytes, bytes, bool]:
    lines = split_header_block(header_block)
    original_host = b"linkedin-browser:9222"
    upgrade = False
    saw_host = False
    saw_accept_encoding = False
    saw_connection = False
    rewritten: list[bytes] = []

    for line in lines:
        lower = line.lower()
        if lower.startswith(b"host:"):
            saw_host = True
            original_host = line.split(b":", 1)[1].strip() or original_host
            rewritten.append(f"Host: {UPSTREAM_HOST}:{UPSTREAM_PORT}".encode("ascii"))
        elif lower.startswith(b"accept-encoding:"):
            saw_accept_encoding = True
            rewritten.append(b"Accept-Encoding: identity")
        elif lower.startswith(b"connection:"):
            saw_connection = True
            if b"upgrade" in lower:
                upgrade = True
                rewritten.append(line)
            else:
                rewritten.append(b"Connection: close")
        else:
            if lower.startswith(b"upgrade:") and b"websocket" in lower:
                upgrade = True
            rewritten.append(line)

    if not saw_host:
        rewritten.append(f"Host: {UPSTREAM_HOST}:{UPSTREAM_PORT}".encode("ascii"))
    if not upgrade and not saw_accept_encoding:
        rewritten.append(b"Accept-Encoding: identity")
    if not upgrade and not saw_connection:
        rewritten.append(b"Connection: close")

    return b"\r\n".join(rewritten) + HEADER_TERMINATOR, original_host, upgrade


def parse_content_length(headers: list[bytes]) -> int | None:
    for line in headers[1:]:
        if line.lower().startswith(b"content-length:"):
            return int(line.split(b":", 1)[1].strip())
    return None


async def read_http_response(reader: asyncio.StreamReader) -> tuple[list[bytes], bytes]:
    header_block = await reader.readuntil(HEADER_TERMINATOR)
    headers = split_header_block(header_block)
    content_length = parse_content_length(headers)
    if content_length is None:
        body = await reader.read()
    else:
        body = await reader.readexactly(content_length)
    return headers, body


def rewrite_response(headers: list[bytes], body: bytes, original_host: bytes) -> bytes:
    for upstream_ws in {
        f"ws://{UPSTREAM_HOST}:{UPSTREAM_PORT}".encode("ascii"),
        b"ws://127.0.0.1:9222",
        b"ws://localhost:9222",
    }:
        body = body.replace(upstream_ws, b"ws://" + original_host)

    out = [headers[0]]
    for line in headers[1:]:
        lower = line.lower()
        if lower.startswith((b"content-length:", b"transfer-encoding:", b"connection:", b"content-encoding:")):
            continue
        out.append(line)
    out.append(b"Content-Length: " + str(len(body)).encode("ascii"))
    out.append(b"Connection: close")
    return b"\r\n".join(out) + HEADER_TERMINATOR + body


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        header_block = await client_reader.readuntil(HEADER_TERMINATOR)
        rewritten_request, original_host, upgrade = rewrite_request_header(header_block)
        upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
        upstream_writer.write(rewritten_request)
        await upstream_writer.drain()

        if upgrade:
            handshake = await upstream_reader.readuntil(HEADER_TERMINATOR)
            client_writer.write(handshake)
            await client_writer.drain()
            await websocket_proxy(client_reader, client_writer, upstream_reader, upstream_writer)
            return

        headers, body = await read_http_response(upstream_reader)
        client_writer.write(rewrite_response(headers, body, original_host))
        await client_writer.drain()
    finally:
        await guarded_close(client_writer)
        if upstream_writer is not None:
            await guarded_close(upstream_writer)


async def main() -> None:
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
