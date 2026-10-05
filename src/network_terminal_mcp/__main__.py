"""Command-line entry point for the local policy health check."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from network_terminal_mcp.config.loader import load_config
from network_terminal_mcp.errors import NetworkMCPError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-terminal-mcp",
        description="network-terminal-mcp policy health check",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="override configuration directory "
        "(default: $NETWORK_MCP_CONFIG_DIR or ~/.config/network-terminal-mcp)",
    )
    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("check", help="validate the optional local policy file")
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
        config = load_config(args.config_dir)
        print(f"config dir: {config.config_dir}")
        print(config.to_summary())
        return 0
    except NetworkMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
