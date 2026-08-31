"""Unit tests for the minimal SOCKS5 client."""

from __future__ import annotations

import socket
import threading

import pytest

from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.socks import socks5_connect


class FakeSocksServer:
    """A single-connection SOCKS5 server that records the CONNECT request."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.reply_code = 0x00
        self.received: bytes | None = None
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None
        self.require_auth = False

    def _handle(self, conn: socket.socket) -> None:
        try:
            greeting = conn.recv(3)
            self.received = greeting
            if self.require_auth:
                conn.sendall(b"\x05\x02")
                return
            conn.sendall(b"\x05\x00")
            request = b""
            while len(request) < 10:
                chunk = conn.recv(10 - len(request))
                if not chunk:
                    return
                request += chunk
            reply = (
                b"\x05"
                + bytes([self.reply_code])
                + b"\x00\x01\x00\x00\x00\x00\x00\x00"
            )
            conn.sendall(reply)
            # Keep the connection alive briefly so the client can return.
            try:
                conn.recv(1024)
            except OSError:
                pass
        except Exception as exc:  # pragma: no cover - test plumbing
            self.error = exc
        finally:
            conn.close()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        conn, _ = self.listener.accept()
        self._handle(conn)

    def close(self) -> None:
        self.listener.close()


def test_socks5_connect_returns_connected_socket() -> None:
    server = FakeSocksServer()
    server.start()
    try:
        sock = socks5_connect("127.0.0.1", server.port, "192.0.2.1", 22, timeout=5)
        try:
            assert server.received == b"\x05\x01\x00"
            assert sock.fileno() >= 0
        finally:
            sock.close()
    finally:
        server.close()


def test_socks5_connect_rejects_proxy_auth_requirement() -> None:
    server = FakeSocksServer()
    server.start()
    try:
        server.require_auth = True
        with pytest.raises(TransportError, match="authentication"):
            socks5_connect("127.0.0.1", server.port, "192.0.2.1", 22, timeout=5)
    finally:
        server.close()


def test_socks5_connect_rejects_non_ip_target() -> None:
    with pytest.raises(TransportError, match="IP addresses"):
        socks5_connect("127.0.0.1", 1080, "not-an-ip", 22, timeout=5)


def test_socks5_connect_wraps_connection_failure() -> None:
    with pytest.raises(TransportError, match="cannot reach target"):
        socks5_connect("127.0.0.1", 1, "192.0.2.1", 22, timeout=1)
