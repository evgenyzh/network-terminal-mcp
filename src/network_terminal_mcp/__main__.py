"""Command-line entry point and runtime readiness check."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from network_terminal_mcp.errors import NetworkMCPError
from network_terminal_mcp.sessions.manager import default_audit_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-terminal-mcp",
        description="persistent local terminals for MCP clients",
    )
    parser.add_argument("--version", action="version", version=_package_version())
    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser(
        "check",
        help="check ssh, telnet, serial, and audit readiness",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        # No subcommand: run the MCP stdio server (this is how clients start it).
        from network_terminal_mcp.server import create_server

        create_server().run(transport="stdio")
        return 0
    try:
        return _check()
    except NetworkMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _check() -> int:
    ok = True
    print(f"network-terminal-mcp {_package_version()}")

    ssh_path = shutil.which("ssh")
    if ssh_path is None:
        ok = False
        print("ssh: not found on PATH (SSH sessions unavailable)")
    else:
        print(f"ssh: {ssh_path}")

    telnet_path = shutil.which("telnet")
    if telnet_path is None:
        print("telnet: not found on PATH (Telnet sessions unavailable)")
    else:
        print(f"telnet: {telnet_path}")

    try:
        import serial
    except ImportError:
        ok = False
        print("serial: pyserial is not installed")
    else:
        print(f"serial: pyserial {getattr(serial, '__version__', 'unknown')}")

    audit_path = default_audit_path()
    try:
        _check_audit_dir(audit_path)
    except OSError as exc:
        ok = False
        print(f"audit: not writable: {audit_path}: {exc}")
    else:
        print(f"audit: {audit_path} (writable)")

    print("config: none required")
    if ssh_path is None and telnet_path is None:
        ok = False
        print("error: neither ssh nor telnet is available", file=sys.stderr)
    return 0 if ok else 1


def _check_audit_dir(path: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=parent,
        prefix=".audit-check-",
        delete=True,
    ):
        pass


def _package_version() -> str:
    try:
        return version("network-terminal-mcp")
    except PackageNotFoundError:  # pragma: no cover - installed package
        return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
