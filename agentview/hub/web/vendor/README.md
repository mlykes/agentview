# Vendored third-party assets

Committed rather than fetched at runtime, deliberately. The published page has a strict
CSP with no external origins, and the whole point of agentview is running on a machine
with no package-registry or CDN access. A `npm install` at deploy time would defeat that.

| File | Source | Version | License |
|---|---|---|---|
| `xterm.js`, `xterm.css` | [xtermjs/xterm.js](https://github.com/xtermjs/xterm.js) (`@xterm/xterm`) | 5.5.0 | MIT — see `xterm.LICENSE` |
| `addon-fit.js` | `@xterm/addon-fit` | 0.10.0 | MIT — same project |

To refresh (runs in a container; nothing is installed on the host):

```bash
docker run --rm -v "$PWD/agentview/hub/web/vendor":/out node:20-slim sh -c '
  cd /tmp && npm pack @xterm/xterm@5.5.0 && tar xzf *.tgz &&
  cp package/lib/xterm.js package/css/xterm.css package/LICENSE /out/'
```
