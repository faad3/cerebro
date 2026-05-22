"use strict";

// ===========================================================================
// Token + login
// ===========================================================================

const TOKEN_KEY = "cerebro.token";

let authBlocked = false;

function getStoredToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function clearToken() {
  authBlocked = false;
  localStorage.removeItem(TOKEN_KEY);
  location.reload();
}

document.getElementById("logout").addEventListener("click", clearToken);
document.getElementById("brand").addEventListener("click", () => navigate("#/"));
for (const link of document.querySelectorAll(".topnav-link")) {
  link.addEventListener("click", () => navigate(link.dataset.route));
}

function highlightTopnav() {
  const h = location.hash || "#/";
  let active = null;
  if (h === "#/" || h.startsWith("#/nodes/")) active = "#/";
  else if (h === "#/dashboard") active = "#/dashboard";
  else active = "#/agents";
  for (const link of document.querySelectorAll(".topnav-link")) {
    link.classList.toggle("active", link.dataset.route === active);
  }
}

function showLoginScreen(reason) {
  teardownView();
  setCrumbs([{ label: "login" }]);
  const view = document.getElementById("view");
  view.innerHTML = "";
  const sec = document.createElement("section");
  sec.className = "screen login-screen";
  sec.innerHTML = `
    <div class="login-card">
      <h2>cerebro</h2>
      <p class="login-hint">paste the master's token — retrieve with <code>docker compose exec cerebro-server cerebro-server token</code></p>
      <form id="login-form" autocomplete="off">
        <input id="token-input" type="password" placeholder="token" autocomplete="current-password" />
        <button type="submit" class="primary">login</button>
      </form>
      <p id="login-error" class="login-error hidden"></p>
    </div>
  `;
  view.appendChild(sec);
  const form = document.getElementById("login-form");
  const input = document.getElementById("token-input");
  const errEl = document.getElementById("login-error");
  if (reason) { errEl.textContent = reason; errEl.classList.remove("hidden"); }
  setTimeout(() => input.focus(), 0);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const t = input.value.trim();
    if (!t) return;
    errEl.classList.add("hidden");
    try {
      const res = await fetch("/api/nodes", { headers: { Authorization: "Bearer " + t } });
      if (res.status === 401) { errEl.textContent = "token rejected"; errEl.classList.remove("hidden"); input.select(); return; }
      if (!res.ok) { errEl.textContent = res.status + " " + res.statusText; errEl.classList.remove("hidden"); return; }
      localStorage.setItem(TOKEN_KEY, t);
      authBlocked = false;
      requestNotifPermission();
      navigate("#/agents");
      render();
    } catch (err) { errEl.textContent = "network error: " + err.message; errEl.classList.remove("hidden"); }
  });
}

async function api(method, path, body) {
  if (authBlocked) throw new Error("auth blocked");
  const token = getStoredToken();
  if (!token) { authBlocked = true; showLoginScreen(); throw new Error("no token"); }
  const opts = { method, headers: { Authorization: "Bearer " + token } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  if (res.status === 401) { authBlocked = true; localStorage.removeItem(TOKEN_KEY); showLoginScreen("token rejected"); throw new Error("unauthorized"); }
  if (!res.ok) { let d = res.statusText; try { const j = await res.json(); if (j.detail) d = j.detail; } catch (_) {} throw new Error(d); }
  if (res.status === 204) return null;
  return res.json();
}

// ===========================================================================
// Helpers
// ===========================================================================

function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "error" ? " error" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function fmtTime(iso) { return new Date(iso).toLocaleString(); }

function relTime(iso) {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return Math.floor(d) + "s ago";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  return Math.floor(d / 3600) + "h ago";
}

function setCrumbs(parts) {
  const el = document.getElementById("crumbs");
  el.innerHTML = "";
  parts.forEach((p, i) => {
    if (i > 0) { const s = document.createElement("span"); s.className = "sep"; s.textContent = "/"; el.appendChild(s); }
    if (p.href) { const a = document.createElement("a"); a.textContent = p.label; a.addEventListener("click", () => navigate(p.href)); el.appendChild(a); }
    else { const s = document.createElement("span"); s.textContent = p.label; el.appendChild(s); }
  });
}

function mountTemplate(id) {
  const tpl = document.getElementById(id);
  const node = tpl.content.cloneNode(true);
  const view = document.getElementById("view");
  view.innerHTML = "";
  view.appendChild(node);
}

function makeConfirmingClick(btn, originalLabel, action, opts = {}) {
  const timeoutMs = opts.timeoutMs ?? 3000;
  let armed = false, timer = null;
  function disarm() { armed = false; btn.textContent = originalLabel; btn.classList.remove("confirming"); if (timer) { clearTimeout(timer); timer = null; } }
  btn.addEventListener("click", async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!armed) { armed = true; btn.textContent = "confirm?"; btn.classList.add("confirming"); timer = setTimeout(disarm, timeoutMs); return; }
    disarm();
    try { await action(); } catch (_) {}
  });
}

function estimateTerminalDims() {
  const cols = Math.max(80, Math.floor((window.innerWidth - 300) / 8));
  const rows = Math.max(24, Math.floor((window.innerHeight - 130) / 17));
  return { cols, rows };
}

// ===========================================================================
// Activity + Notifications
// ===========================================================================

const ACTIVITY_THRESHOLD_MS = 5000;
const _prevActivity = {};

function agentActivityState(a) {
  if (a.status === "dead" || a.status === "orphaned") return "dead";
  if (!a.last_activity_at) return "idle";
  return (Date.now() - new Date(a.last_activity_at).getTime()) < ACTIVITY_THRESHOLD_MS ? "active" : "idle";
}

function activityDotHtml(state) {
  return `<span class="activity-dot ${state}"></span>`;
}

function requestNotifPermission() {
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
}

function fireNotification(title, body, agentId) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const n = new Notification(title, { body, tag: "cerebro-" + agentId });
  n.onclick = () => { window.focus(); navigate(`#/agents/${agentId}`); n.close(); };
  playNotifSound();
}

