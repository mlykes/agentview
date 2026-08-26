/* agentview overview.
 *
 * Polls /v1/view. Polling rather than a WebSocket is deliberate: it keeps the hub
 * dependency-free, and a HUD refreshing every 2s is indistinguishable from push at
 * human timescales. The attach terminal in M3 is where a real socket earns its keep.
 */
(function () {
  "use strict";

  var POLL_MS = 2000;

  // The token arrives as ?t=... on first load. Stash it so the URL can be cleaned up
  // and a refresh still works, then keep it out of the address bar.
  var token = null;
  try {
    var qs = new URLSearchParams(window.location.search);
    token = qs.get("t") || sessionStorage.getItem("agentview_token");
    if (qs.get("t")) {
      sessionStorage.setItem("agentview_token", qs.get("t"));
      history.replaceState({}, "", window.location.pathname);
    }
  } catch (e) {
    /* private mode: fall back to whatever is in the URL */
  }

  var root = document.getElementById("root");
  var totalsEl = document.getElementById("totals");
  var connEl = document.getElementById("conn");
  var connText = document.getElementById("conn-text");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function duration(seconds) {
    if (seconds == null) return "-";
    var s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h";
    return Math.floor(s / 86400) + "d";
  }

  function homeRelative(path) {
    if (!path) return "-";
    return path.replace(/^\/(Users|home)\/[^/]+/, "~");
  }

  function agentRow(agent) {
    var row = el("div", "agent");

    var state = agent.stuck ? "stuck" : agent.status;
    row.appendChild(el("span", "dot " + state));

    var name = el("div", "name", agent.name || "(unnamed)");
    name.title = agent.name || "";
    row.appendChild(name);

    var badge = el("span", "badge", agent.harness_label || agent.harness);
    if (agent.harness_version) badge.title = agent.harness_label + " " + agent.harness_version;
    row.appendChild(badge);

    var cwd = el("div", "cwd");
    cwd.appendChild(document.createTextNode(homeRelative(agent.cwd)));
    if (agent.git_branch) {
      cwd.appendChild(el("span", "branch", "  ⎇ " + agent.git_branch));
    }
    cwd.title = agent.cwd || "";
    row.appendChild(cwd);

    var right = el("div", "right");
    if (agent.stuck) {
      right.appendChild(el("span", "stuck-tag", "stuck " + duration(agent.idle_for)));
    } else {
      right.appendChild(el("span", "status-word " + agent.status, agent.status));
    }
    if (agent.tokens) right.appendChild(el("span", null, agent.tokens.toLocaleString() + " tok"));
    right.appendChild(el("span", null, duration(agent.uptime)));
    row.appendChild(right);

    if (agent.detail) {
      var detail = el("div", "detail", agent.detail.replace(/\s+/g, " "));
      detail.title = agent.detail;
      row.appendChild(detail);
    }
    // Be explicit about why the detail view is unavailable rather than silently
    // rendering a dead affordance.
    if (agent.attach && !agent.attach.available && agent.attach.reason) {
      row.appendChild(el("div", "no-attach", agent.attach.reason));
    }
    return row;
  }

  function contextCard(node, isChild) {
    var ctx = node.context;
    var card = el("div", "ctx" + (isChild ? " child" : ""));

    var head = el("div", "ctx-head");
    var label = (ctx.kind === "container" ? "📦 " : "") + (ctx.label || ctx.id);
    head.appendChild(el("div", "ctx-label", label));

    var meta = [];
    if (ctx.platform) meta.push(ctx.platform + (ctx.arch ? "/" + ctx.arch : ""));
    if (ctx.via_ssh) meta.push("ssh");
    if (ctx.workspace_folder) meta.push(ctx.workspace_folder);
    head.appendChild(el("div", "ctx-meta", meta.join(" · ")));

    head.appendChild(el("div", "ctx-count", node.agents.length + " agent" + (node.agents.length === 1 ? "" : "s")));
    card.appendChild(head);

    if (!node.agents.length) {
      card.appendChild(el("div", "empty", "no agents running here"));
    } else {
      node.agents.forEach(function (agent) { card.appendChild(agentRow(agent)); });
    }

    if (node.warnings && node.warnings.length) {
      card.appendChild(el("div", "warnings", "! " + node.warnings.join("  |  ")));
    }

    if (node.children && node.children.length) {
      var kids = el("div", "children");
      node.children.forEach(function (child) { kids.appendChild(contextCard(child, true)); });
      card.appendChild(kids);
    }
    return card;
  }

  function renderTotals(t) {
    totalsEl.textContent = "";
    function stat(value, label, warn) {
      var wrap = el("span", warn ? "warn" : null);
      wrap.appendChild(el("b", null, String(value)));
      wrap.appendChild(document.createTextNode(" " + label));
      return wrap;
    }
    totalsEl.appendChild(stat(t.agents, t.agents === 1 ? "agent" : "agents"));
    totalsEl.appendChild(stat(t.busy, "busy"));
    totalsEl.appendChild(stat(t.contexts, t.contexts === 1 ? "context" : "contexts"));
    if (t.stuck) totalsEl.appendChild(stat(t.stuck, "stuck", true));
  }

  function render(view) {
    renderTotals(view.totals);
    root.textContent = "";
    if (!view.contexts.length) {
      root.appendChild(el("div", "empty", "No collectors reporting yet."));
      return;
    }
    view.contexts.forEach(function (node) { root.appendChild(contextCard(node, false)); });
  }

  function setConn(ok, text) {
    connEl.className = "conn " + (ok ? "ok" : "bad");
    connText.textContent = text;
  }

  function tick() {
    var headers = token ? { Authorization: "Bearer " + token } : {};
    fetch("/v1/view", { headers: headers, cache: "no-store" })
      .then(function (r) {
        if (r.status === 401) throw new Error("unauthorized - reopen the link the hub printed");
        if (!r.ok) throw new Error("hub returned " + r.status);
        return r.json();
      })
      .then(function (view) {
        render(view);
        setConn(true, "live");
      })
      .catch(function (err) {
        setConn(false, err.message || "hub unreachable");
      });
  }

  // Render the server-injected snapshot immediately so the first frame has real
  // content. Falls through to polling either way.
  try {
    var boot = document.getElementById("bootstrap");
    if (boot && boot.textContent) {
      render(JSON.parse(boot.textContent));
      setConn(true, "live");
    }
  } catch (e) {
    /* fall back to the poll below */
  }

  tick();
  setInterval(tick, POLL_MS);
})();
