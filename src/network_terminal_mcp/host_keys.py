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
    changed: bool = False
    previous_fingerprint: str | None = None


def fingerprint_sha256(key: PKey) -> str:
    """Return an OpenSSH-style SHA-256 fingerprint for a public host key."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return f"SHA256:{base64.b64encode(digest).decode().rstrip('=')}"


def _known_host_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def probe_host_key(host: str, port: int, timeout: float) -> PKey:
    """Perform an unauthenticated SSH handshake and return the public key."""
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        return probe_host_key_socket(sock, timeout)
    except OSError as exc:
        raise TransportError(f"cannot retrieve host key from {host}:{port}: {exc}") from exc
    finally:
        if sock is not None:
            sock.close()


def probe_host_key_socket(sock: object, timeout: float) -> PKey:
    """Perform an SSH handshake over an already-connected socket-like object."""
    transport: paramiko.Transport | None = None
    try:
        transport = paramiko.Transport(sock)  # type: ignore[arg-type]
        transport.banner_timeout = timeout
        transport.auth_timeout = timeout
        transport.start_client(timeout=timeout)
        return transport.get_remote_server_key()
    except (OSError, paramiko.SSHException) as exc:
        raise TransportError(f"cannot retrieve host key: {exc}") from exc
    finally:
        if transport is not None:
            transport.close()


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
        probe: HostKeyProbe | None = None,
    ) -> HostKeyStatus:
        """Verify known host presence or explicitly add a TOFU key.

        ``strict`` never contacts an unknown host and reports the missing key.
        ``accept_new`` retrieves and writes a key only when the host has no
        existing entry. Existing keys are never replaced.

        ``accept_changed`` is a weak-trust, per-profile opt-in for platforms
        known to rotate host keys on every boot (e.g. some SNR). It always
        probes the live key, keeps an unchanged key untouched, and replaces a
        changed or missing key. The change is returned so callers can audit and
        warn. Never use it for devices with stable keys.
        """
        if policy not in {"strict", "accept_new", "accept_changed"}:
            raise TransportError(f"unsupported host key policy {policy!r}")
        host_name = _known_host_name(host, port)
        key_probe = probe or self._probe
        with self._lock:
            keys = self._load()
            known = keys.lookup(host_name)
            if known:
                key_type, key = next(iter(known.items()))
                if policy == "strict":
                    return HostKeyStatus(
                        host=host,
                        port=port,
                        algorithm=key_type,
                        fingerprint=fingerprint_sha256(key),
                        enrolled=False,
                    )
                if policy == "accept_changed":
                    live_key = key_probe(host, port, timeout)
                    live_fingerprint = fingerprint_sha256(live_key)
                    if key.asbytes() == live_key.asbytes():
                        return HostKeyStatus(
                            host=host,
                            port=port,
                            algorithm=live_key.get_name(),
                            fingerprint=live_fingerprint,
                            enrolled=False,
                        )
                    previous = fingerprint_sha256(key)
                    keys.add(host_name, live_key.get_name(), live_key)
                    self._save(keys)
                    return HostKeyStatus(
                        host=host,
                        port=port,
                        algorithm=live_key.get_name(),
                        fingerprint=live_fingerprint,
                        enrolled=True,
                        changed=True,
                        previous_fingerprint=previous,
                    )
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

            key = key_probe(host, port, timeout)
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
