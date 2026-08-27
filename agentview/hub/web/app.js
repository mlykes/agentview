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
  //: ?open=<agent id> deep-links straight to an agent's terminal.
  var openOnLoad = null;
  try {
    var qs = new URLSearchParams(window.location.search);
    openOnLoad = qs.get("open");
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
    var attachable = agent.attach && agent.attach.available;
    var row = el("div", "agent" + (attachable ? " attachable" : ""));
    if (attachable) {
      row.title = "open this agent's terminal";
      row.addEventListener("click", function () { openTerminal(agent); });
    }

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
    if (attachable) {
      row.appendChild(el("div", "attach-hint", "click to open terminal"));
    } else if (agent.attach && agent.attach.reason) {
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

  function findAgent(view, agentId) {
    var found = null;
    view.contexts.forEach(function (node) {
      [node].concat(node.children || []).forEach(function (ctx) {
        ctx.agents.forEach(function (a) { if (a.id === agentId) found = a; });
      });
    });
    return found;
  }

  function render(view) {
    renderTotals(view.totals);
    root.textContent = "";
    if (!view.contexts.length) {
      root.appendChild(el("div", "empty", "No collectors reporting yet."));
      return;
    }
    view.contexts.forEach(function (node) { root.appendChild(contextCard(node, false)); });

    if (openOnLoad) {
      var target = findAgent(view, openOnLoad);
      openOnLoad = null;
      if (target && target.attach && target.attach.available) openTerminal(target);
    }
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

  /* --- terminal ------------------------------------------------------- */

  var term = null, fit = null, stream = null, current = null, streamLive = false;
  var overlay = document.getElementById("term-overlay");
  var body = document.getElementById("term-body");
  var foot = document.getElementById("term-foot");
  var stateEl = document.getElementById("term-state");

  function b64bytes(b64) {
    // atob() yields a binary string, which would mangle multi-byte UTF-8 -- and
    // agent TUIs are made of box-drawing characters. xterm.js takes a Uint8Array
    // and decodes it itself, correctly across chunk boundaries.
    var raw = atob(b64);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  function api(path, payload) {
    return fetch(path + (token ? "?t=" + encodeURIComponent(token) : ""), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  function openTerminal(agent) {
    closeTerminal();
    current = agent;
    streamLive = false;
    overlay.hidden = false;
    document.getElementById("term-title").textContent = agent.name;
    document.getElementById("term-sub").textContent =
      (agent.harness_label || agent.harness) + "  ·  " + (agent.cwd || "");
    document.getElementById("term-dot").className = "term-dot";
    if (stateEl) stateEl.textContent = "";

    term = new Terminal({
      fontSize: 13,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      cursorBlink: true,
      scrollback: 10000,
      // Option-as-Meta matches how a Mac terminal is normally configured, so
      // alt-word-motion behaves the way it does in iTerm2.
      macOptionIsMeta: true,
      // Right-click should offer the browser's own copy/paste rather than being
      // swallowed as a terminal event.
      rightClickSelectsWord: true,
      theme: { background: "#0b0e13", foreground: "#e7edf5" },
    });
    try {
      fit = new FitAddon.FitAddon();
      term.loadAddon(fit);
    } catch (e) { fit = null; }
    term.open(body);
    doFit();

    // Everything xterm emits is forwarded, including the protocol replies tmux
    // waits on before it will paint. Read-only is enforced by the session running
    // `tmux attach -r`, which discards keystrokes server-side -- a stronger
    // guarantee than the browser choosing not to send them.
    // Everything the terminal emits goes to the PTY -- keystrokes and the protocol
    // replies tmux waits on alike. This is a terminal; it behaves like one.
    term.onData(function (data) {
      if (!current) return;
      api("/v1/attach/" + encodeURIComponent(current.id) + "/input", { d: data });
    });

    // Paint the current screen from the server-injected block when this page was
    // deep-linked to this agent. The SSE stream takes over from there.
    try {
      var seed = document.getElementById("term-bootstrap");
      if (seed && seed.textContent) {
        var parsed = JSON.parse(seed.textContent);
        if (parsed.agent_id === agent.id && parsed.data) {
          term.write(b64bytes(parsed.data));
          streamLive = true;
          setFoot(null, true);   // content is on screen; don't keep saying "connecting"
        }
      }
    } catch (e) { /* the stream will fill it in */ }

    var url = "/v1/attach/" + encodeURIComponent(agent.id) + "/stream" +
      "?cols=" + term.cols + "&rows=" + term.rows +
      (token ? "&t=" + encodeURIComponent(token) : "");

    // EventSource cannot set headers, which is why the token also travels as ?t=.
    stream = new EventSource(url);
    stream.onmessage = function (ev) {
      try {
        // atob() yields a binary string, which would mangle any multi-byte UTF-8 --
        // and agent TUIs are full of box-drawing characters. xterm.js accepts a
        // Uint8Array and does the decoding itself, including across chunk splits.
        term.write(b64bytes(ev.data));
        if (!streamLive) { streamLive = true; setFoot(null, true); }
      } catch (e) { /* skip bad frame */ }
    };
    stream.addEventListener("end", function () {
      document.getElementById("term-dot").className = "term-dot dead";
      setFoot("session ended", false);
      if (stream) { stream.close(); stream = null; }
    });
    // EventSource reconnects on its own, so this is a transient state, not a dead
    // end -- saying "close and reopen" would send the user to do work the browser
    // is already doing.
    stream.onerror = function () {
      if (stream && stream.readyState === 2) {
        setFoot("terminal disconnected", false);
      } else {
        setFoot("reconnecting…", false);
      }
    };
    setFoot(null, false);
    term.focus();
  }

  function doFit() {
    if (!fit || !term) return;
    try {
      fit.fit();
      if (current) {
        api("/v1/attach/" + encodeURIComponent(current.id) + "/resize",
            { cols: term.cols, rows: term.rows });
      }
    } catch (e) { /* container not laid out yet */ }
  }

  function setFoot(message, live) {
    foot.textContent = message || (live ? "connected" : "connecting…");
    foot.className = "term-foot" + (live && !message ? " live" : "");
  }

  function closeTerminal() {
    if (stream) { stream.close(); stream = null; }
    if (term) { term.dispose(); term = null; }
    fit = null;
    body.textContent = "";
    overlay.hidden = true;
    current = null;
  }

  document.getElementById("term-close").addEventListener("click", closeTerminal);
  overlay.addEventListener("click", function (e) {
    if (e.target === overlay) closeTerminal();
  });
  document.addEventListener("keydown", function (e) {
    // Only when focus is outside the terminal -- inside it, Escape belongs to the
    // agent, which is the whole point of behaving like a real terminal.
    if (e.key === "Escape" && !overlay.hidden && !body.contains(document.activeElement)) {
      closeTerminal();
    }
  });
  window.addEventListener("resize", doFit);

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