let _audioCtx = null;
function playNotifSound() {
  try {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const o = _audioCtx.createOscillator(), g = _audioCtx.createGain();
    o.connect(g); g.connect(_audioCtx.destination);
    o.type = "sine"; o.frequency.setValueAtTime(880, _audioCtx.currentTime); o.frequency.setValueAtTime(660, _audioCtx.currentTime + 0.1);
    g.gain.setValueAtTime(0.15, _audioCtx.currentTime); g.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.3);
    o.start(); o.stop(_audioCtx.currentTime + 0.3);
  } catch (_) {}
}

function checkActivityTransitions(agents) {
  for (const a of agents) {
    const s = agentActivityState(a);
    if (_prevActivity[a.agent_id] === "active" && s === "idle") {
      fireNotification(`Session "${a.name || a.agent_id.slice(0, 8)}" is idle`, "Finished working.", a.agent_id);
    }
    _prevActivity[a.agent_id] = s;
  }
}

const SKIP_PERMS_KEY = "cerebro.skip_permissions";
function getSkipPermsToggle() { return localStorage.getItem(SKIP_PERMS_KEY) === "1"; }
function setSkipPermsToggle(v) { localStorage.setItem(SKIP_PERMS_KEY, v ? "1" : "0"); }

// ===========================================================================
// Drag-and-drop primitives + html escape
// ===========================================================================

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const FOLDER_ADJ = ["cosmic","wandering","sleepy","fuzzy","silver","tiny","mighty","spicy","glowing","frosty","misty","puzzled","jolly","stoic","sneaky"];
const FOLDER_NOUN = ["nest","stash","cluster","den","grove","atlas","bundle","cabin","loft","shelf","dock","alcove","bunker","attic"];
function folderName() {
  return `${FOLDER_ADJ[Math.floor(Math.random()*FOLDER_ADJ.length)]}-${FOLDER_NOUN[Math.floor(Math.random()*FOLDER_NOUN.length)]}`;
}

const DRAG_MIME = "application/x-cerebro-drag";

function wireDragSource(el, payload) {
  el.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(payload));
    e.dataTransfer.effectAllowed = "move";
    el.classList.add("dragging");
  });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
}

