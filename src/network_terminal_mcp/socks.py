"""Minimal SOCKS5 client used as the last mile to a target.

The network-terminal-mcp transport reaches lab devices either directly, through
an SSH bastion, or through a local SOCKS5 proxy (for example an SSH dynamic
forward ``ssh -D``). This module implements only the parts of RFC 1928 that we
need to open a CONNECT tunnel: no auth, no UDP, no domain-name resolution on
the proxy side (targets are always resolved by the caller and sent as IPs).

The returned socket has a socket-like interface, so it can be handed to Netmiko
as the ``sock`` argument and to paramiko for an unauthenticated host-key probe.
"""

from __future__ import annotations

import socket
import struct
from ipaddress import ip_address

from network_terminal_mcp.errors import TransportError

SOCKS_VERSION = 0x05
REP_SUCCEEDED = 0x00
ATYP_IPV4 = 0x01
ATYP_IPV6 = 0x04


def _socks5_connect(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    timeout: float,
) -> socket.socket:
    """Open a SOCKS5 CONNECT tunnel to the target through the given proxy."""
    try:
        addr = ip_address(target_host)
    except ValueError as exc:
        raise TransportError(
            f"SOCKS targets must be IP addresses, got {target_host!r}"
        ) from exc

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        version, method = _recv_exact(sock, 2, "SOCKS greeting")
        if version != SOCKS_VERSION:
            raise TransportError(f"unsupported SOCKS version {version}")
        if method != 0x00:
            raise TransportError("SOCKS proxy requires authentication (not supported)")

        if addr.version == 4:
            atyp = ATYP_IPV4
            host_bytes = addr.packed
        else:
            atyp = ATYP_IPV6
            host_bytes = addr.packed
        request = (
            struct.pack("!BBB", SOCKS_VERSION, 0x01, 0x00)
            + struct.pack("!B", atyp)
            + host_bytes
            + struct.pack("!H", target_port)
        )
        sock.sendall(request)

        header = _recv_exact(sock, 4, "SOCKS CONNECT reply")
        version, reply, _, atyp = header
        if version != SOCKS_VERSION:
            raise TransportError(f"unsupported SOCKS reply version {version}")
        if reply != REP_SUCCEEDED:
            raise TransportError(f"SOCKS CONNECT failed with reply code {reply}")
        if atyp == ATYP_IPV4:
            _recv_exact(sock, 4 + 2, "SOCKS bind address")
        elif atyp == ATYP_IPV6:
            _recv_exact(sock, 16 + 2, "SOCKS bind address")
        else:
            raise TransportError(f"unsupported SOCKS bind address type {atyp}")
        return sock
    except Exception:
        sock.close()
        raise


def _recv_exact(sock: socket.socket, n: int, what: str) -> bytes:
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise TransportError(f"connection closed while reading {what}")
        data.extend(chunk)
    return bytes(data)


def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    timeout: float,
) -> socket.socket:
    """Open a SOCKS5 CONNECT tunnel; wraps socket errors as TransportError."""
    try:
        return _socks5_connect(proxy_host, proxy_port, target_host, target_port, timeout)
    except OSError as exc:
        raise TransportError(
            f"cannot reach target {target_host}:{target_port} via SOCKS "
            f"{proxy_host}:{proxy_port}: {exc}"
        ) from exc
