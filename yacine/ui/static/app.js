/* Y.A.C.I.N.E — Claude-style UI client. Vanilla JS, no build step. */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const AGENT_LABELS = {
    architect: "🧠 Architect (deepseek-r1:7b)",
    orchestrator: "📋 Orchestrator (llama3.1:8b)",
    executioner: "⚙️ Executioner (qwen2.5-coder:7b)",
    frontend: "💬 Frontend (qwen2.5:3b)",
  };

  // ---- State: chats + projects, each with its own working folder ----
  const state = {
    view: "chats",            // 'chats' | 'projects'
    chats: [],                // {id, title, folder, messages:[{role,text}], projectId}
    projects: [],             // {id, name, folder}
    activeId: null,
    panelOpen: true,
  };
  let nextId = 1;
  const uid = () => "c" + (nextId++) + "_" + Date.now().toString(36);

  function activeChat() {
    return state.chats.find((c) => c.id === state.activeId) || null;
  }

  // ---- Markdown ----
  function renderMarkdown(text, target) {
    try { target.innerHTML = marked.parse(text || ""); }
    catch (e) { target.textContent = text || ""; }
  }
  function escapeHtml(s) {
    return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  // ---- Chat rendering ----
  const chatEl = $("#chat");
  function addMessage(role, text, chatObj) {
    const m = document.createElement("div");
    m.className = "msg " + (role === "user" ? "user" : "agent");
    m.innerHTML = `<div class="who">${role === "user" ? "You" : "Y.A.C.I.N.E"}</div><div class="bubble"></div>`;
    renderMarkdown(text, m.querySelector(".bubble"));
    chatEl.appendChild(m);
    chatEl.scrollTop = chatEl.scrollHeight;
    if (chatObj) chatObj.messages.push({ role, text });
    return m;
  }

  // ---- Left rail ----
  function renderRail() {
    const list = $("#rail-list");
    list.innerHTML = "";
    const items = state.view === "chats" ? state.chats : state.projects;
    const q = ($("#rail-search").value || "").toLowerCase();
    const filtered = items.filter((it) => {
      const t = (it.title || it.name || "").toLowerCase();
      return !q || t.includes(q);
    });
    if (!filtered.length) {
      list.innerHTML = `<div class="rail-empty">No ${state.view} yet.</div>`;
      return;
    }
    filtered.forEach((it) => {
      const row = document.createElement("div");
      row.className = "rail-item" + (it.id === state.activeId ? " active" : "");
      const title = state.view === "chats" ? it.title : it.name;
      const sub = state.view === "chats"
        ? (it.messages.length ? `${it.messages.filter(m=>m.role==='user').pop()?.text || ""}`.slice(0, 60)
                              : "Empty chat")
        : `${it.id}`;
      const folder = it.folder ? `📁 ${it.folder}` : "No folder";
      row.innerHTML = `<div class="ri-title">${escapeHtml(title)}</div>
                       <div class="ri-sub">${escapeHtml(sub)}</div>
                       <div class="ri-folder">${escapeHtml(folder)}</div>`;
      row.onclick = () => {
        if (state.view === "chats") openChat(it.id);
        else selectProjectFolder(it.id);
      };
      list.appendChild(row);
    });
  }

  function openChat(id) {
    state.activeId = id;
    const c = activeChat();
    $("#chat-title").textContent = c.title;
    $("#chat-meta").textContent = c.folder ? `📁 ${c.folder}` : "No folder selected";
    chatEl.innerHTML = "";
    c.messages.forEach((m) => addMessage(m.role, m.text, null));
    renderRail();
  }

  function newChat() {
    const c = { id: uid(), title: "New chat", folder: "", messages: [] };
    state.chats.unshift(c);
    openChat(c.id);
    $("#prompt").focus();
  }

  function selectProjectFolder(id) {
    const p = state.projects.find((x) => x.id === id);
    if (!p) return;
    openFolderModal((folder) => {
      p.folder = folder;
      // Attach a new chat scoped to this project's folder.
      const c = { id: uid(), title: p.name + " · chat", folder, messages: [], projectId: p.id };
      state.chats.unshift(c);
      state.view = "chats";
      $$(".rail-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === "chats"));
      openChat(c.id);
    });
  }

  // ---- Model select ----
  const modelSel = $("#model-select");
  async function loadModels() {
    try {
      const d = await (await fetch("/models")).json();
      const active = d.active || {};
      const opts = (d.available || []).map((m) =>
        `<option value="${m.provider}:${m.model}">${m.provider}:${m.model}</option>`);
      modelSel.innerHTML = opts.join("") || `<option>${active.provider}:${active.model}</option>`;
      const val = `${active.provider}:${active.model}`;
      if ([...modelSel.options].some((o) => o.value === val)) modelSel.value = val;
      setBadge(active.provider, active.model);
    } catch (_) { $("#model-badge").textContent = "model?"; }
  }
  function setBadge(p, m) { $("#model-badge").textContent = `${p}:${m}`; }
  modelSel.onchange = async () => {
    const [p, ...rest] = modelSel.value.split(":");
    const m = rest.join(":");
    try {
      const d = await (await fetch("/chat/model", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: p, model: m }),
      })).json();
      setBadge(d.active.provider, d.active.model);
    } catch (e) { console.error(e); }
  };

  // ---- Monitors ----
  function updateMonitors() {
    fetch("/api/monitor").then((r) => r.json()).then((d) => {
      const g = d.gpu || {};
      $("#mon-gpu").textContent = g.name ? `GPU ${g.mem_used_mb}/${g.mem_total_mb}MB` : "GPU —";
    }).catch(() => {});
  }
  // Poll hardware stats on a slow timer, and not at all when the window is
  // hidden or the browser is throttling background tabs. This used to fire
  // every 3s and spawn subprocesses on the server each time.
  setInterval(() => { if (!document.hidden) updateMonitors(); }, 8000);
  updateMonitors();

  // ---- Folder modal (Windows "Save As" style) ----
  let folderCallback = null;
  let folderCurrent = "";
  let folderSelected = null;       // explicit selection; null = use current dir
  let homePath = "";               // captured ONCE from the initial /api/browse (real home dir)
  let homeCaptured = false;        // guard so we never re-capture after navigating
  const history = [];              // breadcrumb-style back stack
  const QUICK = [                  // Quick access shortcuts (left pane)
    { name: "Desktop", sub: "Desktop" },
    { name: "Documents", sub: "Documents" },
    { name: "Downloads", sub: "Downloads" },
    { name: "Pictures", sub: "Pictures" },
  ];
  function renderQuick() {
    const q = $("#fp-quick");
    if (!q) return;
    q.innerHTML = "";
    QUICK.forEach((item) => {
      const row = document.createElement("div");
      row.className = "fp-quick-row";
      row.innerHTML = `<span class="ic">📁</span><span class="name">${escapeHtml(item.name)}</span>`;
      row.onclick = () => { history.push(folderCurrent); browseTo(homePath + "/" + item.sub); };
      q.appendChild(row);
    });
  }
  function openFolderModal(cb) {
    folderCallback = cb;
    folderCurrent = "";
    folderSelected = null;
    history.length = 0;
    const nf = $("#folder-newfolder");
    if (nf) nf.style.display = "none";
    const nfn = $("#newfolder-name");
    if (nfn) nfn.value = "";
    const fs = $("#fp-search");
    if (fs) fs.value = "";
    renderQuick();
    $("#folder-modal").hidden = false;
    browseTo("");
  }
  function selectRow(path) {
    folderSelected = path;
    $$("#folder-list .folder-row").forEach((r) =>
      r.classList.toggle("selected", r.dataset.path === path));
    $("#folder-current").textContent = path || folderCurrent;
  }
  async function browseTo(path) {
    const body = JSON.stringify({ path: path || null });
    const d = await (await fetch("/api/browse", {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    })).json();
    folderCurrent = d.path || "";
    folderSelected = null;
    // Capture the real home dir EXACTLY ONCE — only on the initial open (empty path).
    // This prevents re-capturing a navigated path (which would corrupt Quick access).
    if (!homeCaptured && (!path || path === "" || path === null)) {
      const segs = folderCurrent.split(/[\\/]/).filter(Boolean);
      if (segs.length >= 1) { homePath = segs.join("/"); homeCaptured = true; }
    }
    $("#folder-current").textContent = folderCurrent || "—";
    // breadcrumb address bar
    const bc = $("#folder-breadcrumb");
    bc.innerHTML = "";
    const parts = folderCurrent.split(/[\\/]/).filter(Boolean);
    let acc = "";
    parts.forEach((part, i) => {
      acc += (acc ? "/" : "") + part;
      const seg = acc;
      const crumb = document.createElement("span");
      crumb.className = "fp-crumb";
      crumb.textContent = part;
      crumb.onclick = () => browseTo(seg);
      bc.appendChild(crumb);
      if (i < parts.length - 1) {
        const sep = document.createElement("span");
        sep.className = "fp-sep"; sep.textContent = "›";
        bc.appendChild(sep);
      }
    });
    // list
    const list = $("#folder-list");
    list.innerHTML = "";
    if (d.error) {
      const err = document.createElement("div");
      err.className = "fp-empty";
      err.textContent = "Could not open this folder: " + d.error;
      list.appendChild(err);
      return;
    }
    if (d.parent) {
      const up = document.createElement("div");
      up.className = "folder-row up-row";
      up.innerHTML = `<span class="ic">↩</span><span class="name">.. (up)</span><span class="type"></span>`;
      up.onclick = () => { history.push(folderCurrent); browseTo(d.parent); };
      list.appendChild(up);
    }
    (d.entries || []).forEach((e) => {
      const row = document.createElement("div");
      row.className = "folder-row" + (e.is_dir ? " dir" : " file");
      row.dataset.path = e.path;
      row.innerHTML = `<span class="ic">${e.is_dir ? "📁" : "📄"}</span>` +
        `<span class="name">${escapeHtml(e.name)}</span>` +
        `<span class="type">${e.is_dir ? "Folder" : "File"}</span>`;
      if (e.is_dir) {
        // Single click: navigate INTO the folder AND mark it selected.
        row.onclick = () => { history.push(folderCurrent); browseTo(e.path); selectRow(e.path); };
        // Double click: explicitly confirm selection (same target).
        row.ondblclick = () => selectRow(e.path);
      }
      list.appendChild(row);
    });
    // The directory you're currently viewing is always the selection target.
    selectRow(folderCurrent);
  }
  $("#fp-back").onclick = () => { if (history.length) browseTo(history.pop()); };
  $("#fp-up").onclick = () => {
    // go to parent of current
    const parent = folderCurrent.split(/[\\/]/).slice(0, -1).join("/") || folderCurrent;
    if (parent !== folderCurrent) { history.push(folderCurrent); browseTo(parent); }
  };
  $("#fp-search").addEventListener("input", (ev) => {
    const q = ev.target.value.trim().toLowerCase();
    $$("#folder-list .folder-row").forEach((r) => {
      const n = (r.querySelector(".name")?.textContent || "").toLowerCase();
      r.style.display = (!q || n.includes(q)) ? "" : "none";
    });
  });
  $("#folder-close").onclick = () => { $("#folder-modal").hidden = true; };
  $("#folder-choose").onclick = () => {
    $("#folder-modal").hidden = true;
    const chosen = folderSelected || folderCurrent;
    if (folderCallback) folderCallback(chosen);
    folderCallback = null;
  };
  // Drag-to-resize handle (bottom-right).
  (function () {
    const handle = $("#fp-resize");
    const modal = document.querySelector(".folder-picker");
    if (!handle || !modal) return;
    let sx = 0, sy = 0, sw = 0, sh = 0, dragging = false;
    handle.addEventListener("mousedown", (e) => {
      dragging = true; sx = e.clientX; sy = e.clientY;
      sw = modal.offsetWidth; sh = modal.offsetHeight;
      e.preventDefault();
      document.body.style.cursor = "nwse-resize";
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const w = Math.max(420, sw + (e.clientX - sx));
      const h = Math.max(320, sh + (e.clientY - sy));
      modal.style.width = w + "px";
      modal.style.height = h + "px";
      modal.style.maxHeight = "92vh";
    });
    window.addEventListener("mouseup", () => {
      if (dragging) { dragging = false; document.body.style.cursor = ""; }
    });
  })();
  $("#folder-new").onclick = () => {
    const nf = $("#folder-newfolder");
    if (!nf) return;
    nf.style.display = nf.style.display === "none" ? "flex" : "none";
    if (nf.style.display === "flex") $("#newfolder-name").focus();
  };
  async function createFolder() {
    const name = $("#newfolder-name").value.trim();
    if (!name) return;
    const r = await (await fetch("/api/mkdir", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent: folderCurrent, name }),
    })).json();
    if (r.ok) {
      $("#newfolder-name").value = "";
      const nf = $("#folder-newfolder");
      if (nf) nf.style.display = "none";
      await browseTo(r.path);
      selectRow(r.path);
    } else {
      $("#folder-current").textContent = "Error: " + (r.error || "create failed");
    }
  }
  $("#newfolder-btn").onclick = createFolder;
  $("#newfolder-name").addEventListener("keydown", (e) => { if (e.key === "Enter") createFolder(); });
  $("#pick-folder").onclick = () => openFolderModal((folder) => {
    const c = activeChat();
    if (!c) { newChat(); }
    activeChat().folder = folder;
    $("#chat-meta").textContent = `📁 ${folder}`;
    renderRail();
  });

  // ---- Right panel ----
  $("#toggle-panel").onclick = () => {
    state.panelOpen = !state.panelOpen;
    $("#panel").classList.toggle("collapsed", !state.panelOpen);
    $("#toggle-panel").textContent = state.panelOpen ? "⮞ Panel" : "⮜ Panel";
  };
  $$(".panel-tab").forEach((t) => t.onclick = () => {
    $$(".panel-tab").forEach((x) => x.classList.remove("active"));
    $$(".panel-pane").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $(`.panel-pane[data-ppane="${t.dataset.ptab}"]`).classList.add("active");
  });
  let thinkingBlocks = {};
  function renderThinking(source, message) {
    let block = thinkingBlocks[source];
    if (!block || !block.isConnected) {
      $("#thinking").innerHTML = "";
      block = document.createElement("div");
      block.className = "think-block think-" + source;
      block.innerHTML = `<div class="think-head">${AGENT_LABELS[source] || source}</div><div class="think-body"></div>`;
      $("#thinking").appendChild(block);
      thinkingBlocks[source] = block;
    }
    block.querySelector(".think-body").textContent = message;
    const pane = $(".panel-pane[data-ppane='thinking']");
    if (pane) pane.scrollTop = pane.scrollHeight;
  }
  function logEvent(ev) {
    const row = document.createElement("div");
    row.className = "event-row " + (ev.kind || "");
    row.innerHTML = `<span class="src">[${ev.source || "?"}]</span> ${escapeHtml(ev.message || "")}`;
    $("#event-log").appendChild(row);
    if (ev.kind === "reasoning" && ev.source && AGENT_LABELS[ev.source]) renderThinking(ev.source, ev.message);
  }

  // ---- Send (fixed: pipelineOn is now declared) ----
  const taskOn = null; // reserved
  const pipelineOn = $("#pipeline-on");
  const promptEl = $("#prompt");
  const sendBtn = $("#send");
  const statusbar = null;
  function setRunning(on) {
    sendBtn.disabled = on;
    promptEl.disabled = on;
    sendBtn.textContent = on ? "…" : "↑";
    const stop = $("#stop");
    if (stop) stop.hidden = !on;
  }

  async function sendMessage() {
    const request = promptEl.value.trim();
    if (!request) return;
    let c = activeChat();
    if (!c) { newChat(); c = activeChat(); }
    if (c.title === "New chat") {
      c.title = request.slice(0, 48);
      $("#chat-title").textContent = c.title;
      renderRail();
    }
    addMessage("user", request, c);
    promptEl.value = "";
    setRunning(true);

    if (pipelineOn.checked) {
      await runPipeline(request, c);
      setRunning(false);
      return;
    }

    // Normal chat, streamed. Parse SSE manually: lines "event: X" then
    // "data: Y" (data is the raw string payload, NOT json).
    const bubble = addMessage("agent", "▍", c);
    const bodyEl = bubble.querySelector(".bubble");
    let full = "";
    try {
      const resp = await fetch("/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: "chat", message: request, project_path: c.folder || null }),
      });
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let curEvent = "";
      const flush = () => {
        if (curEvent === "token") { full += buf; bodyEl.textContent = full + "▍"; chatEl.scrollTop = chatEl.scrollHeight; }
        else if (curEvent === "done") {
          try { const d = JSON.parse(buf); if (d.response) full = d.response; } catch (_) {}
        }
        buf = "";
      };
      let pending = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        // Do NOT clear `buf` here: an SSE frame can be split across chunks, and
        // resetting per chunk silently dropped the first half of long events.
        const chunk = pending + dec.decode(value, { stream: true });
        const lines = chunk.split("\n");
        pending = lines.pop();          // keep any partial trailing line
        // Process line by line.
        for (const raw of lines) {
          const line = raw.replace(/\r$/, "");
          if (line.startsWith("event:")) { curEvent = line.slice(6).trim(); }
          else if (line.startsWith("data:")) { buf += line.slice(5).trim(); }
          else if (line === "") { flush(); curEvent = ""; }  // blank line = end of event
        }
      }
      flush();
      bodyEl.textContent = full;
      renderMarkdown(full, bodyEl);
      c.messages.push({ role: "agent", text: full });
    } catch (e) {
      bodyEl.textContent = "Error: " + e.message;
    } finally {
      setRunning(false);
    }
  }

  // ---- Progress panel renderer ----
  const AGENT_INFO = {
    architect:    { who: "Architect",    role: "deepseek-r1:7b · plans the project" },
    orchestrator: { who: "Orchestrator", role: "llama3.1:8b · breaks plan into tasks" },
    executioner:  { who: "Executioner",  role: "qwen2.5-coder:7b · writes & verifies code" },
    frontend:     { who: "Frontend",     role: "qwen2.5:3b · summarizes results" },
  };
  function resetProgress() {
    $("#progress-pane").innerHTML =
      `<div class="prog-bar-wrap"><div class="prog-bar" id="prog-bar"></div></div>` +
      `<div class="prog-meta"><span class="prog-pct" id="prog-pct">0%</span>` +
      `<span class="prog-stage" id="prog-stage">Initializing…</span></div>` +
      `<div class="prog-agent"><span class="dot"></span>` +
      `<div><div class="who" id="prog-who">—</div><div class="role" id="prog-role">waiting</div></div></div>` +
      `<div class="prog-section"><h4>🧠 What it's thinking</h4>` +
      `<div class="body empty" id="prog-think">Not started.</div></div>` +
      `<div class="prog-section"><h4>⚙️ What it's processing</h4>` +
      `<div class="body empty" id="prog-proc">Not started.</div></div>`;
  }
  function setProgress(pct, stageLabel) {
    const bar = $("#prog-bar"); if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    const p = $("#prog-pct"); if (p) p.textContent = Math.round(pct) + "%";
    const s = $("#prog-stage"); if (s && stageLabel) s.textContent = stageLabel;
  }
  function setActiveAgent(source) {
    const info = AGENT_INFO[source]; if (!info) return;
    const who = $("#prog-who"); if (who) who.textContent = info.who;
    const role = $("#prog-role"); if (role) role.textContent = info.role;
  }
  function appendThink(text) {
    const el = $("#prog-think"); if (!el) return;
    el.classList.remove("empty");
    el.textContent = (el.textContent === "Not started." ? "" : el.textContent + "\n\n") + text.slice(0, 800);
    el.scrollTop = el.scrollHeight;
  }
  function appendProc(text) {
    const el = $("#prog-proc"); if (!el) return;
    el.classList.remove("empty");
    el.textContent = (el.textContent === "Not started." ? "" : el.textContent + "\n") + "• " + text.slice(0, 300);
    el.scrollTop = el.scrollHeight;
  }


  // Render the finished pipeline result.
  //
  // This function was CALLED but never defined, inside a `catch (_) {}` that
  // swallowed the resulting ReferenceError. Every pipeline run therefore threw
  // away its own result: the chat bubble kept whatever placeholder text it had
  // and nothing was ever saved to the conversation.
  function finishPipeline(res, c, prog, progEl) {
    const ok = !!(res && res.success);
    const secs = res && res.duration ? Number(res.duration).toFixed(1) + "s" : "";
    const lines = [];

    if (ok) {
      lines.push("✅ Pipeline complete" + (secs ? " in " + secs : ""));
    } else {
      lines.push("❌ Pipeline did not complete" + (secs ? " after " + secs : ""));
      if (res && res.error) lines.push("", "Reason: " + res.error);
    }
    if (res && res.verification_passed) lines.push("Verification passed.");

    const tasks = (res && res.micro_tasks) || [];
    if (tasks.length) {
      lines.push("", "Micro-tasks (" + tasks.length + "):");
      tasks.slice(0, 20).forEach((t, i) => {
        const mark = t.done || t.status === "done" ? "✓" : "•";
        lines.push("  " + mark + " " + (t.title || t.description || t.id || ("task " + (i + 1))));
      });
      if (tasks.length > 20) lines.push("  … and " + (tasks.length - 20) + " more");
    }
    if (res && res.summary) lines.push("", res.summary);
    if (res && res.plan && !ok) lines.push("", "Plan:", String(res.plan).slice(0, 1500));

    const text = lines.join("\n");
    progEl.textContent = text;
    setProgress(100, ok ? "Complete" : "Failed");

    // Keep it on the conversation object so switching chats does not lose it.
    try {
      c.messages = c.messages || [];
      c.messages.push({ role: "agent", text: text });
    } catch (_) {}

    // Surface the execution log in the Log tab rather than dropping it.
    try {
      const log = (res && res.execution_log) || [];
      log.slice(0, 200).forEach((l) => appendProc(typeof l === "string" ? l : JSON.stringify(l)));
    } catch (_) {}
  }

  async function runPipeline(request, c) {
    $("#thinking").innerHTML = "";
    thinkingBlocks = {};
    resetProgress();
    // Auto-switch the right panel to the Progress tab so it's visible.
    $$(".panel-tab").forEach((t) => t.classList.toggle("active", t.dataset.ptab === "progress"));
    $$(".panel-pane").forEach((x) => x.classList.toggle("active", x.dataset.ppane === "progress"));
    // Live progress bubble in the chat so the run never looks "frozen".
    const prog = addMessage("agent", "⏳ Pipeline starting…", c);
    const progEl = prog.querySelector(".bubble");
    const stageTag = { architect: "🧠 Architect", orchestrator: "📋 Orchestrator",
      executioner: "⚙️ Executioner", frontend: "💬 Frontend" };
    let lastStage = "";
    const setProg = (txt) => { progEl.textContent = txt; chatEl.scrollTop = chatEl.scrollHeight; };
    const body = { request, provider: "ollama", base_url: "http://localhost:11434",
                   project_path: c.folder || null };
    try {
      const resp = await fetch("/orchestrate", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      // An HTTP failure is NOT a fetch failure: without this check a 401 or a
      // 500 was read as if it were an event stream, so the UI sat on
      // "Initializing..." or reported a meaningless transport error instead of
      // the status the server actually returned.
      if (!resp.ok) {
        let detail = "";
        try { detail = (await resp.text()).slice(0, 500); } catch (_) {}
        let hint = "";
        if (resp.status === 401) {
          hint = "\n\nThe UI was not authenticated. Reload this page (Ctrl+F5) " +
                 "so it picks up the current API key.";
        } else if (resp.status === 404) {
          hint = "\n\nThis server build has no /orchestrate route. Restart the " +
                 "server from the current code.";
        } else if (resp.status >= 500) {
          hint = "\n\nThe server hit an error before the run started. The full " +
                 "traceback is in the server console.";
        }
        progEl.textContent = "Pipeline could not start: HTTP " + resp.status +
                             " " + resp.statusText + (detail ? "\n\n" + detail : "") + hint;
        setProgress(100, "Failed");
        return;
      }
      if (!resp.body) {
        progEl.textContent = "Pipeline could not start: the server returned no " +
                             "response stream. Check the server console.";
        setProgress(100, "Failed");
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let curEvent = "";
      const flush = () => {
        if (curEvent === "event") {
          try {
            const ev = JSON.parse(buf);
            logEvent(ev);
            if (ev.kind === "progress") {
              const pct = Number(ev.data && ev.data.pct) || 0;
              setProgress(pct, ev.message);
              if (ev.source && stageTag[ev.source]) { lastStage = stageTag[ev.source]; setActiveAgent(ev.source); }
            } else if (ev.source && stageTag[ev.source]) { lastStage = stageTag[ev.source]; setActiveAgent(ev.source); }
            if (ev.kind === "reasoning" && ev.source) {
              appendThink(ev.message);
              setProg(`⏳ ${lastStage || "Pipeline"} running…\n\n${ev.message.slice(-400)}`);
            } else if (ev.kind === "tool" || (ev.kind === "agent" && ev.source === "executioner")) {
              appendProc(ev.message);
              setProg(`⏳ ${lastStage || "Pipeline"}: ${ev.message}`);
            } else if (ev.source) {
              setProg(`⏳ ${lastStage} working…`);
            }
          } catch (_) {}
        }
        else if (curEvent === "result") {
          try {
            finishPipeline(JSON.parse(buf), c, prog, progEl);
          } catch (err) {
            // Never swallow this again -- an empty catch here is what hid the
            // fact that finishPipeline did not exist at all.
            console.error("finishPipeline failed", err, buf);
            progEl.textContent = "The pipeline finished but its result could not " +
                                 "be displayed: " + err.message;
          }
        }
        else if (curEvent === "error") {
          // The server could not finish the run. Show WHY -- this used to be
          // impossible: the stream just died and the browser said
          // "network error", which told the user nothing.
          try {
            const err = JSON.parse(buf);
            const msg = "Pipeline failed while " + (err.stage || "running") +
                        ".\n\n" + (err.error || "Unknown error") +
                        (err.hint ? "\n\n" + err.hint : "");
            progEl.textContent = msg;
            appendProc("ERROR " + (err.error || ""));
            setProgress(100, "Failed");
          } catch (_) {
            progEl.textContent = "Pipeline failed: " + buf;
          }
        }
        buf = "";
      };
      let pending = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        // Do NOT clear `buf` here: an SSE frame can be split across chunks, and
        // resetting per chunk silently dropped the first half of long events.
        const chunk = pending + dec.decode(value, { stream: true });
        const lines = chunk.split("\n");
        pending = lines.pop();          // keep any partial trailing line
        for (const raw of lines) {
          const line = raw.replace(/\r$/, "");
          if (line.startsWith("event:")) { curEvent = line.slice(6).trim(); }
          else if (line.startsWith("data:")) { buf += line.slice(5).trim(); }
          else if (line === "") { flush(); curEvent = ""; }
        }
      }
      flush();
    } catch (e) {
      // A genuine transport failure (server stopped, connection dropped).
      progEl.textContent =
        "Lost the connection to the Y.A.C.I.N.E server: " + e.message +
        "\n\nIs it still running? Check the server console for a traceback.";
    }
  }

  // ---- Wire up ----
  $("#new-chat").onclick = newChat;
  sendBtn.onclick = sendMessage;
  promptEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  const stopBtn = $("#stop");
  if (stopBtn) stopBtn.onclick = async () => {
    stopBtn.disabled = true;
    try { await fetch("/orchestrate/cancel", { method: "POST" }); } catch (_) {}
    stopBtn.textContent = "■ Stopping…";
  };
  $("#rail-search").oninput = renderRail;
  $$(".rail-tab").forEach((t) => t.onclick = () => {
    state.view = t.dataset.view;
    $$(".rail-tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    renderRail();
  });

  // ---- Init ----
  state.projects.push({ id: uid(), name: "Baccalaureate Math (باكالوريا)", folder: "" });
  newChat();
  loadModels();
})();
