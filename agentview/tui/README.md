# TUI client (scaffolding)

Not built yet. It exists now to keep the eventual SSH-native TUI cheap, by proving
one thing early: **the hub's API is sufficient to render the HUD without any hub-side
logic.** `client.py` consumes the same `/v1/agents` endpoint the web UI uses and
renders it with the shared `collector.render` module.

If a future TUI ever needs something the API cannot provide, that is a bug in the
API, not a reason to special-case the TUI.

Attach is transport-agnostic for the same reason: the PTY proxy speaks raw bytes, so
a TUI can shell straight to `tmux attach` locally and skip the hub entirely.
