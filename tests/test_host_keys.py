"""Tests for local SSH host-key handling."""

from __future__ import annotations

import stat
from pathlib import Path

import paramiko
import pytest

from network_terminal_mcp.errors import TransportError
from network_terminal_mcp.host_keys import HostKeyStore, fingerprint_sha256


def test_strict_unknown_host_never_calls_probe(tmp_path: Path) -> None:
    called = False

    def probe(host: str, port: int, timeout: float) -> paramiko.PKey:
        nonlocal called
        called = True
        raise AssertionError("strict mode must not probe unknown hosts")

    store = HostKeyStore(tmp_path / "known_hosts", probe=probe)
    with pytest.raises(TransportError, match="unknown host key"):
        store.ensure("192.0.2.1", policy="strict")
    assert called is False


def test_accept_new_enrolls_and_strict_reuses_key(tmp_path: Path) -> None:
    key = paramiko.RSAKey.generate(1024)
    calls = 0

    def probe(host: str, port: int, timeout: float) -> paramiko.PKey:
        nonlocal calls
        calls += 1
        assert host == "192.0.2.1"
        assert port == 22
        return key

    path = tmp_path / "state" / "known_hosts"
    store = HostKeyStore(path, probe=probe)
    enrolled = store.ensure("192.0.2.1", policy="accept_new")
    assert enrolled.enrolled is True
    assert enrolled.algorithm == "ssh-rsa"
    assert enrolled.fingerprint == fingerprint_sha256(key)
    assert calls == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    reused = store.ensure("192.0.2.1", policy="strict")
    assert reused.enrolled is False
    assert reused.fingerprint == enrolled.fingerprint
    assert calls == 1


def test_nonstandard_port_uses_bracketed_known_hosts_name(tmp_path: Path) -> None:
    key = paramiko.ECDSAKey.generate(bits=256)
    store = HostKeyStore(tmp_path / "known_hosts", probe=lambda *_: key)
    store.ensure("192.0.2.1", port=2222, policy="accept_new")
    assert store.ensure("192.0.2.1", port=2222, policy="strict").enrolled is False
    with pytest.raises(TransportError, match="unknown host key"):
        store.ensure("192.0.2.1", port=22, policy="strict")


def test_invalid_policy_rejected(tmp_path: Path) -> None:
    store = HostKeyStore(tmp_path / "known_hosts")
    with pytest.raises(TransportError, match="unsupported host key policy"):
        store.ensure("192.0.2.1", policy="replace")
