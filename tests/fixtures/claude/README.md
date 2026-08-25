# Fixtures

Synthetic, but faithful to the real on-disk layout Claude Code writes (verified against
a live machine). Deliberately **not** copied from a real installation: real session and
job files contain prompt text and account identifiers that must not land in a public
repository.

- `sessions/1001.json` — live, busy
- `sessions/1002.json` — **ghost**: registry file whose process is gone
- `sessions/1003.json` — idle session whose job is blocked on a human
- `sessions/1004.json` — pid reused by a non-Claude process
- `sessions/broken.json` — malformed, must produce a warning not a crash
