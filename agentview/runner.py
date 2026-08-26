"""`agentview run` -- launch an agent inside tmux so it can be attached to.

You cannot attach to the PTY of a process started outside a multiplexer. That is an
OS-level fact, not a limitation we can engineer around, so agents must be *started*
inside tmux to be watchable. This shim does that and nothing else: it does not wrap,
proxy or interpret the agent, it just puts it in a named tmux session and hands over
the terminal.

    agentview run -- claude
    agentview run --name api -- opencode

Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from typing import List, Optional

from agentview.collector import tmux

SESSION_PREFIX = "agentview"


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    return slug or "agent"


def session_name(name: str) -> str:
    # tmux treats "." and ":" as target separators, so they must not appear here.
    return "{}_{}".format(SESSION_PREFIX, slugify(name))


def unique_session_name(name: str) -> str:
    base = session_name(name)
    if not tmux.has_session(base):
        return base
    return "{}-{}".format(base, int(time.time()) % 100000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentview run",
        description="Launch an agent inside a tmux session so agentview can attach to it.",
        epilog="example: agentview run --name api -- opencode",
    )
    parser.add_argument("--name", default=None, help="label for this agent (default: the command)")
    parser.add_argument(
        "--detached", action="store_true",
        help="start the session in the background instead of attaching your terminal",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- <command to run>")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("nothing to run. usage: agentview run -- <command>", file=sys.stderr)
        return 2

    if not tmux.available():
        print(
            "tmux is not installed, and it is what makes attach possible.\n"
            "Install it, or run the agent normally -- it will still appear in the HUD,\n"
            "just without a terminal view.",
            file=sys.stderr,
        )
        return 1

    name = args.name or os.path.basename(command[0])
    session = unique_session_name(name)

    # -A would attach to an existing session of the same name; we picked a unique one
    # above so each launch is its own session.
    create = ["tmux", "new-session", "-d", "-s", session, "-n", slugify(name)] + command
    try:
        result = subprocess.run(create, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        print("could not start tmux session: {}".format(exc), file=sys.stderr)
        return 1
    if result.returncode != 0:
        print("tmux refused to start the session: {}".format(result.stderr.strip()), file=sys.stderr)
        return 1

    print("started '{}' in tmux session {}".format(name, session), file=sys.stderr)
    if args.detached:
        print("attach with:  tmux attach -t {}".format(session), file=sys.stderr)
        return 0

    # Hand the terminal over. execvp replaces this process so ctrl-b etc. behave
    # exactly as they would in a normal tmux session.
    try:
        os.execvp("tmux", ["tmux", "attach", "-t", session])
    except OSError as exc:
        print("started, but could not attach: {}".format(exc), file=sys.stderr)
        return 1
    return 0
