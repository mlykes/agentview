"""`agentview` command-line entrypoint."""

from __future__ import annotations

import sys
from typing import List, Optional

USAGE = """agentview - a local HUD for watching coding agents run

usage:
  agentview collect [options]     discover agents on this machine (see --help)
  agentview run -- <cmd>          launch an agent under tmux so it can be attached
  agentview hub                   serve the HUD

Run `agentview <command> --help` for options.
"""


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    command, rest = argv[0], argv[1:]
    if command == "collect":
        from agentview.collector.__main__ import main as collector_main

        return collector_main(rest)
    if command == "run":
        from agentview.runner import main as runner_main

        return runner_main(rest)
    if command == "hub":
        from agentview.hub.server import main as hub_main

        return hub_main(rest)

    print("unknown command: {}\n".format(command), file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
