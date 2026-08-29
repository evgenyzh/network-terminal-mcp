"""Local SSH host-key storage with explicit trust-on-first-use support."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import paramiko
from paramiko.pkey import PKey

from network_terminal_mcp.errors import TransportError

HostKeyProbe = Callable[[str, int, float], PKey]


@dataclass(frozen=True)
class HostKeyStatus:
    """A host key found in or added to the local known-hosts file."""

    host: str
    port: int
    algorithm: str
    fingerprint: str
    enrolled: bool


def fingerprint_sha256(key: PKey) -> str:
    """Return an OpenSSH-style SHA-256 fingerprint for a public host key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return f"SHA256:{base64.b64encode(digest).decode().rstrip('=')}"


def _known_host_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def probe_host_key(host: str, port: int, timeout: float) -> PKey:
    """Perform an unauthenticated SSH handshake and return the public key."""
    sock: socket.socket | None = None
    transport: paramiko.Transport | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.banner_timeout = timeout
        transport.auth_timeout = timeout
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    except (OSError, paramiko.SSHException) as exc:
        raise TransportError(f"cannot retrieve host key from {host}:{port}: {exc}") from exc
    finally:
        if transport is not None:
            transport.close()
        elif sock is not None:
            sock.close()


class HostKeyStore:
    """Manage a dedicated known-hosts file owned by network-terminal-mcp."""

    def __init__(self, path: Path, *, probe: HostKeyProbe = probe_host_key) -> None:
        self._path = path
        self._probe = probe
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def ensure(
        self,
        host: str,
        *,
        port: int = 22,
        policy: str = "strict",
        timeout: float = 10.0,
    ) -> HostKeyStatus:
        """Verify known host presence or explicitly add a TOFU key.

        ``strict`` never contacts an unknown host and reports the missing key.
        ``accept_new`` retrieves and writes a key only when the host has no
        existing entry. Existing keys are never replaced.
        """
        if policy not in {"strict", "accept_new"}:
            raise TransportError(f"unsupported host key policy {policy!r}")
        host_name = _known_host_name(host, port)
        with self._lock:
            keys = self._load()
            known = keys.lookup(host_name)
            if known:
                key_type, key = next(iter(known.items()))
                return HostKeyStatus(
                    host=host,
                    port=port,
                    algorithm=key_type,
                    fingerprint=fingerprint_sha256(key),
                    enrolled=False,
                )
            if policy == "strict":
                raise TransportError(
                    f"unknown host key for {host_name}; enroll it explicitly "
                    "with host_key_policy: accept_new"
                )

            key = self._probe(host, port, timeout)
            keys.add(host_name, key.get_name(), key)
            self._save(keys)
            return HostKeyStatus(
                host=host,
                port=port,
                algorithm=key.get_name(),
                fingerprint=fingerprint_sha256(key),
                enrolled=True,
            )

    def _load(self) -> paramiko.HostKeys:
        keys = paramiko.HostKeys()
        if not self._path.exists():
            return keys
        try:
            keys.load(str(self._path))
        except (OSError, paramiko.SSHException) as exc:
            raise TransportError(f"cannot load known hosts {self._path}: {exc}") from exc
        return keys

    def _save(self, keys: paramiko.HostKeys) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self._path.parent, 0o700)
            keys.save(str(self._path))
            os.chmod(self._path, 0o600)
        except OSError as exc:
            raise TransportError(f"cannot save known hosts {self._path}: {exc}") from exc
