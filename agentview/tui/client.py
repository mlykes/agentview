"""Minimal TUI-shaped client.

Deliberately dumb: fetch the hub's snapshot, hand it to the shared renderer. Its job
is to prove the API is sufficient for a non-web frontend before we invest in a real
terminal UI. Stdlib only, like everything a restricted box has to run.

    python3 -m agentview.tui.client --hub http://localhost:7788
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import List, Optional

from agentview.collector.render import render
from agentview.model import AgentRecord, ContextRef, Snapshot


def snapshot_from_dict(payload: dict) -> Snapshot:
    ctx = ContextRef(**payload["context"])
    agents = []
    for raw in payload.get("agents", []):
        raw = dict(raw)
        raw.pop("attach", None)  # rendering does not need the attach spec
        agents.append(AgentRecord(**raw))
    return Snapshot(
        context=ctx,
        agents=agents,
        collected_at=payload.get("collected_at", 0.0),
        warnings=payload.get("warnings", []),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m agentview.tui.client")
    parser.add_argument("--hub", default="http://127.0.0.1:7788")
    args = parser.parse_args(argv)

    url = args.hub.rstrip("/") + "/v1/agents"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - a CLI should explain, not traceback
        print("could not reach hub at {}: {}".format(url, exc), file=sys.stderr)
        print("(the hub lands in M2; until then use `agentview collect`)", file=sys.stderr)
        return 1

    print(render(snapshot_from_dict(payload)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