function wireDragTarget(el, onDrop) {
  el.addEventListener("dragover", (e) => {
    if (!Array.from(e.dataTransfer.types || []).includes(DRAG_MIME)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    el.classList.add("drop-target");
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
  el.addEventListener("drop", async (e) => {
    el.classList.remove("drop-target");
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    e.preventDefault(); e.stopPropagation();
    try { await onDrop(JSON.parse(raw), e); } catch (_) {}
  });
}

// ===========================================================================
// Router
// ===========================================================================

let currentTeardown = null;
function teardownView() { if (currentTeardown) { try { currentTeardown(); } catch (_) {} currentTeardown = null; } }
function navigate(hash) { if (location.hash === hash) render(); else location.hash = hash; }

window.addEventListener("hashchange", render);
window.addEventListener("DOMContentLoaded", () => { if (!location.hash) location.hash = "#/agents"; render(); });

let _currentRoute = null;

// Module-level state that survives route switches.
// Agent panels (DOM + xterm + WS) stay alive when we navigate to nodes/dashboard
// and come back — so the terminal state and scrollback are preserved.
const agentPanels = new Map();  // agent_id → {panel, claudeTerm, bashTerm, onMove, onUp, cleanup}
let _agentsLastSelected = null;  // which agent was active when we last left the view

async function render() {
  highlightTopnav();
  if (authBlocked || !getStoredToken()) { teardownView(); showLoginScreen(); return; }

  const h = location.hash || "#/agents";
  const route =
    h === "#/" ? "nodes" :
    h === "#/dashboard" ? "dashboard" :
    h.startsWith("#/agents") ? "agents" :
    "nodes";

  // Only tear down if we're actually switching to a different route.
  // This preserves agent panel cache while navigating between agents.
  if (_currentRoute !== route) {
    teardownView();
    _currentRoute = route;
  }

  if (route === "nodes") return renderNodes();
  if (route === "dashboard") return renderDashboard();
  return renderAgentsDetail();
}

// ===========================================================================
// Nodes view (unchanged)
// ===========================================================================

async function renderNodes() {
  setCrumbs([]);
  mountTemplate("tpl-nodes");
  const grid = document.getElementById("node-grid");
  const empty = document.getElementById("nodes-empty");
  document.getElementById("refresh-nodes").addEventListener("click", refresh);

  async function refresh() {
    let nodes = [], agentsByNode = {};
    try {
      [nodes, agentsByNode] = await Promise.all([
        api("GET", "/api/nodes"),
        api("GET", "/api/agents").then(rows => { const by = {}; for (const a of rows) (by[a.node_id] = by[a.node_id] || []).push(a); return by; }),
      ]);
    } catch (e) { toast("failed: " + e.message, "error"); return; }
    grid.innerHTML = "";
    if (!nodes.length) { empty.classList.remove("hidden"); return; }
    empty.classList.add("hidden");
    for (const n of nodes) {
      const ag = agentsByNode[n.node_id] || [];
      const active = ag.filter(a => agentActivityState(a) === "active").length;
      const idle = ag.filter(a => agentActivityState(a) === "idle" && a.status === "running").length;
      const dead = ag.filter(a => a.status === "dead" || a.status === "orphaned").length;
      const summary = [active ? `${active} active` : "", idle ? `${idle} idle` : "", dead ? `${dead} dead` : ""].filter(Boolean).join(" · ") || "no agents";
      const card = document.createElement("div");
      card.className = "node-card";
      card.innerHTML = `
        <div class="host"><span class="dot ${n.status}"></span>${n.name || n.hostname}</div>
        <div class="meta"><span>${n.node_id.slice(0, 8)}</span><span>${summary}</span></div>
        <div class="meta"><span>last beat: ${relTime(n.last_heartbeat)}</span></div>
        <div class="actions"><button class="ghost" data-act="open">open</button><button class="primary" data-act="new">+ new session</button></div>
      `;
      const hostEl = card.querySelector(".host");
      hostEl.dataset.fallback = n.hostname;
      makeEditable(hostEl, n.name || n.hostname, `/api/nodes/${n.node_id}`, "name");
      card.querySelector('[data-act="open"]').addEventListener("click", () => navigate(`#/nodes/${n.node_id}/agents`));
      card.querySelector('[data-act="new"]').addEventListener("click", () => {
        const { cols, rows } = estimateTerminalDims();
        api("POST", "/api/agents", { node_id: n.node_id, cols, rows, dangerously_skip_permissions: getSkipPermsToggle() })
          .then(a => navigate(`#/agents/${a.agent_id}`))
          .catch(e => toast("failed: " + e.message, "error"));
      });
      grid.appendChild(card);
    }
  }
  await refresh();
  const iv = setInterval(refresh, 5000);
  currentTeardown = () => clearInterval(iv);
}

function makeEditable(el, currentVal, patchUrl, fieldName = "name") {
  el.classList.add("editable-name");
  el.title = "click to rename";
  el.addEventListener("click", (e) => {
    e.stopPropagation();
    if (el.querySelector(".edit-name-input")) return;
    const inp = document.createElement("input");
    inp.className = "edit-name-input"; inp.value = currentVal || ""; inp.placeholder = "name";
    el.textContent = ""; el.appendChild(inp); inp.focus(); inp.select();
    async function save() {
      const v = inp.value.trim();
      try { await api("PATCH", patchUrl, { [fieldName]: v }); el.textContent = v || el.dataset.fallback || "—"; currentVal = v; }
      catch (err) { toast("rename failed: " + err.message, "error"); el.textContent = currentVal || el.dataset.fallback || "—"; }
    }
    inp.addEventListener("keydown", (ke) => { if (ke.key === "Enter") { ke.preventDefault(); save(); } if (ke.key === "Escape") el.textContent = currentVal || el.dataset.fallback || "—"; });
    inp.addEventListener("blur", save);
  });
}

// ===========================================================================
// Master-detail: sidebar with all agents + terminal pane on the right
// ===========================================================================

let _agentsDetailState = null; // persists across sidebar refreshes

const VIEW_MODE_KEY = "cerebro.view_mode";  // "all" | "by-node"
function getViewMode() { return localStorage.getItem(VIEW_MODE_KEY) || "all"; }
function setViewMode(m) { localStorage.setItem(VIEW_MODE_KEY, m); }

async function renderAgentsDetail() {
  // Parse route: #/agents → use last selected (if still alive); #/agents/{id} → specific
  const h = location.hash || "#/agents";
  const m = h.match(/^#\/agents\/([^/]+)/);
  let selectedId = m ? m[1] : null;
  if (!selectedId && _agentsLastSelected && agentPanels.has(_agentsLastSelected)) {
    selectedId = _agentsLastSelected;
    // Update URL without triggering another render.
    const newHash = `#/agents/${selectedId}`;
    history.replaceState(null, "", newHash);
  }

  // If the template is already mounted (sidebar still in DOM), just update selection.
  if (_agentsDetailState && document.getElementById("sidebar")) {
    await _agentsDetailState.selectAgent(selectedId);
    return;
  }

  setCrumbs([]);
  mountTemplate("tpl-agents-detail");

  // Re-attach cached panels (if we're returning to this view from nodes/dashboard).
  const panelsSlot = document.getElementById("agent-panels");
  for (const cache of agentPanels.values()) {
    panelsSlot.appendChild(cache.panel);
  }

  const sidebarList = document.getElementById("sidebar-list");
  const sidebarFavs = document.getElementById("sidebar-favorites");
  const totalEl = document.getElementById("sidebar-total");
  const viewToggle = document.getElementById("view-toggle");

  // Set initial toggle state.
  for (const btn of viewToggle.querySelectorAll(".view-toggle-btn")) {
    btn.classList.toggle("active", btn.dataset.mode === getViewMode());
    btn.onclick = () => {
      setViewMode(btn.dataset.mode);
      for (const b of viewToggle.querySelectorAll(".view-toggle-btn")) {
        b.classList.toggle("active", b.dataset.mode === btn.dataset.mode);
      }
      refreshSidebar();
    };
  }

  let allAgents = [];
  let allNodes = [];
  let allFolders = [];
  let allPlugins = [];
  let hostByNode = {};
  let activeAgentId = null;

  // ---- Favorite toggle ------------------------------------------------------
  async function toggleFavorite(agentId, isFav) {
    try {
      await api("PATCH", `/api/agents/${agentId}`, { is_favorite: !isFav });
      await refreshSidebar();
    } catch (e) { toast("failed: " + e.message, "error"); }
  }

  // ---- Folder operations ---------------------------------------------------
  async function createFolderWith(name, agentIds, nodeId, section = "default") {
    try {
      const f = await api("POST", "/api/folders", { name, node_id: nodeId || null, section });
      for (const aid of agentIds) {
        await api("PATCH", `/api/agents/${aid}`, { folder_id: f.folder_id });
      }
      await refreshSidebar();
      return f;
    } catch (e) { toast("folder failed: " + e.message, "error"); }
  }

  async function moveAgentToFolder(agentId, folderId) {
    try {
      await api("PATCH", `/api/agents/${agentId}`, { folder_id: folderId });
      await refreshSidebar();
    } catch (e) { toast("move failed: " + e.message, "error"); }
  }

  // Move an agent between sections (favorite ↔ default). Backend clears its folder_id automatically.
  async function setAgentFavorite(agentId, fav) {
    try {
      await api("PATCH", `/api/agents/${agentId}`, { is_favorite: fav });
      await refreshSidebar();
    } catch (e) { toast("failed: " + e.message, "error"); }
  }

  async function renameFolder(folderId, name) {
    try {
      await api("PATCH", `/api/folders/${folderId}`, { name });
      await refreshSidebar();
    } catch (e) { toast("rename failed: " + e.message, "error"); }
  }

  async function deleteFolderApi(folderId) {
    try {
      await api("DELETE", `/api/folders/${folderId}`);
      await refreshSidebar();
    } catch (e) { toast("delete failed: " + e.message, "error"); }
  }

  // ---- Render helpers ------------------------------------------------------
  function pluginIconFor(agent) {
    const pid = agent.plugin_id || "claude";
    const p = allPlugins.find(x => x.id === pid);
    return p ? p.icon || "•" : "•";
  }

  function buildItemEl(a, opts = {}) {
    const state = agentActivityState(a);
    const item = document.createElement("div");
    item.className = "sidebar-item" + (a.agent_id === activeAgentId ? " active" : "");
    item.dataset.agentId = a.agent_id;
    item.draggable = true;
    const name = a.name || a.agent_id.slice(0, 10);
    const showHost = opts.showHost !== false;
    const hostLine = showHost
      ? `${hostByNode[a.node_id] || ""}${a.cwd ? " · " + a.cwd : ""}`
      : (a.cwd || "");
    item.title = `${name}${a.cwd ? "\n" + a.cwd : ""}\nhost: ${hostByNode[a.node_id] || a.node_id.slice(0,8)}\nplugin: ${a.plugin_id || "claude"}`;
    item.innerHTML = `
      ${activityDotHtml(state)}
      <span class="plugin-icon" title="${a.plugin_id || "claude"}">${pluginIconFor(a)}</span>
      <div class="sidebar-item-info">
        <div class="sidebar-item-name" title="double-click to rename">${escapeHtml(name)}</div>
        <div class="sidebar-item-host">${escapeHtml(hostLine)}</div>
      </div>
      <button class="item-fav ${a.is_favorite ? "is-fav" : ""}" title="${a.is_favorite ? "unfavorite" : "favorite"}">${a.is_favorite ? "★" : "☆"}</button>
    `;
    item.addEventListener("click", (e) => {
      if (e.target.closest(".item-fav")) return;
      if (e.target.closest(".sidebar-item-name input")) return;
      if (e.detail >= 2) return; // ignore the click that's part of a dblclick
      navigate(`#/agents/${a.agent_id}`);
    });
    item.querySelector(".item-fav").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFavorite(a.agent_id, a.is_favorite);
    });

    // Double-click name → inline rename.
    const nameEl = item.querySelector(".sidebar-item-name");
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (nameEl.querySelector("input")) return;
      const inp = document.createElement("input");
      inp.className = "edit-name-input";
      inp.value = a.name || "";
      inp.placeholder = "name";
      nameEl.textContent = "";
      nameEl.appendChild(inp);
      inp.focus();
      inp.select();
      const finish = async (save) => {
        const v = inp.value.trim();
        if (save && v !== (a.name || "")) {
          try {
            await api("PATCH", `/api/agents/${a.agent_id}`, { name: v });
            a.name = v || null;
          } catch (err) { toast("rename failed: " + err.message, "error"); }
        }
        nameEl.textContent = a.name || a.agent_id.slice(0, 10);
        await refreshSidebar();
      };
      inp.addEventListener("keydown", (ke) => {
        if (ke.key === "Enter") { ke.preventDefault(); finish(true); }
        if (ke.key === "Escape") { ke.preventDefault(); finish(false); }
      });
      inp.addEventListener("blur", () => finish(true));
    });
    wireDragSource(item, { type: "agent", id: a.agent_id });
    wireDragTarget(item, async (payload) => {
      if (payload.type !== "agent" || payload.id === a.agent_id) return;
      const dragged = allAgents.find(x => x.agent_id === payload.id);
      if (!dragged) return;
      // Drop agent onto agent → join target's folder (if any), else create folder
      // in target's section. Backend will flip is_favorite to match target's section.
      if (a.folder_id) {
        await moveAgentToFolder(payload.id, a.folder_id);
      } else {
        // First align section so backend doesn't reject folder creation.
        if (dragged.is_favorite !== a.is_favorite) {
          await api("PATCH", `/api/agents/${payload.id}`, { is_favorite: a.is_favorite });
        }
        await createFolderWith(folderName(), [a.agent_id, payload.id], null, a.is_favorite ? "favorite" : "default");
      }
    });
    return item;
  }

  function buildFolderEl(folder, agentsInFolder) {
    const el = document.createElement("div");
    el.className = "folder" + (folder._collapsed ? " collapsed" : "");
    el.dataset.folderId = folder.folder_id;
    const header = document.createElement("div");
    header.className = "folder-header";
    header.title = `folder: ${folder.name}\ndouble-click name to rename · click × twice to delete`;
    header.innerHTML = `
      <span class="folder-chev">▾</span>
      <span class="folder-name" title="double-click to rename">${escapeHtml(folder.name)}</span>
      <span class="folder-count">${agentsInFolder.length}</span>
      <button class="folder-del" title="click twice to delete folder">×</button>
    `;
    header.addEventListener("click", (e) => {
      if (e.target.closest(".folder-del")) return;
      if (e.target.closest(".folder-name input")) return;
      folder._collapsed = !folder._collapsed;
      el.classList.toggle("collapsed");
    });
    // Inline rename on dblclick of name.
    const nameEl = header.querySelector(".folder-name");
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      const inp = document.createElement("input");
      inp.value = folder.name;
      nameEl.classList.add("editing");
      nameEl.textContent = "";
      nameEl.appendChild(inp);
      inp.focus();
      inp.select();
      const finish = async (save) => {
        const v = inp.value.trim();
        nameEl.classList.remove("editing");
        nameEl.textContent = save && v ? v : folder.name;
        if (save && v && v !== folder.name) await renameFolder(folder.folder_id, v);
      };
      inp.addEventListener("keydown", (ke) => {
        if (ke.key === "Enter") { ke.preventDefault(); finish(true); }
        if (ke.key === "Escape") finish(false);
      });
      inp.addEventListener("blur", () => finish(true));
    });
    header.querySelector(".folder-del").addEventListener("click", async (e) => {
      e.stopPropagation();
      const btn = e.currentTarget;
      if (!btn.dataset.armed) {
        btn.dataset.armed = "1";
        btn.textContent = "✓";
        btn.style.color = "var(--danger)";
        setTimeout(() => { delete btn.dataset.armed; btn.textContent = "×"; btn.style.color = ""; }, 2500);
        return;
      }
      await deleteFolderApi(folder.folder_id);
    });
    // Drop onto folder header → move agent into folder.
    wireDragTarget(header, async (payload) => {
      if (payload.type !== "agent") return;
      await moveAgentToFolder(payload.id, folder.folder_id);
    });

    el.appendChild(header);
    const children = document.createElement("div");
    children.className = "folder-children";
    for (const a of agentsInFolder) {
      children.appendChild(buildItemEl(a));
    }
    el.appendChild(children);
    return el;
  }


  // ---- Sidebar render -----------------------------------------------------
  async function refreshSidebar() {
    try {
      [allAgents, allNodes, allFolders, allPlugins] = await Promise.all([
        api("GET", "/api/agents"),
        api("GET", "/api/nodes"),
        api("GET", "/api/folders"),
        allPlugins.length ? Promise.resolve(allPlugins) : api("GET", "/api/plugins"),
      ]);
    } catch (e) { return; }
    hostByNode = {};
    for (const n of allNodes) hostByNode[n.node_id] = n.name || n.hostname;
    checkActivityTransitions(allAgents);

    const running = allAgents.filter(a => a.status === "running").length;
    totalEl.textContent = `${running}/${allAgents.length}`;

    // Both sections (favorites top, default below) are rendered with the same rules.
    sidebarFavs.innerHTML = "";
    sidebarList.innerHTML = "";

    const mode = getViewMode();
    const favAgents = allAgents.filter(a => a.is_favorite);
    const defAgents = allAgents.filter(a => !a.is_favorite);

    // Both sections share identical rendering rules — only the section name differs.
    renderSection(sidebarFavs, favAgents, "favorite", mode);
    renderSection(sidebarList, defAgents, "default", mode);

    // Drop on the default section background → unfavorite the dragged agent.
    wireDragTarget(sidebarList, async (payload) => {
      if (payload.type !== "agent") return;
      const a = allAgents.find(x => x.agent_id === payload.id);
      if (!a || !a.is_favorite) return;
      await setAgentFavorite(a.agent_id, false);
    });
    // Drop on the favorites section background → favorite the dragged agent.
    wireDragTarget(sidebarFavs, async (payload) => {
      if (payload.type !== "agent") return;
      const a = allAgents.find(x => x.agent_id === payload.id);
      if (!a || a.is_favorite) return;
      await setAgentFavorite(a.agent_id, true);
    });

    if (allAgents.length === 0) {
      sidebarList.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px">no sessions yet — click + to start</div>';
    }

    // Dispose cached panels for agents that no longer exist.
    // Also sync panel title with current name (for rename).
    const liveIds = new Set(allAgents.map(a => a.agent_id));
    for (const id of [...agentPanels.keys()]) {
      if (!liveIds.has(id)) {
        disposeAgentPanel(id);
        continue;
      }
      const cur = allAgents.find(x => x.agent_id === id);
      const cache = agentPanels.get(id);
      const titleEl = cache && cache.panel.querySelector(".agent-title-name");
      if (cur && titleEl) titleEl.textContent = cur.name || "session";
    }
  }

  function renderSection(container, agents, section, mode) {
    if (mode === "by-node") {
      const byNode = {};
      for (const a of agents) (byNode[a.node_id] = byNode[a.node_id] || []).push(a);
      for (const n of allNodes) {
        const ags = byNode[n.node_id] || [];
        if (!ags.length && allNodes.length > 1) continue;
        const group = document.createElement("div");
        group.className = "sidebar-group";
        group.innerHTML = `<span class="dot ${n.status}"></span><span>${escapeHtml(hostByNode[n.node_id] || n.node_id.slice(0,8))}</span>`;
        const addBtn = document.createElement("button");
        addBtn.className = "group-add";
        addBtn.textContent = "+";
        addBtn.title = "new session on this node";
        addBtn.onclick = (e) => { e.stopPropagation(); openPicker({ nodeId: n.node_id }); };
        group.appendChild(addBtn);
        container.appendChild(group);
        renderAgentsAndFolders(container, ags, n.node_id, section);
      }
    } else {
      renderAgentsAndFolders(container, agents, null, section);
    }
  }

  function renderAgentsAndFolders(container, agents, nodeFilter, section) {
    // Group agents by folder.
    const byFolder = new Map();
    const loose = [];
    for (const a of agents) {
      if (a.folder_id) {
        if (!byFolder.has(a.folder_id)) byFolder.set(a.folder_id, []);
        byFolder.get(a.folder_id).push(a);
      } else {
        loose.push(a);
      }
    }
    // Folders shown in this section: matching section AND node-scope.
    const relevantFolders = allFolders.filter(f => {
      if ((f.section || "default") !== section) return false;
      if (nodeFilter == null) return true;
      return f.node_id == null || f.node_id === nodeFilter;
    });
    relevantFolders.sort((a,b) => a.position - b.position);
    for (const f of relevantFolders) {
      const items = byFolder.get(f.folder_id) || [];
      container.appendChild(buildFolderEl(f, items));
    }
    loose.sort((a,b) => a.position - b.position);
    for (const a of loose) container.appendChild(buildItemEl(a));
  }

  // Close button → back to full list.
  // close-detail is per-panel now; handler wired in buildAgentPanel.

  async function selectAgent(agentId) {
    activeAgentId = agentId;

    // Highlight in sidebar (favorites or default).
    for (const el of document.querySelectorAll(".sidebar-item")) {
      el.classList.toggle("active", el.dataset.agentId === agentId);
    }

    const detailEmpty = document.getElementById("detail-empty");
    const detailAgent = document.getElementById("detail-agent");
    const agentPanelsEl = document.getElementById("agent-panels");

    // No agent → show empty state. Sidebar stays in place either way.
    if (!agentId) {
      detailEmpty.classList.remove("hidden");
      detailAgent.classList.add("hidden");
      for (const cache of agentPanels.values()) cache.panel.classList.add("hidden");
      return;
    }

    detailEmpty.classList.add("hidden");
    detailAgent.classList.remove("hidden");

    // Find or build the target panel.
    let cached = agentPanels.get(agentId);
    if (!cached) {
      let agent = allAgents.find(a => a.agent_id === agentId);
      if (!agent) {
        try { agent = await api("GET", `/api/agents/${agentId}`); } catch (e) { toast("agent not found", "error"); return; }
      }
      // Auto-resume orphaned/dead agents that have a saved session.
      if (agent.status !== "running" && agent.claude_session_id) {
        toast(`resuming ${agent.name || agent.agent_id.slice(0,8)}...`);
        try {
          const { cols, rows } = estimateTerminalDims();
          agent = await api("POST", `/api/agents/${agentId}/resume?cols=${cols}&rows=${rows}`);
        } catch (e) { toast("resume failed: " + e.message, "error"); return; }
      }
      if (!agent.claude_terminal_id) { toast("no claude terminal — agent cannot be opened", "error"); return; }
      cached = buildAgentPanel(agent);
      agentPanels.set(agentId, cached);
      agentPanelsEl.appendChild(cached.panel);
    }

    // Show target FIRST, then hide others — no blank frame.
    cached.panel.classList.remove("hidden");
    for (const [id, other] of agentPanels) {
      if (id !== agentId) other.panel.classList.add("hidden");
    }
    // Refit in case viewport changed.
    requestAnimationFrame(() => {
      cached.claudeTerm.fit();
      if (cached.bashTerm) cached.bashTerm.fit();
    });
  }

  // Build a per-window panel: toolbar + single terminal pane.
  // Panels are cached in `agentPanels` and survive across selections.
  function buildAgentPanel(agent) {
    const hostname = hostByNode[agent.node_id] || agent.node_id.slice(0, 8);
    const pluginIcon = pluginIconFor(agent);

    const panel = document.createElement("div");
    panel.className = "agent-panel";
    panel.dataset.agentId = agent.agent_id;
    panel.innerHTML = `
      <div class="agent-toolbar">
        <span class="agent-title">${pluginIcon} ${escapeHtml(hostname)} · <span class="agent-title-name" style="color:var(--accent)">${escapeHtml(agent.name || "session")}</span></span>
        <span class="spacer"></span>
        <button class="danger" data-role="kill">kill</button>
        <button class="ghost" data-role="close">close</button>
      </div>
      <div class="split" data-role="split">
        <div class="pane" data-role="pane-claude">
          <div class="pane-term" data-role="term-claude"></div>
        </div>
      </div>
    `;

    const claudeContainer = panel.querySelector('[data-role="term-claude"]');
    const claudeTerm = mountTerminal(claudeContainer, agent.claude_terminal_id);

    // Kill button.
    const killBtn = panel.querySelector('[data-role="kill"]');
    makeConfirmingClick(killBtn, "kill", async () => {
      try {
        await api("DELETE", `/api/agents/${agent.agent_id}`);
        disposeAgentPanel(agent.agent_id);
        await refreshSidebar();
        navigate("#/agents");
      } catch (e) { toast("kill failed: " + e.message, "error"); }
    });

    panel.querySelector('[data-role="close"]').addEventListener("click", () => navigate("#/agents"));

    return {
      panel,
      claudeTerm,
      get bashTerm() { return null; },
      onMove() {}, onUp() {},
      cleanup() {
        claudeTerm.dispose();
        panel.remove();
      },
    };
  }

  function disposeAgentPanel(agentId) {
    const cache = agentPanels.get(agentId);
    if (cache) {
      cache.cleanup();
      agentPanels.delete(agentId);
    }
  }

  // ---- Creation flow: + button → plugin picker → options → create ----
  const createBtn = document.getElementById("create-btn");
  const pickerOverlay = document.getElementById("picker-overlay");
  const pickerGrid = document.getElementById("picker-grid");
  const optsOverlay = document.getElementById("options-overlay");
  const optsTitle = document.getElementById("options-title");
  const optsFields = document.getElementById("opt-fields");
  const optNodePicker = document.getElementById("opt-node-picker");
  const optNodeHidden = document.getElementById("opt-node");

  let creating = false;
  let _selectedPlugin = null;
  let _pickerCtx = {};   // { nodeId? } — preselected node when opened via group "+"

  function openPicker(ctx = {}) {
    _pickerCtx = ctx;
    pickerOverlay.classList.remove("hidden");
    renderPickerGrid();
  }
  function closePicker() { pickerOverlay.classList.add("hidden"); }
  function openOptions(plugin) {
    _selectedPlugin = plugin;
    closePicker();
    optsOverlay.classList.remove("hidden");
    optsTitle.textContent = `new ${plugin.label || plugin.id}`;
    renderNodePicker();
    renderOptionsForm(plugin);
  }
  function closeOptions() { optsOverlay.classList.add("hidden"); }

  createBtn.onclick = (e) => { e.stopPropagation(); openPicker(); };
  document.getElementById("picker-close").onclick = closePicker;
  pickerOverlay.onclick = (e) => { if (e.target === pickerOverlay) closePicker(); };
  document.getElementById("options-close").onclick = closeOptions;
  document.getElementById("options-back").onclick = () => { closeOptions(); openPicker(_pickerCtx); };
  optsOverlay.onclick = (e) => { if (e.target === optsOverlay) closeOptions(); };

  function renderPickerGrid() {
    pickerGrid.innerHTML = "";
    if (!allPlugins.length) {
      pickerGrid.innerHTML = '<div style="padding:12px;color:var(--muted);grid-column:1/-1">no plugins available</div>';
      return;
    }
    for (const p of allPlugins) {
      const tile = document.createElement("div");
      tile.className = "picker-tile";
      tile.style.borderLeft = `3px solid ${p.color || "#444"}`;
      tile.innerHTML = `
        <div class="picker-icon">${p.icon || "•"}</div>
        <div class="picker-label">${escapeHtml(p.label || p.id)}</div>
      `;
      tile.addEventListener("click", () => openOptions(p));
      pickerGrid.appendChild(tile);
    }
  }

  function renderNodePicker() {
    optNodePicker.innerHTML = "";
    const preselect = _pickerCtx.nodeId || optNodeHidden.value || (allNodes[0] && allNodes[0].node_id);
    optNodeHidden.value = preselect || "";
    for (const n of allNodes) {
      const btn = document.createElement("div");
      btn.className = "node-pick-item" + (n.node_id === preselect ? " selected" : "");
      btn.dataset.nodeId = n.node_id;
      btn.innerHTML = `<span class="node-status ${n.status}"></span>${escapeHtml(n.name || n.hostname)}`;
      btn.addEventListener("click", () => {
        optNodeHidden.value = n.node_id;
        for (const b of optNodePicker.querySelectorAll(".node-pick-item")) b.classList.remove("selected");
        btn.classList.add("selected");
        // Reload dirs for any path browsers in the form.
        for (const pb of optsFields.querySelectorAll("[data-opt-key]")) {
          if (pb.dataset.optType === "path") {
            const reload = pb._loadDirs;
            if (reload) reload(pb._currentPath || "~");
          }
        }
      });
      optNodePicker.appendChild(btn);
    }
  }

  // Build options form dynamically from plugin manifest.
  function renderOptionsForm(plugin) {
    optsFields.innerHTML = "";
    for (const opt of plugin.options || []) {
      const wrapper = document.createElement("div");
      wrapper.className = "custom-field";
      wrapper.dataset.optKey = opt.key;
      wrapper.dataset.optType = opt.type;

      const label = document.createElement("label");
      label.textContent = opt.label || opt.key;
      wrapper.appendChild(label);

      if (opt.type === "string") {
        const inp = document.createElement("input");
        inp.type = "text"; inp.className = "inline-input";
        inp.dataset.field = opt.key;
        if (opt.default != null) inp.value = opt.default;
        if (opt.optional) inp.placeholder = "optional";
        wrapper.appendChild(inp);
      } else if (opt.type === "number") {
        const inp = document.createElement("input");
        inp.type = "number"; inp.className = "inline-input";
        inp.dataset.field = opt.key;
        if (opt.default != null) inp.value = opt.default;
        wrapper.appendChild(inp);
      } else if (opt.type === "bool") {
        // Replace label with toggle row.
        wrapper.innerHTML = "";
        const toggle = document.createElement("label");
        toggle.className = "toggle-label";
        const cb = document.createElement("input");
        cb.type = "checkbox"; cb.dataset.field = opt.key;
        cb.checked = opt.default === true || (opt.key === "skip_perms" && getSkipPermsToggle());
        const span = document.createElement("span");
        span.textContent = opt.label || opt.key;
        toggle.appendChild(cb); toggle.appendChild(span);
        wrapper.appendChild(toggle);
        if (opt.key === "skip_perms") {
          cb.addEventListener("change", () => setSkipPermsToggle(cb.checked));
        }
      } else if (opt.type === "path") {
        const browser = document.createElement("div");
        browser.className = "path-browser";
        const inp = document.createElement("input");
        inp.type = "text"; inp.className = "inline-input";
        inp.dataset.field = opt.key;
        inp.style.width = "100%";
        inp.placeholder = "~/";
        inp.value = opt.default || "~";
        const dirs = document.createElement("div");
        dirs.className = "path-dirs";
        browser.appendChild(inp); browser.appendChild(dirs);
        wrapper.appendChild(browser);

        let current = inp.value;
        async function loadDirs(path) {
          const nodeId = optNodeHidden.value;
          if (!nodeId) return;
          current = path;
          wrapper._currentPath = current;
          try {
            const res = await api("GET", `/api/nodes/${nodeId}/ls?path=${encodeURIComponent(path)}`);
            inp.value = res.path; current = res.path;
            dirs.innerHTML = "";
            if (res.path !== "/") {
              const up = document.createElement("div");
              up.className = "path-dir-item path-up";
              up.innerHTML = '<span class="dir-icon">↑</span> ..';
              up.addEventListener("click", () => {
                const parent = res.path.replace(/\/[^/]+\/?$/, "") || "/";
                loadDirs(parent);
              });
              dirs.appendChild(up);
            }
            for (const e of res.entries) {
              const item = document.createElement("div");
              item.className = "path-dir-item";
              const icon = e.symlink ? "↗" : "▸";
              const cls = e.symlink ? "dir-icon symlink" : "dir-icon";
              item.innerHTML = `<span class="${cls}">${icon}</span> ${escapeHtml(e.name)}`;
              item.addEventListener("click", () => loadDirs(res.path + "/" + e.name));
              dirs.appendChild(item);
            }
          } catch (err) {
            dirs.innerHTML = `<div style="padding:6px;color:var(--muted);font-size:11px">error: ${err.message}</div>`;
          }
        }
        wrapper._loadDirs = loadDirs;
        inp.addEventListener("keydown", (e) => {
          if (e.key === "Enter") { e.preventDefault(); loadDirs(inp.value); }
          if (e.key === "Tab") {
            e.preventDefault();
            const first = dirs.querySelector(".path-dir-item:not(.path-up)");
            if (first) {
              const name = first.textContent.trim().replace(/^[↗▸]\s*/, "");
              const parts = inp.value.split("/"); parts.pop();
              const base = parts.join("/") || current;
              loadDirs(base + "/" + name);
            }
          }
        });
        // Initial load.
        loadDirs(inp.value);
      }
      optsFields.appendChild(wrapper);
    }
  }

  // Create button in options modal.
  document.getElementById("opt-create").onclick = async () => {
    if (creating || !_selectedPlugin) return;
    const nodeId = optNodeHidden.value;
    if (!nodeId) { toast("pick a node", "error"); return; }
    const opts = {};
    for (const f of optsFields.querySelectorAll("[data-field]")) {
      const k = f.dataset.field;
      if (f.type === "checkbox") opts[k] = f.checked;
      else if (f.type === "number") opts[k] = f.value === "" ? null : Number(f.value);
      else opts[k] = f.value;
    }
    const name = (opts.name || "").trim() || null;
    creating = true;
    const btn = document.getElementById("opt-create");
    btn.disabled = true; btn.textContent = "creating...";
    const { cols, rows } = estimateTerminalDims();
    try {
      const body = {
        node_id: nodeId,
        name,
        plugin_id: _selectedPlugin.id,
        plugin_options: opts,
        cols, rows,
      };
      // Legacy compat for claude plugin via top-level fields.
      if (_selectedPlugin.id === "claude") {
        if ("skip_perms" in opts) body.dangerously_skip_permissions = !!opts.skip_perms;
        if (opts.cwd) body.cwd = opts.cwd;
      }
      const a = await api("POST", "/api/agents", body);
      closeOptions();
      await refreshSidebar();
      navigate(`#/agents/${a.agent_id}`);
    } catch (e) { toast("failed: " + e.message, "error"); }
    finally { creating = false; btn.disabled = false; btn.textContent = "create"; }
  };

  _agentsDetailState = { selectAgent, openPicker };

  await refreshSidebar();
  await selectAgent(selectedId);

  const iv = setInterval(refreshSidebar, 5000);

  // Global splitter drag + window resize handlers for whichever panel is active.
  function onGlobalMove(e) {
    const cache = agentPanels.get(activeAgentId);
    if (cache && cache.onMove) cache.onMove(e);
  }
  function onGlobalUp(e) {
    for (const cache of agentPanels.values()) {
      if (cache.onUp) cache.onUp(e);
    }
  }
  function onWinResize() {
    requestAnimationFrame(() => {
      for (const cache of agentPanels.values()) {
        cache.claudeTerm.fit();
        if (cache.bashTerm) cache.bashTerm.fit();
      }
    });
  }
  document.addEventListener("mousemove", onGlobalMove);
  document.addEventListener("mouseup", onGlobalUp);
  window.addEventListener("resize", onWinResize);

  currentTeardown = () => {
    clearInterval(iv);
    document.removeEventListener("mousemove", onGlobalMove);
    document.removeEventListener("mouseup", onGlobalUp);
    window.removeEventListener("resize", onWinResize);
    // Remember which agent was active for when we return.
    _agentsLastSelected = activeAgentId;
    // Detach panels from DOM but keep them alive (WS + xterm stay connected).
    // They'll be re-attached when the agents view mounts again.
    for (const cache of agentPanels.values()) {
      if (cache.panel.parentNode) cache.panel.remove();
    }
    _agentsDetailState = null;
  };
}

