/* OpenAmika — Tools page: her agents & MCP servers, consent settings, jobs, approvals, call log. */
(function () {
  "use strict";
  const { nav, footer, toast, getJSON, postAction, el } = window.OA;
  nav("tools"); footer();
  const $ = (id) => document.getElementById(id);

  let state = null;        // last GET /api/build_f/tools payload (jobs and approvals are shown on the Use page)

  function configFromState(s) {
    // the brain hands over the raw tools.json (env / cwd / headers included) — a save must never strip
    // what the cards do not show. The reconstruction below is only for a brain that predates "config".
    if (s.config && typeof s.config === "object") {
      const doc = JSON.parse(JSON.stringify(s.config));
      doc.mcpServers = doc.mcpServers && typeof doc.mcpServers === "object" ? doc.mcpServers : {};
      return doc;
    }
    const servers = {};
    for (const [name, server] of Object.entries(s.servers || {})) {
      const entry = { enabled: !!server.enabled, tools: { ...(server.policies || {}) } };
      if (server.transport === "http") {
        entry.url = server.target;
      } else {
        const parts = (server.target || "").trim().split(/\s+/);
        entry.command = parts[0] || "";
        entry.args = parts.slice(1);
      }
      if (server.kind === "agent") entry.kind = "agent";
      if (server.persona_note) entry.persona_note = server.persona_note;
      servers[name] = entry;
    }
    return { enabled: !!s.enabled, clerk_role: s.clerk_role || "surface", limits: { ...(s.limits || {}) }, mcpServers: servers };
  }

  function splitCommand(text) {
    const out = [];
    const re = /"([^"]*)"|(\S+)/g;
    let m;
    while ((m = re.exec(text || ""))) out.push(m[1] !== undefined ? m[1] : m[2]);
    return out;
  }
  function joinCommand(auto) {
    if (!auto || !auto.command) return "";
    const q = (s) => (/\s/.test(s) ? '"' + s + '"' : s);
    return [auto.command, ...(auto.args || [])].map(q).join(" ");
  }

  function mergedConfig() {
    // the UI-visible knobs win; everything else (env/headers/cwd) comes from the raw editor
    let raw;
    try { raw = JSON.parse($("rawConfig").value); } catch (_) { raw = null; }
    const base = raw && typeof raw === "object" ? raw : configFromState(state || {});
    base.enabled = $("toolsEnabled").checked;
    base.clerk_role = $("clerkRole").value;
    base.limits = {
      max_calls_per_turn: Number($("limCalls").value) || 3,
      call_timeout_s: Number($("limTimeout").value) || 20,
      poll_interval_s: Number($("limPoll").value) || 2.5,
    };
    base.mcpServers = base.mcpServers || {};
    document.querySelectorAll("[data-server-toggle]").forEach((input) => {
      const name = input.getAttribute("data-server-toggle");
      if (base.mcpServers[name]) base.mcpServers[name].enabled = input.checked;
    });
    document.querySelectorAll("select[data-policy-server]").forEach((select) => {
      const name = select.getAttribute("data-policy-server");
      const tool = select.getAttribute("data-policy-tool");
      if (base.mcpServers[name]) {
        base.mcpServers[name].tools = base.mcpServers[name].tools || {};
        base.mcpServers[name].tools[tool] = select.value;
      }
    });
    document.querySelectorAll("input[data-autostart]").forEach((input) => {
      const name = input.getAttribute("data-autostart");
      const entry = base.mcpServers[name];
      if (!entry) return;
      const parts = splitCommand(input.value.trim());
      if (!parts.length) { delete entry.autostart; return; }
      const prev = entry.autostart && typeof entry.autostart === "object" ? entry.autostart : {};
      entry.autostart = { ...prev, command: parts[0], args: parts.slice(1) };   // cwd/env from the raw editor survive
    });
    document.querySelectorAll("textarea[data-note]").forEach((area) => {
      const name = area.getAttribute("data-note");
      if (base.mcpServers[name]) {
        const note = area.value.trim();
        if (note) base.mcpServers[name].persona_note = note; else delete base.mcpServers[name].persona_note;
      }
    });
    return base;
  }

  // ── the IoT safety statement (docs/IOT_DISCLAIMER.md): IoT turns on only after it was read and accepted ──
  // The brain and the gateway refuse a config that switches a smart-home server on before that (409, code
  // iot_disclaimer_required); this page asks first, and answers such a refusal with the same dialog.
  const consent = { doc: null, resolve: null, promise: null, mode: null, autoShown: false };

  function iotAccepted() { return !!(state && state.iot_disclaimer && state.iot_disclaimer.accepted); }
  function isSmartHome(name) { return !!(state && state.servers && state.servers[name] && state.servers[name].iot); }
  function turnsOnIot(cfg) {
    return Object.entries((cfg && cfg.mcpServers) || {}).some(([name, entry]) => entry && entry.enabled && isSmartHome(name));
  }

  function inlineText(text) {               // the statement's **bold**; everything else stays plain text
    const frag = document.createDocumentFragment();
    String(text || "").split("**").forEach((part, i) => {
      if (part) frag.append(i % 2 ? el("strong", { text: part }) : document.createTextNode(part));
    });
    return frag;
  }

  function showStatement(lang) {
    const languages = (consent.doc && consent.doc.statement && consent.doc.statement.languages) || {};
    const section = languages[lang] || languages.en;
    if (!section) return;
    const box = $("iotConsentText");
    box.replaceChildren(...section.blocks.map((block) =>
      block.type === "h" ? el("h4", {}, inlineText(block.text))
        : block.type === "ul" ? el("ul", {}, ...block.items.map((item) => el("li", {}, inlineText(item))))
          : el("p", {}, inlineText(block.text))));
    box.setAttribute("lang", lang === "zh" ? "zh-CN" : languages[lang] ? lang : "en");
    box.scrollTop = 0;
    document.querySelectorAll(".oa-consent-lang").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
  }

  // mode "accept": resolves true once the person ticked the box, pressed Enable IoT and the acceptance was recorded;
  // false on Cancel / Escape (IoT stays off); null when the statement could not even be loaded (already said so).
  // mode "view": the statement with a Close button.
  async function openConsent(mode) {
    const dialog = $("iotConsent");
    if (dialog.open) return mode !== "view" && consent.mode === "accept" ? consent.promise : false;
    try { consent.doc = await getJSON("/api/build_f/iot/disclaimer"); }
    catch (error) {
      toast("The IoT safety statement could not be loaded, so IoT stays off: " + (error.message || error), true);
      return null;
    }
    if (dialog.open) return false;
    const doc = consent.doc, accepting = mode !== "view";
    consent.mode = accepting ? "accept" : "view";
    $("iotConsentVersion").textContent = "Version " + doc.version;
    $("iotConsentLead").textContent = accepting
      ? "Before Amika can reach anything in your home, read this statement in full. IoT stays off unless you accept it."
      : doc.accepted ? "You accepted this version" + (doc.accepted_at ? " on " + new Date(doc.accepted_at).toLocaleString() : "") + "."
        : "IoT stays off until you accept this statement; turning IoT on asks for it.";
    const changed = $("iotConsentChanged");
    changed.hidden = !(accepting && doc.accepted_version && doc.accepted_version !== doc.version);
    changed.textContent = "This statement changed since you accepted version " + doc.accepted_version
      + ". IoT stays off until you read and accept the new version.";
    $("iotConsentAccept").hidden = !accepting;
    $("iotConsentEnable").hidden = !accepting;
    $("iotConsentCancel").textContent = accepting ? "Cancel" : "Close";
    $("iotConsentCheck").checked = false;
    $("iotConsentEnable").disabled = true;
    $("iotConsentError").hidden = true;
    showStatement("en");
    dialog.showModal();
    $("iotConsentText").focus();
    consent.promise = new Promise((resolve) => { consent.resolve = resolve; });
    return consent.promise;
  }

  function closeConsent(result) {
    const resolve = consent.resolve;
    consent.resolve = null;
    consent.promise = null;
    consent.mode = null;
    if ($("iotConsent").open) $("iotConsent").close();
    if (resolve) resolve(result);
  }

  $("iotConsentCheck").addEventListener("change", () => { $("iotConsentEnable").disabled = !$("iotConsentCheck").checked; });
  $("iotConsentCancel").addEventListener("click", () => closeConsent(false));
  $("iotConsent").addEventListener("cancel", (event) => { event.preventDefault(); closeConsent(false); });   // Escape
  $("iotConsent").addEventListener("close", () => { if (consent.resolve) closeConsent(false); });
  document.querySelectorAll(".oa-consent-lang").forEach((b) => b.addEventListener("click", () => showStatement(b.dataset.lang)));
  $("iotConsentEnable").addEventListener("click", async () => {
    const button = $("iotConsentEnable");
    if (!$("iotConsentCheck").checked || !consent.doc) return;
    button.disabled = true;
    try {
      const result = await postAction("/api/build_f/iot/disclaimer", "iot-disclaimer", { accept: true, version: consent.doc.version });
      if (state) state.iot_disclaimer = result.iot_disclaimer;
      renderConsent(result.iot_disclaimer);
      closeConsent(true);
    } catch (error) {
      const box = $("iotConsentError");
      box.hidden = false;
      box.textContent = "Not recorded, IoT stays off: " + (error.message || error);
      button.disabled = !$("iotConsentCheck").checked;
    }
  });

  // the safety line in the Smart Home and IoT card: accepted (which version, when), held off, or not asked yet
  function renderConsent(c) {
    if (!c) return;
    const held = !c.accepted && Array.isArray(c.held) && c.held.length > 0;
    $("iotSafety").classList.toggle("held", held);
    $("iotConsentState").textContent = c.accepted
      ? "IoT safety statement version " + c.version + " accepted" + (c.accepted_at ? " on " + new Date(c.accepted_at).toLocaleString() : "") + "."
      : held && c.accepted_version
        ? "IoT is off: the IoT safety statement changed (you accepted version " + c.accepted_version + "; the current one is "
          + c.version + "). Read it and accept it to turn IoT back on."
        : held ? "IoT is off: " + c.held.join(", ") + " is switched on, but the IoT safety statement (version " + c.version
          + ") has not been accepted yet. Read it and accept it to turn IoT on."
          : "Turning IoT on shows the IoT safety statement first; IoT stays off unless you accept it.";
    const button = $("iotConsentOpen");
    button.textContent = held ? "Read and accept…" : "Read the IoT safety statement";
    button.classList.toggle("primary", held);
    button.dataset.mode = held ? "accept" : "view";
  }

  async function acceptHeld() {
    if (await openConsent("accept")) {
      toast("IoT safety statement accepted — IoT is on.");
      refresh();
      iotRefresh();
    }
  }
  $("iotConsentOpen").addEventListener("click", () => {
    if ($("iotConsentOpen").dataset.mode === "accept") acceptHeld();
    else openConsent("view");
  });

  // false when the person did not accept the statement: then nothing is saved and the cards show IoT off again
  // (the raw editor keeps what was typed in it)
  async function consentFirst() {
    const accepted = await openConsent("accept");
    if (accepted) return true;
    if (state) renderServers(state);
    if (accepted === false) toast("Nothing saved: IoT stays off.");
    return false;
  }

  async function save(cfg) {
    // a smart-home server switched on before the statement was accepted: the statement first; Cancel = nothing saved
    if (turnsOnIot(cfg) && !iotAccepted() && !(await consentFirst())) return;
    let result;
    try { result = await postAction("/api/build_f/tools/config", "tools-config", { config: cfg }); }
    catch (error) {
      if (error.code !== "iot_disclaimer_required") throw error;
      if (!(await consentFirst())) return;        // the server knew better (a new entry, or another tab): same dialog
      result = await postAction("/api/build_f/tools/config", "tools-config", { config: cfg });
    }
    apply(result);
    toast("Tool settings saved.");
  }

  function apply(s) {
    state = s;
    renderConsent(s.iot_disclaimer);
    // switched on but held off (an install from before the statement, or a new version of it): ask once per visit
    const c = s.iot_disclaimer;
    if (c && !c.accepted && Array.isArray(c.held) && c.held.length && !consent.autoShown) {
      consent.autoShown = true;
      acceptHeld();
    }
    $("toolsEnabled").checked = !!s.enabled;
    $("clerkRole").value = s.clerk_role || "surface";
    const limits = s.limits || {};
    $("limCalls").value = limits.max_calls_per_turn || 3;
    $("limTimeout").value = limits.call_timeout_s || 20;
    $("limPoll").value = limits.poll_interval_s || 2.5;
    if (document.activeElement !== $("rawConfig")) {
      $("rawConfig").value = JSON.stringify(configFromState(s), null, 2);
    }
    renderServers(s);
    renderLog(s);
    renderResults(s.results || []);
  }

  const connectionActions = new Set();
  async function connectionAction(name, action, button) {
    if (connectionActions.has(name)) return;
    connectionActions.add(name);
    button.disabled = true;
    try {
      const result = await postAction("/api/build_f/tools/" + action, "tools-" + action, { server: name });
      apply(result);
      if (result.warning) toast(result.warning, true);
    } catch (error) { toast(error.message || String(error), true); }
    finally { connectionActions.delete(name); button.disabled = false; }
  }

  function renderResults(results) {
    const box = $("toolResults");
    const images = results.filter(r => (r.content || []).some(b => b.type === "image"));
    // Polls should not collapse an open preview or steal its focus.
    const signature = JSON.stringify(images);
    if (box.dataset.signature === signature) return;
    box.dataset.signature = signature;
    box.hidden = !images.length;
    box.replaceChildren();
    if (!images.length) return;
    box.append(el("h3", { text: "Tool images" }));
    for (const result of images.slice().reverse()) {
      const details = el("details", { class: "oa-details" }, el("summary", { text: result.server + " · " + result.tool }));
      for (const block of result.content) {
        if (block.type !== "image" || !/^\/api\/build_f\/tools\/attachments\/[a-f0-9]{32}$/.test(block.url || "")) continue;
        details.append(el("a", { href: block.url, target: "_blank", rel: "noopener noreferrer", text: "Open image ↗" }),
          el("img", { src: block.url, alt: "Image returned by " + result.tool, loading: "lazy", style: "display:block;max-width:100%;max-height:420px;object-fit:contain;margin:12px 0;" }),
          el("p", { class: "card-hint", text: block.analysis ? "Vision: " + block.analysis : "Image retained; visual analysis unavailable." }));
      }
      box.append(details);
    }
  }

  function renderServers(s) {
    const box = $("serversBox");
    const active = document.activeElement;
    if ((box.contains(active) || $("iotServer").contains(active)) && active.matches("input, textarea, select")) return;
    const focusKey = active && ["data-connect", "data-disconnect", "data-auth"].find(key => active.hasAttribute(key));
    const focusName = focusKey ? active.getAttribute(focusKey) : null;
    box.replaceChildren();
    const entries = Object.entries(s.servers || {});
    if (!entries.length) {
      box.append(el("section", { class: "tech-card" },
        el("div", { class: "card-hint", text: "No servers yet. To connect Hermes Agent or nanoMuse, copy config/tools.example.json to config/tools.json and fill in its hermes or nanomuse entry (README, “Her helpers”; docs/TOOLS.md has every step), then reload this page — or switch to Professional (top right) to add a server here. The smart-home server lives in servers/openamika-iot." })));
      return;
    }
    for (const [name, server] of entries) {
      const card = el("section", { class: "tech-card server-card" });
      const toggle = el("input", { type: "checkbox", "data-server-toggle": name });
      // a smart-home server held off by the IoT safety statement shows as off: it is off until the statement is accepted
      toggle.checked = !!server.enabled && !server.blocked;
      toggle.addEventListener("change", () => save(mergedConfig()).catch((e) => toast(String(e.message || e), true)));
      // dot: grey = disabled · amber = enabled but the bridge is not up · red = bridge up, the agent itself unreachable · green = all good
      const health = server.health || null;
      const agentDown = !!(server.connected && health && health.ok === false);
      const starting = agentDown && health.reason === "starting";
      const startFailed = agentDown && health.reason === "start_failed";
      const refusing = agentDown && health.reason === "refused";      // up and authenticated, but it would not take her last task
      const noModel = agentDown && health.reason === "no_model";      // up, but its own model has no working key
      // Start only for an agent that is not up: one that answers (refusing tasks, no model, a rejected key) is running
      // already, and a second copy would only fail on its port
      const canStart = server.kind === "agent" && server.enabled && server.autostart && agentDown && !starting
        && !refusing && !noModel && health.reason !== "unauthorized";
      const head = el("div", { class: "server-head" },
        el("span", { class: "dot " + (!server.enabled ? "" : !server.connected ? "warn" : starting ? "warn" : agentDown ? "err" : "ok") }),
        el("span", { class: "name", text: name }),
        el("span", { class: "tag", text: server.kind === "agent" ? "agent" : server.transport }),
        health ? el("span", { class: "tag", style: starting ? "color:var(--warn);" : agentDown ? "color:var(--err);" : "",
          text: starting ? "agent starting…" : startFailed ? "start failed" : refusing ? "agent refusing tasks"
            : noModel ? "agent has no model" : agentDown ? "agent unreachable"
            : "agent online" + (health.version ? " · v" + health.version : "") }) : "",
        canStart ? el("button", {
          class: "btn small primary", text: "Start",
          onclick: async (event) => {
            event.preventDefault();
            toast("Starting " + name + "…");
            try {
              const result = await postAction("/api/build_f/tools/start", "tools-start", { server: name });
              toast(result.started ? name + " launched — the card turns green when it answers." : name + ": " + ((result.health || {}).detail || "not started"), !result.started);
              refresh();
            } catch (error) { toast(name + ": " + (error.message || error), true); }
          },
        }) : "",
        el("span", { class: "target mono", text: server.target || "" }),
        el("label", { class: "switch" }, toggle, el("span", { class: "track" })),
        el("button", {
          class: "btn small", text: "Test",
          onclick: async (event) => {
            event.preventDefault();
            toast("Connecting " + name + "…");
            try {
              const result = await postAction("/api/build_f/tools/test", "tools-test", { server: name });
              const h = result.health;
              const agentNote = h ? (h.ok ? " · agent online" + (h.version ? " v" + h.version : "") : " · agent UNREACHABLE: " + (h.detail || "")) : "";
              toast(name + ": " + (result.tools || []).length + " tool(s) discovered" + (result.kind === "agent" ? " (agent bridge)" : "") + agentNote + ".", !!(h && !h.ok));
              refresh();
            } catch (error) { toast(name + ": " + (error.message || error), true); }
          },
        }),
        el("button", {
          class: "btn small danger", text: "Remove", "data-pro": "",   // deleting a server from tools.json: Professional only
          onclick: async (event) => {
            event.preventDefault();
            if (!window.confirm("Remove server '" + name + "' from tools.json?")) return;
            const cfg = mergedConfig();
            delete cfg.mcpServers[name];
            save(cfg).catch((e) => toast(String(e.message || e), true));
          },
        }));
      card.append(head);
      if (server.transport === "http") {
        const busy = ["connecting", "authorizing"].includes(server.connection_state);
        const row = el("div", { class: "form-row", style: "margin-top:10px;" });
        const connect = el("button", { class: "btn small", text: busy ? "Connecting…" : server.connection_state === "auth_required" ? "Sign in" : server.connected ? "Reconnect" : "Connect",
          "data-connect": name, onclick: event => connectionAction(name, "connect", event.currentTarget) });
        connect.disabled = busy || connectionActions.has(name);
        row.append(connect);
        if (server.connected || busy || server.connection_state === "auth_required") row.append(el("button", {
          class: "btn small", text: busy ? "Cancel connection" : "Disconnect account", "data-disconnect": name,
          onclick: event => connectionAction(name, "disconnect", event.currentTarget) }));
        if (server.protocol_version) row.append(el("span", { class: "card-hint", text: "MCP " + server.protocol_version }));
        card.append(row);
        const auth = server.authorization;
        if (auth && auth.url) {
          let url;
          try { url = new URL(auth.url); } catch (_) { url = null; }
          if (url && ["http:", "https:"].includes(url.protocol)) card.append(el("div", { class: "card-hint" },
            el("p", { text: "Sign in with " + (auth.issuer || url.hostname) + (auth.scope ? " · Permissions: " + auth.scope : "") }),
            el("a", { href: auth.url, target: "_blank", rel: "noopener noreferrer", text: "Continue to sign in ↗", "data-auth": name })));
        }
      }
      if (server.blocked) card.append(el("div", { class: "card-hint", style: "color:var(--warn);margin-top:8px;",
        text: "Switched on in tools.json, but held off until the IoT safety statement is accepted (Smart Home and IoT card)." }));
      else if (server.error) card.append(el("div", { class: "card-hint", style: "color:var(--err);margin-top:8px;", text: server.error }));
      if (starting) card.append(el("div", { class: "card-hint", style: "margin-top:8px;", text: (health.detail || "starting…") + " — this card updates by itself." }));
      else if (agentDown) card.append(el("div", { class: "card-hint", style: "color:var(--err);margin-top:8px;",
        text: (health.detail || "The bridge is up but the agent itself does not answer.")
          + (refusing ? " She is told it is not taking tasks and will not promise it anything."
            : noModel ? " She is told it cannot do anything until its model is set up, and will not promise it anything."
            : " She is told it is unreachable and will not promise it anything.") }));
      if (health && health.ok && health.note) card.append(el("div", { class: "card-hint", style: "margin-top:8px;", text: health.note }));
      if (server.kind === "agent") {
        const startField = el("div", { class: "field", style: "margin-top:10px;", "data-pro": "" },   // Professional only
          el("label", { text: "Start command — run automatically when the agent is not running (leave empty to manage it yourself)" }));
        const startInput = el("input", { type: "text", class: "mono", "data-autostart": name, spellcheck: "false",
          placeholder: name === "nanomuse" ? "C:\\OpenAmika\\Nanomuse\\venv\\Scripts\\nanomuse.exe serve --config C:\\OpenAmika\\Nanomuse\\data\\config.toml --no-qr --host 127.0.0.1 --port 8787" : "%LOCALAPPDATA%\\hermes\\bin\\hermes.exe gateway" });
        startInput.value = joinCommand(server.autostart);
        startField.append(startInput);
        card.append(startField);
      }
      // what she is told about this server (in character, Chinese like the rest of her prompts)
      const noteField = el("div", { class: "field", style: "margin-top:10px;", "data-pro": "" },   // Professional only
        el("label", { text: "What she knows about it (goes into her prompt when enabled; {user_name} = your name)" }));
      const area = el("textarea", { class: "mono", rows: "3", "data-note": name, spellcheck: "false",
        placeholder: server.kind === "agent" ? "e.g. 赫尔墨斯——{user_name}装在他电脑上的小帮手程序，能替你上网查东西、看他的屏幕、读写邮件、网上下单……" : "e.g. 家里的灯和插座，可以开关、查状态" });
      area.value = server.persona_note || "";
      noteField.append(area);
      card.append(noteField);
      if (server.tools && server.tools.length) {
        const table = el("table", { class: "oa-table", style: "margin-top:10px;" });
        table.append(el("thead", {}, el("tr", {}, el("th", { text: "Tool" }), el("th", { text: "Description" }), el("th", { text: "Policy" }))));
        const body = el("tbody");
        for (const tool of server.tools) {
          const select = el("select", { class: "policy-select", "data-policy-server": name, "data-policy-tool": tool.name });
          for (const policy of ["auto", "confirm", "deny"]) select.append(el("option", { value: policy, text: policy }));
          select.value = tool.policy || "confirm";
          select.addEventListener("change", () => save(mergedConfig()).catch((e) => toast(String(e.message || e), true)));
          body.append(el("tr", {}, el("td", { text: tool.name }), el("td", { text: (tool.description || "").slice(0, 160) }), el("td", {}, select)));
        }
        table.append(body);
        card.append(el("div", { class: "table-scroll", "data-pro": "" }, table));   // per-tool policies: Professional only
      } else if (server.enabled && !server.blocked) {
        card.append(el("div", { class: "card-hint", style: "margin-top:8px;", text: "No tools discovered yet — press Test to connect." }));
      }
      // the smart-home server lives inside the Smart Home and IoT panel, above its devices, without its own card chrome
      if (name === "iot") { card.classList.add("embedded"); $("iotServer").replaceChildren(card); }
      else box.append(card);
    }
    if (focusKey) {
      const replacement = [...box.querySelectorAll("[" + focusKey + "]")].find(node => node.getAttribute(focusKey) === focusName);
      if (replacement && !replacement.disabled) replacement.focus({ preventScroll: true });
    }
  }

  function renderLog(s) {
    const body = $("logBody");
    const log = (s.log || []).slice().reverse();
    body.replaceChildren();
    if (!log.length) {
      body.append(el("tr", {}, el("td", { colspan: "5", class: "card-hint", text: "No tool calls yet." })));
      return;
    }
    for (const entry of log) {
      const when = new Date((entry.ts || 0) * 1000);
      const outcome = entry.ok === null ? "pending" : entry.ok ? "ok"
        : (entry.policy === "refused" ? "refused" : entry.policy === "unreachable" ? "not sent" : "error");
      const detail = (entry.result || "").slice(0, 120) + (entry.quote ? "  [you said: " + String(entry.quote).slice(0, 60) + "]" : "");
      body.append(el("tr", {},
        el("td", { text: when.toLocaleTimeString() }),
        el("td", { text: entry.server + "__" + entry.tool }),
        el("td", {}, el("code", { text: JSON.stringify(entry.arguments || {}).slice(0, 80) })),
        el("td", { text: outcome + (entry.policy === "confirmed" ? " (approved)" : "") }),
        el("td", { text: detail })));
    }
  }

  $("toolsSave").addEventListener("click", () => save(mergedConfig()).catch((e) => toast(String(e.message || e), true)));
  $("rawSave").addEventListener("click", () => {
    let raw;
    try { raw = JSON.parse($("rawConfig").value); }
    catch (error) { toast("Raw config is not valid JSON: " + error.message, true); return; }
    save(raw).catch((e) => toast(String(e.message || e), true));
  });

  $("addTransport").addEventListener("change", () => {
    const http = $("addTransport").value !== "stdio";
    $("addTargetLabel").textContent = http ? "Server URL" : "Command + args (first word = command)";
    $("addTarget").placeholder = http ? "https://example.com/mcp" : "python -m openamika_agents hermes";
  });
  $("addBtn").addEventListener("click", () => {
    const name = $("addName").value.trim();
    const target = $("addTarget").value.trim();
    if (!/^[A-Za-z0-9_-]{1,32}$/.test(name)) { toast("Name: letters, digits, - or _ only.", true); return; }
    if (!target) { toast("Target is required.", true); return; }
    const cfg = mergedConfig();
    if (cfg.mcpServers[name]) { toast("A server with that name exists.", true); return; }
    if ($("addTransport").value !== "stdio") {
      cfg.mcpServers[name] = { url: target, transport: $("addTransport").value, enabled: false };
    } else {
      const parts = target.split(/\s+/);
      cfg.mcpServers[name] = { command: parts[0], args: parts.slice(1), enabled: false };
      if (/openamika_agents/.test(target)) { cfg.mcpServers[name].kind = "agent"; cfg.mcpServers[name].cwd = "../servers/openamika-agents"; }
    }
    save(cfg).then(() => { $("addName").value = ""; $("addTarget").value = ""; })
      .catch((e) => toast(String(e.message || e), true));
  });

  // ── smart home: the plugs by hand (brain /api/build_f/iot — she reaches the same server through MCP) ──
  const iot = { timer: null, cards: new Map(), permit: false };
  const iotNum = (v, digits) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) : null);
  function iotAgo(epoch) {
    const s = Math.max(0, Date.now() / 1000 - (Number(epoch) || 0));
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  const iotClock = (epoch) => new Date((Number(epoch) || 0) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  async function iotPost(url, action, body) {
    try { return await postAction(url, action, body); }
    catch (error) { toast(String(error.message || error), true); return null; }   // 409 while she speaks: the detail says so, just retry
  }
  function iotLight(d) {                 // [dot class, text to show]
    if (!d || !d.enabled) return ["", "the smart-home server is off — flip its switch above (Professional mode adds one if it is missing)"];
    if (d.blocked) return ["warn", "Until the statement is accepted, no smart-home server is started and no device is switched."];
    if (!d.connected) return ["err", (d.health && d.health.detail) || "not connected"];
    if (d.health && d.health.ok === false) return ["warn", d.health.detail || d.health.reason || "degraded"];
    return ["ok", ""];
  }
  // one card per device, built once and updated in place so a nickname being typed survives the refresh
  function iotCard(dev) {
    const key = dev.friendly_name;
    const card = el("div", { class: "iot-dev" });
    const name = el("b"), sub = el("div", { class: "card-hint" }), state = el("span", { class: "iot-state" });
    const readings = el("div", { class: "iot-readings mono" }), seen = el("div", { class: "card-hint" });
    const flags = el("div", { class: "card-hint mono", "data-pro": "" });
    const actions = el("div", { class: "iot-actions" });
    const setState = (st) => {
      const s = String(st || "unknown").toUpperCase();
      state.textContent = s;
      state.className = "iot-state " + (s === "ON" ? "on" : s === "OFF" ? "off" : "unknown");
    };
    const switchTo = async (action) => {
      actions.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      setState(action === "toggle" ? (state.textContent === "ON" ? "OFF" : "ON") : action);   // optimistic; the refresh tells the truth
      const result = await iotPost("/api/build_f/iot/device", "iot-device", { device: key, action });
      const r = result && result.result;
      if (r && r.ok === false) toast(r.error || r.message || "The plug did not answer.", true);
      actions.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      iotRefresh();
    };
    for (const [label, action, cls] of [["On", "on", "btn small primary"], ["Off", "off", "btn small"], ["Toggle", "toggle", "btn small"]]) {
      actions.append(el("button", { class: cls, type: "button", text: label, onclick: () => switchTo(action) }));
    }
    const nick = el("input", { type: "text", placeholder: "Nickname — what you call it", maxlength: "40" });
    const note = el("input", { type: "text", placeholder: "What is it? (Amika reads this)", maxlength: "200" });
    for (const input of [nick, note]) input.addEventListener("input", () => { card.dataset.dirty = "yes"; });
    const save = el("button", { class: "btn small", type: "button", text: "Save", onclick: async () => {
      const was = card._dev || {};
      let ok = true;
      if (nick.value.trim() !== (was.nickname || "")) ok = !!(await iotPost("/api/build_f/iot/device", "iot-device", { device: key, nickname: nick.value.trim() })) && ok;
      if (note.value.trim() !== (was.note || "")) ok = !!(await iotPost("/api/build_f/iot/device", "iot-device", { device: key, note: note.value.trim() })) && ok;
      if (ok) { card.dataset.dirty = "no"; toast("Saved — she knows it as “" + (nick.value.trim() || key) + "”."); }
      iotRefresh();
    } });
    card.update = (d) => {
      card._dev = d;
      name.textContent = d.nickname || d.friendly_name;
      sub.textContent = [d.vendor, d.model].filter(Boolean).join(" ") + (d.description ? " — " + d.description : "");
      setState(d.state);
      readings.textContent = [[d.power, 1, " W"], [d.voltage, 0, " V"], [d.current, 2, " A"], [d.energy, 2, " kWh"], [d.temperature, 0, " °C"]]
        .map(([v, digits, unit]) => { const t = iotNum(v, digits); return t === null ? null : t + unit; }).filter(Boolean).join(" · ");
      readings.hidden = !readings.textContent;
      // availability is only known when zigbee2mqtt's availability feature is on; "unknown" would just look broken
      const avail = d.availability === "online" || d.availability === "offline" ? d.availability : null;
      seen.textContent = [avail, d.last_seen ? "seen " + iotAgo(d.last_seen) : null].filter(Boolean).join(" · ");
      flags.textContent = [d.ieee, d.supported === false ? "unsupported" : d.supported ? "supported" : null,
        d.interview_completed === false ? "interview pending" : null].filter(Boolean).join(" · ");
      card.classList.toggle("offline", d.availability === "offline");
      if (card.dataset.dirty !== "yes" && document.activeElement !== nick && document.activeElement !== note) {
        nick.value = d.nickname || "";
        note.value = d.note || "";
      }
    };
    card.append(
      el("div", { class: "iot-head" }, el("div", { class: "iot-name" }, name, sub), state),
      actions, readings, seen,
      el("div", { class: "iot-edit" }, nick, el("div", { class: "row" }, note, save)),
      flags);
    card.update(dev);
    return card;
  }
  function renderIot(d) {
    const [level, detail] = iotLight(d);
    $("iotDot").className = "dot " + level;
    if (d && d.iot_disclaimer) renderConsent(d.iot_disclaimer);
    const devices = d && d.enabled && Array.isArray(d.devices) ? d.devices : [];
    const on = devices.filter((x) => String(x.state).toUpperCase() === "ON").length;
    $("iotSub").textContent = !d || !d.enabled ? "no smart-home server enabled" : d.blocked ? "off · safety statement not accepted"
      : !d.connected ? "not connected"
        : devices.length + (devices.length === 1 ? " device" : " devices") + (devices.length ? " · " + on + " on" : "");
    const hint = $("iotHint");
    hint.style.color = level === "err" ? "var(--err)" : level === "warn" ? "var(--warn)" : "";
    hint.textContent = !d || !d.enabled ? detail : detail || (!devices.length ? "No devices yet — put a plug into pairing mode and press Find new devices." : "");
    hint.hidden = !hint.textContent;
    const box = $("iotDevices"), present = new Set();
    for (const dev of devices) {
      const key = dev.friendly_name || dev.ieee;
      if (!key) continue;
      present.add(key);
      let card = iot.cards.get(key);
      if (!card) { card = iotCard({ ...dev, friendly_name: key }); iot.cards.set(key, card); box.append(card); }
      else card.update(dev);
    }
    for (const [key, card] of iot.cards) if (!present.has(key)) { card.remove(); iot.cards.delete(key); }
    // pairing: Find opens permit-join for ~4 minutes; the note counts down from permit_join_end
    const disc = (d && d.discovery) || {};
    iot.permit = !!disc.permit_join;
    const left = iot.permit && disc.permit_join_end ? Math.max(0, Math.round(Number(disc.permit_join_end) - Date.now() / 1000)) : 0;
    $("iotPairBtn").hidden = iot.permit;
    $("iotPairBtn").disabled = !d || !d.enabled || !d.connected;
    $("iotPairStop").hidden = !iot.permit;
    $("iotPairNote").textContent = iot.permit
      ? "Pairing is open" + (left ? " — " + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") + " left" : "") + ". Put the new plug into pairing mode now."
      : disc.zigbee2mqtt_online === false && d && d.connected ? "zigbee2mqtt is offline — check the launcher console."
      : "Put a new plug into pairing mode, then press Find.";
    const events = (Array.isArray(disc.events) ? disc.events : []).slice().sort((a, b) => (b.at || 0) - (a.at || 0)).slice(0, 8);
    $("iotEvents").replaceChildren(...events.map((e, i) =>
      el("div", { class: i === 0 ? "fresh" : "", text: [iotClock(e.at), e.type, e.device, e.status, e.description].filter(Boolean).join(" ") })));
  }
  async function iotRefresh() {
    clearTimeout(iot.timer); iot.timer = null;
    if (document.hidden) return;                                   // resumes on visibilitychange
    try { renderIot(await getJSON("/api/build_f/iot")); }
    catch (error) {
      $("iotDot").className = "dot err";
      $("iotSub").textContent = "unavailable";
      const hint = $("iotHint");
      hint.hidden = false; hint.style.color = "var(--err)";
      hint.textContent = "Smart home status unavailable: " + (error.message || error) + " — is the brain running?";
    }
    iot.timer = setTimeout(iotRefresh, iot.permit ? 1500 : 3000);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !iot.timer) iotRefresh(); });
  $("iotPairBtn").addEventListener("click", async () => {
    $("iotPairBtn").disabled = true;
    if (await iotPost("/api/build_f/iot/pairing", "iot-pairing", { enable: true, seconds: 254 })) toast("Pairing is open for about four minutes.");
    iotRefresh();
  });
  $("iotPairStop").addEventListener("click", async () => {
    if (await iotPost("/api/build_f/iot/pairing", "iot-pairing", { enable: false })) toast("Pairing closed.");
    iotRefresh();
  });
  iotRefresh();

  async function refresh() {
    try { apply(await getJSON("/api/build_f/tools")); }
    catch (error) {
      $("serversBox").replaceChildren(el("section", { class: "tech-card" },
        el("div", { class: "card-hint", text: "Tool layer unavailable: " + (error.message || error) + " — is the brain running?" })));
    }
  }
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
