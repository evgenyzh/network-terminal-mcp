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


def test_accept_new_reuses_an_existing_key_without_probing(tmp_path: Path) -> None:
    key = paramiko.RSAKey.generate(1024)
    calls = 0

    def probe(host: str, port: int, timeout: float) -> paramiko.PKey:
        nonlocal calls
        calls += 1
        return key

    store = HostKeyStore(tmp_path / "known_hosts", probe=probe)
    enrolled = store.ensure("192.0.2.1", policy="accept_new")
    reused = store.ensure("192.0.2.1", policy="accept_new")

    assert enrolled.enrolled is True
    assert reused.enrolled is False
    assert reused.fingerprint == enrolled.fingerprint
    assert calls == 1


def test_per_call_probe_overrides_the_default_probe(tmp_path: Path) -> None:
    default_key = paramiko.RSAKey.generate(1024)
    forwarded_key = paramiko.RSAKey.generate(1024)
    default_calls = 0
    forwarded_calls = 0

    def default_probe(host: str, port: int, timeout: float) -> paramiko.PKey:
        nonlocal default_calls
        default_calls += 1
        return default_key

    def forwarded_probe(host: str, port: int, timeout: float) -> paramiko.PKey:
        nonlocal forwarded_calls
        forwarded_calls += 1
        return forwarded_key

    store = HostKeyStore(tmp_path / "known_hosts", probe=default_probe)
    status = store.ensure("192.0.2.1", policy="accept_new", probe=forwarded_probe)

    assert status.fingerprint == fingerprint_sha256(forwarded_key)
    assert default_calls == 0
    assert forwarded_calls == 1


def test_accept_changed_enrolls_unknown_host(tmp_path: Path) -> None:
    key = paramiko.RSAKey.generate(1024)
    store = HostKeyStore(tmp_path / "known_hosts", probe=lambda *_: key)
    status = store.ensure("192.0.2.1", policy="accept_changed")
    assert status.enrolled is True
    assert status.changed is False
    assert status.previous_fingerprint is None


def test_accept_changed_keeps_unchanged_key(tmp_path: Path) -> None:
    key = paramiko.RSAKey.generate(1024)
    store = HostKeyStore(tmp_path / "known_hosts", probe=lambda *_: key)
    store.ensure("192.0.2.1", policy="accept_new")
    before = (tmp_path / "known_hosts").read_text()
    status = store.ensure("192.0.2.1", policy="accept_changed")
    assert status.enrolled is False
    assert status.changed is False
    assert (tmp_path / "known_hosts").read_text() == before


def test_accept_changed_replaces_a_changed_key(tmp_path: Path) -> None:
    first = paramiko.RSAKey.generate(1024)
    second = paramiko.RSAKey.generate(1024)
    from network_terminal_mcp.host_keys import fingerprint_sha256

    store = HostKeyStore(tmp_path / "known_hosts", probe=lambda *_: first)
    store.ensure("192.0.2.1", policy="accept_new")
    probe = [second]

    def rotate(host: str, port: int, timeout: float) -> paramiko.PKey:
        return probe[0]

    rotating = HostKeyStore(tmp_path / "known_hosts", probe=rotate)
    status = rotating.ensure("192.0.2.1", policy="accept_changed")
    assert status.enrolled is True
    assert status.changed is True
    assert status.fingerprint == fingerprint_sha256(second)
    assert status.previous_fingerprint == fingerprint_sha256(first)
    again = rotating.ensure("192.0.2.1", policy="accept_changed")
    assert again.enrolled is False
    assert again.changed is False


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