// ===========================================================================
// Terminal mount helper
// ===========================================================================

function mountTerminal(containerEl, terminalId, wsUrlOverride) {
  const term = new Terminal({
    cursorBlink: true, fontSize: 13,
    fontFamily: '"JetBrains Mono", "Fira Code", monospace',
    theme: { background: "#000000", foreground: "#e0e0e0", cursor: "#00ff88" },
    scrollback: 5000, allowProposedApi: true,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(containerEl);
  requestAnimationFrame(() => { try { fit.fit(); } catch (_) {} });

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = wsUrlOverride || `${proto}//${location.host}/ws/terminal/${terminalId}?token=${encodeURIComponent(getStoredToken())}`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  let opened = false, closedByUs = false, onDead = null;

  ws.onopen = () => { opened = true; const { cols, rows } = term; ws.send(JSON.stringify({ type: "resize", cols, rows })); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") { try { const m = JSON.parse(ev.data); if (m.type === "terminal_dead") { term.write(`\r\n\x1b[33m[exited code=${m.exit_code}]\x1b[0m\r\n`); if (onDead) onDead(m); } } catch (_) {} return; }
    term.write(new Uint8Array(ev.data));
  };
  ws.onerror = () => { if (!opened) toast("ws error", "error"); };
  ws.onclose = () => { if (!closedByUs) term.write("\r\n\x1b[31m[disconnected]\x1b[0m\r\n"); };
  term.onData(d => { if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(d)); });
  term.onResize(({ cols, rows }) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "resize", cols, rows })); });

  return {
    term, ws,
    fit() { try { fit.fit(); } catch (_) {} },
    setOnDead(fn) { onDead = fn; },
    dispose() { closedByUs = true; try { ws.close(); } catch (_) {} try { term.dispose(); } catch (_) {} },
  };
}

