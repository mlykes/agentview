"""Collector entrypoint.

    python3 -m agentview.collector --once            # JSON snapshot
    python3 -m agentview.collector --once -f table   # human-readable

MUST run on a bare interpreter with an empty site-packages -- that zero-dependency
property is what makes this deployable to a locked-down box by copying a directory.
CI enforces it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from agentview.collector import context as context_mod
from agentview.collector.core import collect
from agentview.collector.render import render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m agentview.collector",
        description="Discover running coding agents and report them.",
    )
    parser.add_argument(
        "--once", action="store_true", help="collect a single snapshot and exit"
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("json", "table"),
        default="json",
        help="output format (default: json)",
    )
    parser.add_argument(
        "--interval", type=float, default=5.0, help="seconds between ticks (default: 5)"
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Claude Code config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    parser.add_argument(
        "--label", default=None, help="display label for this context, e.g. 'work devbox'"
    )
    parser.add_argument(
        "--parent",
        default=None,
        help="context id of the host running this container, so the UI can nest it",
    )
    parser.add_argument("--indent", type=int, default=None, help="pretty-print JSON")
    parser.add_argument(
        "--hub", default=None, help="push snapshots to this hub, e.g. http://localhost:7788"
    )
    parser.add_argument("--token", default=None, help="hub auth token")
    parser.add_argument("--quiet", action="store_true", help="do not print snapshots")
    return parser


def emit(snapshot, fmt: str, indent: Optional[int]) -> None:
    if fmt == "table":
        print(render(snapshot))
    else:
        print(json.dumps(snapshot.to_dict(), indent=indent, default=str))
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = context_mod.detect(parent_id=args.parent, label=args.label)

    if args.once:
        emit(collect(ctx=ctx, config_dir=args.config_dir), args.format, args.indent)
        return 0

    client = None
    if args.hub:
        from agentview.collector.transport import HubClient

        client = HubClient(args.hub, token=args.token)
        try:
            client.hello(ctx.to_dict())
            print("connected to hub at {}".format(args.hub), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - retry on the next tick instead
            print("hub not reachable yet ({}); will keep trying".format(exc), file=sys.stderr)

    try:
        while True:
            snapshot = collect(ctx=ctx, config_dir=args.config_dir)
            if client:
                try:
                    client.push(snapshot.to_dict())
                except Exception as exc:  # noqa: BLE001 - a hub blip must not kill us
                    print("push failed: {}".format(exc), file=sys.stderr)
            if not args.quiet:
                emit(snapshot, args.format, args.indent)
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
