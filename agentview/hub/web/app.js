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
    var isOpen = current && current.id === agent.id;
    var row = el("div", "agent" + (attachable ? " attachable" : "") + (isOpen ? " active" : ""));
    if (attachable) {
      row.title = "open this agent's terminal";
      row.addEventListener("click", function () { openTerminal(agent); });
    }

    var state = agent.stuck ? "stuck" : agent.status;
    row.appendChild(el("span", "dot " + state));

    // The harness assigns each session a colour; carry it through rather than
    // inventing one. The dot already means status, so the colour goes on the name
    // and the row's left edge.
    var colour = agent.color ? String(agent.color).toLowerCase().replace(/[^a-z]/g, "") : "";

    var name = el("div", "name");
    var label = el("span", "label", agent.name || "(unnamed)");
    if (colour) {
      // Unknown colour names fall back to inherit rather than to something invented.
      label.style.color = "var(--sc-" + colour + ", inherit)";
      row.style.borderLeftColor = "var(--sc-" + colour + ", transparent)";
    }
    name.appendChild(label);
    var notes = [];
    if (agent.harness_name) {
      notes.push(agent.harness_label + " calls it \"" + agent.harness_name + "\"");
    }
    if (agent.harness_color) {
      notes.push(agent.harness_label + " colours it " + agent.harness_color);
    }
    name.title = notes.length ? agent.name + "  (" + notes.join("; ") + ")" : (agent.name || "");
    if (canEdit) {
      name.appendChild(swatchButton(agent, name));
      name.appendChild(renameButton(agent, name, label));
      if (stoppable(agent)) name.appendChild(killButton(agent));
    }
    row.appendChild(name);

    var badge = el("span", "badge", agent.harness_label || agent.harness);
    if (agent.harness_version) badge.title = agent.harness_label + " " + agent.harness_version;
    row.appendChild(badge);

    // The directory is the group heading now, so repeating it on every row would
    // just be noise. The branch is not in the heading, so it stays here.
    var branch = el("div", "branch-cell", agent.git_branch ? "⎇ " + agent.git_branch : "");
    branch.title = agent.cwd || "";
    row.appendChild(branch);

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

  function stoppable(agent) {
    // The same two signals the server decides on -- a background job has an id to
    // pass to `claude stop`, and a tmux-resident agent has a session to kill.
    // Repeated here only to avoid rendering a control that would always fail; the
    // server re-checks and stays the authority.
    var extra = agent.extra || {};
    return !!(extra.job_id || extra.tmux_session);
  }

  function killButton(agent) {
    var btn = el("button", "rename kill", "\u2715");
    btn.type = "button";
    btn.title = "stop this agent";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();  // the row itself opens the terminal
      // Stopping ends a running agent and cannot be undone from here, so it asks
      // first -- and names the agent, because these rows sit close together.
      var what = (agent.extra || {}).job_id
        ? "Stop \"" + agent.name + "\"?\n\nThe session ends and its transcript is kept."
        : "Stop \"" + agent.name + "\"?\n\nThis kills the tmux session it runs in.";
      if (!window.confirm(what)) return;
      if (current && current.id === agent.id) closeTerminal();
      post("/v1/stop", { id: agent.id });
    });
    return btn;
  }

  function swatchButton(agent, nameCell) {
    var colour = agent.color ? String(agent.color).toLowerCase().replace(/[^a-z]/g, "") : "";
    var btn = el("button", "swatch" + (colour ? "" : " none"));
    btn.type = "button";
    btn.title = agent.color_pending
      // A colour set here reaches the session by being typed into its terminal, so
      // it waits until there is one open. Say so rather than leaving it a mystery.
      ? "colour: " + colour + " -- applies in the session when you open its terminal"
      : colour
        ? "colour: " + colour + " -- click to change"
        : "set a colour for this agent";
    if (agent.color_pending) btn.className += " pending";
    if (colour) btn.style.background = "var(--sc-" + colour + ", transparent)";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();  // the row itself opens the terminal
      openColourMenu(agent, nameCell, btn);
    });
    return btn;
  }

  function openColourMenu(agent, nameCell, anchor) {
    if (editing) return;
    editing = agent.id;

    var menu = el("div", "swatch-menu");
    menu.addEventListener("click", function (ev) { ev.stopPropagation(); });

    function close(commit, value) {
      if (!menu.parentNode) return;
      menu.parentNode.removeChild(menu);
      document.removeEventListener("mousedown", onOutside, true);
      document.removeEventListener("keydown", onKey, true);
      editing = null;
      if (commit) submitColour(agent.id, value);
      else tick();
    }
    function onOutside(ev) { if (!menu.contains(ev.target)) close(false); }
    function onKey(ev) { if (ev.key === "Escape") close(false); }

    palette.forEach(function (name) {
      var dot = el("button", "swatch");
      dot.type = "button";
      dot.title = name;
      dot.style.background = "var(--sc-" + name + ", transparent)";
      dot.addEventListener("click", function () { close(true, name); });
      menu.appendChild(dot);
    });

    // Clearing falls back to whatever the harness records, which for many sessions
    // is nothing -- so this reads as "no colour" rather than "default colour".
    var clear = el("button", "clear", "none");
    clear.type = "button";
    clear.addEventListener("click", function () { close(true, ""); });
    menu.appendChild(clear);

    nameCell.appendChild(menu);
    document.addEventListener("mousedown", onOutside, true);
    document.addEventListener("keydown", onKey, true);
  }

  function submitColour(id, value) {
    post("/v1/color", { id: id, color: value });
  }

  function post(path, body) {
    var headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(path, { method: "POST", headers: headers, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (res) { if (res && res.error) setConn(false, res.error); })
      .catch(function () { setConn(false, "update failed"); })
      .then(function () { tick(); });
  }

  function renameButton(agent, nameCell, label) {
    var btn = el("button", "rename", "\u270e");
    btn.type = "button";
    btn.title = "rename in agentview (clear the field to restore the original name)";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();  // the row itself opens the terminal
      startRename(agent, nameCell, label);
    });
    return btn;
  }

  function startRename(agent, nameCell, label) {
    if (editing) return;
    editing = agent.id;

    var input = document.createElement("input");
    input.type = "text";
    input.value = agent.name || "";
    input.maxLength = 64;
    input.addEventListener("click", function (ev) { ev.stopPropagation(); });

    var done = false;
    function finish(commit) {
      if (done) return;
      done = true;
      editing = null;
      if (commit) submitRename(agent.id, input.value);
      else tick();  // rebuild from server state
    }

    input.addEventListener("keydown", function (ev) {
      ev.stopPropagation();
      if (ev.key === "Enter") finish(true);
      else if (ev.key === "Escape") finish(false);
    });
    input.addEventListener("blur", function () { finish(true); });

    nameCell.replaceChild(input, label);
    input.focus();
    input.select();
  }

  function submitRename(id, value) {
    post("/v1/rename", { id: id, name: value });
  }

  function byDirectory(agents) {
    var groups = {};
    var order = [];
    agents.forEach(function (agent) {
      var key = agent.cwd || "";
      if (!groups[key]) {
        groups[key] = { cwd: key, label: key ? homeRelative(key) : "(no directory)", agents: [] };
        order.push(key);
      }
      groups[key].agents.push(agent);
    });
    // Alphabetical by displayed path so the list does not reshuffle as agents come
    // and go; agents with no directory sit at the end rather than sorting as "".
    order.sort(function (a, b) {
      if (!a) return 1;
      if (!b) return -1;
      return groups[a].label < groups[b].label ? -1 : groups[a].label > groups[b].label ? 1 : 0;
    });
    return order.map(function (key) { return groups[key]; });
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
      byDirectory(node.agents).forEach(function (group) {
        var head = el("div", "dir-head");
        head.appendChild(el("span", "dir-path", group.label));
        head.title = group.cwd || "";
        head.appendChild(el("span", "dir-count",
          group.agents.length + " agent" + (group.agents.length === 1 ? "" : "s")));
        card.appendChild(head);
        group.agents.forEach(function (agent) { card.appendChild(agentRow(agent)); });
      });
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
    // Rebuilding now would blow away the input the user is typing into.
    if (editing) return;
    root.textContent = "";
    if (!view.contexts.length) {
      root.appendChild(el("div", "empty", "No collectors reporting yet."));
      return;
    }
    view.contexts.forEach(function (node) { root.appendChild(contextCard(node, false)); });

    claimPending(view);

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

  /* --- launching a new agent ------------------------------------------ */

  var launchWrap = document.getElementById("launch");
  var launchBtn = document.getElementById("launch-btn");
  var launchMenu = document.getElementById("launch-menu");
  var pendingSession = null;
  //: Whether this hub accepts row edits; a read-only hub does not.
  var canEdit = false;
  //: Colour names the server has tokens for. Taken from the server rather than
  //: hardcoded here, so the palette cannot drift out of sync with the stylesheet.
  var palette = [];
  //: Agent id currently being edited. render() rebuilds the whole list, so an open
  //: editor or colour menu would be destroyed by the next poll tick unless we hold off.
  var editing = null;

  function loadHarnesses() {
    fetch("/v1/harnesses" + (token ? "?t=" + encodeURIComponent(token) : ""),
          { headers: token ? { Authorization: "Bearer " + token } : {} })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (data.colours) palette = data.colours;
        if (!!data.can_edit !== canEdit) {
          canEdit = !!data.can_edit;
          tick();  // repaint now rather than leaving the controls missing for a poll
        }
        if (!data.can_launch) return;
        launchWrap.hidden = false;
        launchMenu.textContent = "";
        if (!data.harnesses.length) {
          launchMenu.appendChild(el("div", "none", "no agent CLIs found on PATH"));
          return;
        }
        data.harnesses.forEach(function (h) {
          var item = el("button", null, h.label);
          item.addEventListener("click", function () { launch(h); });
          launchMenu.appendChild(item);
        });
      })
      .catch(function () { /* launching stays hidden */ });
  }

  function launch(harness) {
    launchMenu.hidden = true;
    launchBtn.disabled = true;
    launchBtn.textContent = "starting " + harness.label + "…";
    api("/v1/launch", { harness: harness.harness })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.body.error || "could not start it");
        // The agent takes a moment to register. Remember the tmux session and open
        // its terminal as soon as the next poll shows it.
        pendingSession = res.body.session;
        launchBtn.textContent = "waiting for " + harness.label + "…";
        tick();
      })
      .catch(function (err) {
        launchBtn.disabled = false;
        launchBtn.textContent = "+ new agent";
        alert("Could not start the agent: " + err.message);
      });
  }

  function claimPending(view) {
    if (!pendingSession) return;
    var found = null;
    view.contexts.forEach(function (node) {
      [node].concat(node.children || []).forEach(function (ctx) {
        ctx.agents.forEach(function (a) {
          if (a.extra && a.extra.tmux_session === pendingSession) found = a;
        });
      });
    });
    if (!found || !found.attach || !found.attach.available) return;
    pendingSession = null;
    launchBtn.disabled = false;
    launchBtn.textContent = "+ new agent";
    openTerminal(found);
  }

  launchBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    launchMenu.hidden = !launchMenu.hidden;
  });
  document.addEventListener("click", function () { launchMenu.hidden = true; });

  /* --- terminal ------------------------------------------------------- */

  var term = null, fit = null, stream = null, current = null, streamLive = false;
  var resizeObserver = null;
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
    if (current && current.id === agent.id) return;   // already showing this one
    closeTerminal();
    current = agent;
    streamLive = false;
    lastSize = "";
    overlay.hidden = false;
    document.body.classList.add("with-terminal");
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
    // The list narrows as the dock appears, so the container's final width is not
    // known yet. Observe it instead of guessing when layout has settled -- guessing
    // left the terminal sized for the pre-split width and clipped on the right.
    doFit();
    if (window.ResizeObserver) {
      resizeObserver = new ResizeObserver(function () { doFit(); });
      resizeObserver.observe(body);
    } else {
      requestAnimationFrame(doFit);
      setTimeout(doFit, 120);
    }

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
        if (!streamLive) {
          streamLive = true;
          setFoot(null, true);
          // First content can introduce a scrollbar; re-measure once it has.
          requestAnimationFrame(doFit);
        }
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
    if (!streamLive) setFoot(null, false);   // the seed may already have connected us
    term.focus();
  }

  var lastSize = "";

  function doFit() {
    if (!fit || !term) return;
    try {
      fit.fit();
      var size = term.cols + "x" + term.rows;
      // The observer fires repeatedly during layout; only tell the PTY when the
      // size actually changed.
      if (current && size !== lastSize) {
        lastSize = size;
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
    if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null; }
    if (stream) { stream.close(); stream = null; }
    if (term) { term.dispose(); term = null; }
    fit = null;
    body.textContent = "";
    overlay.hidden = true;
    document.body.classList.remove("with-terminal");
    current = null;
  }

  document.getElementById("term-close").addEventListener("click", function () {
    closeTerminal();
    tick();   // repaint the list at full width and drop the active highlight
  });
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
    // Capabilities first: agentRow() reads canEdit and the palette, so learning
    // them after the first render would leave the controls missing until the next
    // poll -- indistinguishable from the feature not existing.
    var caps = document.getElementById("caps");
    if (caps && caps.textContent) {
      var parsed = JSON.parse(caps.textContent);
      canEdit = !!parsed.can_edit;
      palette = parsed.colours || [];
    }
  } catch (e) {
    /* the /v1/harnesses fetch below still settles it */
  }

  try {
    var boot = document.getElementById("bootstrap");
    if (boot && boot.textContent) {
      render(JSON.parse(boot.textContent));
      setConn(true, "live");
    }
  } catch (e) {
    /* fall back to the poll below */
  }

  loadHarnesses();
  tick();
  setInterval(tick, POLL_MS);
})();