// ===========================================================================
// Dashboard (orchestrator + agent cards)
// ===========================================================================

async function renderDashboard() {
  setCrumbs([]);
  mountTemplate("tpl-dashboard");

  const orchContainer = document.getElementById("term-orchestrator");
  const orchProto = location.protocol === "https:" ? "wss:" : "ws:";
  const orchTerm = mountTerminal(orchContainer, null, `${orchProto}//${location.host}/ws/orchestrator?token=${encodeURIComponent(getStoredToken())}`);

  const cardList = document.getElementById("dash-agent-list");
  const totalEl = document.getElementById("dash-total");

  async function refreshCards() {
    let agents = [], nodes = [];
    try { [agents, nodes] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/nodes")]); } catch (_) { return; }
    const hostByNode = {};
    for (const n of nodes) hostByNode[n.node_id] = n.name || n.hostname;
    checkActivityTransitions(agents);
    totalEl.textContent = `${agents.filter(a => a.status === "running").length}/${agents.length}`;

    cardList.innerHTML = "";
    for (const a of agents) {
      const state = agentActivityState(a);
      const card = document.createElement("div");
      card.className = "dash-card";
      card.innerHTML = `
        <div class="dash-card-top">${activityDotHtml(state)}<span class="dash-card-name">${a.name || a.agent_id.slice(0, 12)}</span>
          <span class="status-pill ${state}">${state}</span><span class="dash-card-host">${hostByNode[a.node_id] || ""}</span></div>
        <div class="dash-card-bottom"><button class="ghost" data-act="open">open</button>
          ${a.status !== "running" && a.claude_session_id ? '<button class="primary" data-act="resume">resume</button>' : ""}
          <button class="danger" data-act="kill">kill</button></div>
      `;
      card.querySelector('[data-act="open"]').addEventListener("click", () => navigate(`#/agents/${a.agent_id}`));
      const rb = card.querySelector('[data-act="resume"]');
      if (rb) rb.addEventListener("click", async () => { try { await api("POST", `/api/agents/${a.agent_id}/resume?cols=120&rows=40`); await refreshCards(); } catch (e) { toast("resume: " + e.message, "error"); } });
      makeConfirmingClick(card.querySelector('[data-act="kill"]'), "kill", async () => { try { await api("DELETE", `/api/agents/${a.agent_id}`); await refreshCards(); } catch (e) { toast("kill: " + e.message, "error"); } });
      cardList.appendChild(card);
    }
  }
  await refreshCards();
  const iv = setInterval(refreshCards, 5000);
  const onR = () => orchTerm.fit();
  window.addEventListener("resize", onR);
  currentTeardown = () => { clearInterval(iv); window.removeEventListener("resize", onR); orchTerm.dispose(); };
}
