"""Command-line entry point for configuration health checks.

Phase 0 ships a small CLI that validates the configuration and prints a summary
or a single device target. The MCP stdio server is added in Phase 1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from network_terminal_mcp.config.loader import load_config
from network_terminal_mcp.errors import NetworkMCPError
from network_terminal_mcp.targets.resolver import TargetResolver


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-terminal-mcp",
        description="network-terminal-mcp configuration health check",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="override configuration directory "
        "(default: $NETWORK_MCP_CONFIG_DIR or ~/.config/network-terminal-mcp)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate configuration and print summary")
    check.add_argument(
        "--device",
        help="resolve and print a single inventory device",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_dir)
        if args.device:
            target = TargetResolver(config).resolve(name=args.device)
            print(
                f"{target.name}: host={target.host} "
                f"credentials={target.credentials} connection={target.connection}"
            )
        else:
            print(f"config dir: {config.config_dir}")
            print(config.to_summary())
        return 0
    except NetworkMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
