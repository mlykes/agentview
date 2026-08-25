"""Push snapshots to a hub.

Outbound only. The collector never binds a port -- that is what keeps agentview
deployable on a machine whose security posture you do not control.

Stdlib only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional


class HubClient:
    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload, default=str).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8") or "{}"
        return json.loads(body)

    def hello(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/v1/hello", {"context": context})

    def push(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        return self._post("/v1/snapshot", snapshot)
