"""Small HTTPS CONNECT proxy for sandbox allowlisted egress."""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from typing import cast

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_HEADER_BYTES = 8192


def dns_nxdomain_response(query: bytes) -> bytes:
    """Return NXDOMAIN without forwarding sandbox DNS requests anywhere."""
    if len(query) < 12:
        return b""
    question_count = int.from_bytes(query[4:6], "big")
    offset = 12
    if question_count != 1:
        question_count = 0
        question = b""
    else:
        try:
            while True:
                label_length = query[offset]
                offset += 1
                if label_length == 0:
                    break
                if label_length & 0xC0 or label_length > 63:
                    raise ValueError("Compressed or invalid DNS label")
                offset += label_length
            offset += 4  # QTYPE and QCLASS
            if offset > len(query):
                raise ValueError("Truncated DNS question")
            question = query[12:offset]
        except (IndexError, ValueError):
            question_count = 0
            question = b""
    response_flags = 0x8000 | (int.from_bytes(query[2:4], "big") & 0x0100) | 0x0003
    header = (
        query[:2]
        + response_flags.to_bytes(2, "big")  # response, query RD flag, NXDOMAIN
        + question_count.to_bytes(2, "big")
        + b"\x00\x00\x00\x00\x00\x00"
    )
    return header + question


class _DNSDenyProtocol(asyncio.DatagramProtocol):
    transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        response = dns_nxdomain_response(data)
        if response and self.transport is not None:
            self.transport.sendto(response, addr)


async def _deny_dns_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while prefix := await reader.readexactly(2):
            size = int.from_bytes(prefix, "big")
            if size > 4096:
                return
            query = await reader.readexactly(size)
            response = dns_nxdomain_response(query)
            writer.write(len(response).to_bytes(2, "big") + response)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


def normalize_allowlist(hosts: tuple[str, ...]) -> frozenset[str]:
    """Validate exact DNS names; wildcards, IP literals, and URLs are not allowed."""
    normalized: set[str] = set()
    for raw_host in hosts:
        host = raw_host.rstrip(".").lower()
        labels = host.split(".")
        if (
            not host
            or len(host) > 253
            or len(labels) < 2
            or any(not _HOST_LABEL.fullmatch(label) for label in labels)
        ):
            raise ValueError(f"Unsafe egress allowlist hostname: {raw_host!r}")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("Egress allowlist entries must be DNS hostnames, not IP addresses.")
        normalized.add(host)
    if not normalized:
        raise ValueError("Allowlisted egress requires at least one approved hostname.")
    return frozenset(normalized)


def host_is_allowed(host: str, allowlist: frozenset[str]) -> bool:
    candidate = host.rstrip(".").lower()
    return candidate in allowlist


async def _resolve_public(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = sorted({record[4][0] for record in records})
    if not addresses:
        raise OSError("Target hostname did not resolve.")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise PermissionError("Target hostname resolves to a non-public address.")
    return addresses


async def handle_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    allowlist: frozenset[str],
    *,
    resolve: Callable[[str], Awaitable[list[str]]] = _resolve_public,
    open_connection: Callable[
        ..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    ] = asyncio.open_connection,
) -> None:
    """Serve one CONNECT request, denying non-HTTPS and non-allowlisted targets."""
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        if len(request) > _MAX_HEADER_BYTES:
            raise ValueError("Request headers exceed limit.")
        first_line = request.split(b"\r\n", 1)[0].decode("ascii")
        method, authority, version = first_line.split(" ")
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise PermissionError("Only HTTPS CONNECT requests are supported.")
        host, sep, port_text = authority.rpartition(":")
        if not sep or not host or port_text != "443" or not host_is_allowed(host, allowlist):
            raise PermissionError("Destination is not on the sandbox egress allowlist.")
        if any(char in host for char in "/\\@[]"):
            raise PermissionError("Invalid CONNECT hostname.")
        addresses = await resolve(host)
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise PermissionError("Target hostname resolves to a non-public address.")
        # Connect to the checked IP, not the hostname, to prevent DNS rebinding.
        upstream_reader, upstream_writer = await open_connection(addresses[0], 443)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await _relay(reader, writer, upstream_reader, upstream_writer)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError):
        await _deny(writer, b"400 Bad Request")
    except (PermissionError, OSError, UnicodeError):
        await _deny(writer, b"403 Forbidden")
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            await upstream_writer.wait_closed()
        writer.close()
        await writer.wait_closed()


async def _deny(writer: asyncio.StreamWriter, status: bytes) -> None:
    writer.write(b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n\r\n")
    await writer.drain()


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        while chunk := await source.read(65536):
            target.write(chunk)
            await target.drain()

    tasks = [
        asyncio.create_task(pipe(client_reader, upstream_writer)),
        asyncio.create_task(pipe(upstream_reader, client_writer)),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


async def serve() -> None:
    import json
    import os

    hosts = tuple(json.loads(os.environ.get("FORGE_EGRESS_ALLOWLIST", "[]")))
    allowlist = normalize_allowlist(hosts)
    dns_server = await asyncio.get_running_loop().create_datagram_endpoint(
        _DNSDenyProtocol, local_addr=("0.0.0.0", 53)
    )
    dns_tcp = await asyncio.start_server(_deny_dns_tcp, "0.0.0.0", 53)
    server = await asyncio.start_server(
        lambda reader, writer: handle_connect(reader, writer, allowlist), "0.0.0.0", 3128
    )
    async with server:
        async with dns_tcp:
            try:
                await server.serve_forever()
            finally:
                dns_server[0].close()


if __name__ == "__main__":
    asyncio.run(serve())
