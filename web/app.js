"use strict";
// HyruleLink single-page UI: home (join / create / live-rooms) → room. Talks REST
// for rooms and one WebSocket for live ledger state. Roles: "ui" (a player),
// "spectator" (read-only watcher), and any of those may carry an admin key that
// unlocks global-admin controls (manage/kick/delete on ANY room).

const $ = (id) => document.getElementById(id);
const show = (id) => $(id).classList.remove("hidden");
const hide = (id) => $(id).classList.add("hidden");

const state = {
  name: localStorage.getItem("hl_name") || "",
  room: null,        // {code, name, player_id?, player_token?, items:[...]}
  ws: null,
  ledger: {},
  you: null,
  host: null,
  players: [],
  spectator: false,  // read-only watcher (no player)
  admin: false,      // server confirmed our admin key for this room
  adminKey: localStorage.getItem("hl_admin") || "",
  adminEnabled: false,
  rooms: [],         // last fetched live-rooms list
  cooldownTimer: null,
  roomsTimer: null,
};

// Either the room's host OR a confirmed global admin gets management controls.
const isAdmin = () => !!state.admin || (state.you != null && state.you === state.host);

// ── REST ─────────────────────────────────────────────────────────────────
async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `error ${res.status}`);
  return data;
}

const entryName = () => ($("entry-name").value || "").trim() || "Spectator";

async function createRoom() {
  $("entry-err").textContent = "";
  try {
    enterRoom(await api("/api/rooms", { name: "Co-op", display_name: entryName() }));
  } catch (e) { $("entry-err").textContent = e.message; }
}

// Remember our player identity per room so re-joining from the browser reconnects
// as the SAME player (keeps items) instead of piling up duplicates.
function savedPlayers() {
  try { return JSON.parse(localStorage.getItem("hl_rooms") || "{}"); } catch (e) { return {}; }
}
function rememberPlayer(room) {
  const all = savedPlayers();
  all[room.code] = { player_id: room.player_id, player_token: room.player_token };
  localStorage.setItem("hl_rooms", JSON.stringify(all));
}

async function joinRoom(code) {
  $("entry-err").textContent = "";
  try {
    code = (code || $("entry-code").value || "").trim().toUpperCase();
    if (!code) { $("entry-err").textContent = "enter a room code"; return; }
    const saved = savedPlayers()[code];
    if (saved && saved.player_token) {
      try { enterRoom(await api(`/api/rooms/${code}/resume`, saved)); return; }
      catch (e) { /* server reset / unknown player → fall through to a fresh join */ }
    }
    enterRoom(await api(`/api/rooms/${code}/join`, { display_name: entryName() }));
  } catch (e) { $("entry-err").textContent = e.message; }
}

// ── home (entry + live rooms) ─────────────────────────────────────────────
function showHome() {
  hide("room"); show("entry"); show("rooms");
  $("whoami").textContent = "";
  if ($("admin-key")) $("admin-key").value = state.adminKey;
  loadRooms();
  if (!state.roomsTimer) state.roomsTimer = setInterval(() => {
    if (!$("rooms").classList.contains("hidden")) loadRooms();
  }, 8000);
}

async function loadRooms() {
  try {
    const data = await api("/api/rooms");
    state.adminEnabled = !!data.admin_enabled;
    state.rooms = data.rooms || [];
    renderRoomsList();
  } catch (e) { /* server unreachable — leave the last list */ }
}

function fmtAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - (ts || 0));
  if (s < 90) return "active now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function renderRoomsList() {
  const ul = $("rooms-list");
  const rooms = state.rooms;
  $("rooms-empty").classList.toggle("hidden", rooms.length > 0);
  const adminOn = state.adminEnabled && !!state.adminKey;
  // Public list exposes only the watch handle (pub_id) + name — never the join
  // code. Anyone can Watch; joining as a player needs the code (typed above).
  ul.innerHTML = rooms.map((r) => `
    <li>
      <span class="room-meta">
        <strong>${escapeHtml(r.name || "Co-op")}</strong>
        <span class="muted">${r.players} player${r.players === 1 ? "" : "s"} · ${fmtAgo(r.last_active)}</span>
      </span>
      <span class="room-row-actions">
        <button class="small" data-watch="${escapeHtml(r.pub_id)}" data-name="${escapeHtml(r.name || "Co-op")}">Watch</button>
        ${adminOn ? `<button class="small danger" data-del="${escapeHtml(r.pub_id)}">Delete</button>` : ""}
      </span>
    </li>`).join("");
  ul.querySelectorAll("button[data-watch]").forEach((b) => {
    b.onclick = () => watchRoom(b.getAttribute("data-watch"), b.getAttribute("data-name"));
  });
  ul.querySelectorAll("button[data-del]").forEach((b) => {
    b.onclick = () => deleteRoom(b.getAttribute("data-del"));
  });
  $("admin-unlock").classList.toggle("hidden", !state.adminEnabled);
  const badge = $("admin-state");
  badge.textContent = state.adminKey ? "admin mode" : "";
  badge.className = "pill ok" + (state.adminKey ? "" : " hidden");
}

async function deleteRoom(pub) {
  if (!confirm("Delete this room? This disconnects everyone and erases its progress.")) return;
  try {
    const res = await fetch(`/api/rooms/${encodeURIComponent(pub)}/delete`, {
      method: "POST", headers: { "X-Admin-Key": state.adminKey },
    });
    if (!res.ok) throw new Error(res.status === 403 ? "wrong admin key" : `delete failed (${res.status})`);
    loadRooms();
  } catch (e) { alert(e.message); }
}

// ── room entry (player) / watch (spectator) ───────────────────────────────
function enterRoom(room) {
  state.spectator = false;
  state.room = room;
  localStorage.setItem("hl_name", entryName());
  rememberPlayer(room);
  openRoomView(room.name, `${entryName()} · room ${room.code}`);
  renderAgentHelp(room);
  connectWS();
}

function watchRoom(pub, name) {
  state.spectator = true;
  state.room = { pub, name: name || "Co-op", items: [] };
  openRoomView(name || "Co-op", `watching · ${name || "a room"}`);
  connectWS();
}

function openRoomView(name, who) {
  hide("entry"); hide("rooms"); show("room");
  $("room-title").textContent = name || "Co-op";
  $("room-code").textContent = state.room.code || "";
  // watchers never see the private join code; players do (to share it)
  $("room-code-wrap").classList.toggle("hidden", state.spectator);
  $("whoami").textContent = who;
  // the "connect your game" helper only makes sense for a real player
  $("btn-connect-game").classList.toggle("hidden", state.spectator);
  $("agent-help").classList.add("hidden");
}

function leaveRoom() {
  if (state.ws) { try { state.ws.close(); } catch (e) {} }
  state.ws = null;
  state.room = null;
  state.spectator = false;
  state.you = null;
  state.admin = false;
  showHome();
}

function renderAgentHelp(room) {
  $("agent-cmd").textContent = "Most players just use Play.cmd — this is for manual setup.";
  const wsBase = location.origin.replace(/^http/, "ws");
  $("agent-cfg").textContent = JSON.stringify({
    server_http: location.origin,
    server_ws: wsBase + "/ws",
    room: room.code,
    player_id: room.player_id,
    player_token: room.player_token,
    transport: "emu",
    poll_interval: 1.0,
  }, null, 2);
}

// ── WebSocket ─────────────────────────────────────────────────────────────
function connectWS() {
  if (state.ws) { try { state.ws.close(); } catch (e) {} }
  const url = location.origin.replace(/^http/, "ws") + "/ws";
  const ws = new WebSocket(url);
  state.ws = ws;
  $("ws-status").textContent = "connecting…";
  $("ws-status").className = "pill";
  ws.onopen = () => {
    const hello = { type: "hello" };
    if (state.spectator) {
      hello.role = "spectator";
      hello.watch = state.room.pub;     // public handle, not the code
    } else {
      hello.role = "ui";
      hello.room = state.room.code;
      hello.player_id = state.room.player_id;
      hello.token = state.room.player_token;
    }
    if (state.adminKey) hello.admin_key = state.adminKey;
    ws.send(JSON.stringify(hello));
    $("ws-status").textContent = "live";
    $("ws-status").className = "pill ok";
  };
  ws.onclose = () => {
    $("ws-status").textContent = "disconnected";
    $("ws-status").className = "pill bad";
    if (state.room && state.ws === ws) setTimeout(() => { if (state.room) connectWS(); }, 2500);
  };
  ws.onmessage = (ev) => handleMsg(JSON.parse(ev.data));
}

function handleMsg(msg) {
  if (msg.type === "state") {
    if (msg.items) state.room.items = msg.items;   // catalog (needed by watchers)
    if (msg.name) { state.room.name = msg.name; $("room-title").textContent = msg.name; }
    state.ledger = msg.ledger;
    state.you = msg.you;
    state.host = msg.host;
    state.admin = !!msg.admin;
    state.spectator = !!msg.spectator;
    state.players = msg.players || [];
    state.cooldown_s = msg.cooldown_s;
    $("whoami").textContent = state.spectator
      ? `watching · ${state.room.name || "a room"}`
      : (state.admin ? `admin · ${state.room.name || "a room"}` : $("whoami").textContent);
    renderPlayers(msg.players);
    renderAdmin();
    renderGrid();
  } else if (msg.type === "event") {
    addLog(msg.text);
  } else if (msg.type === "reject") {
    addLog("⚠ " + msg.reason, true);
    if (/room (not found|closed)/i.test(msg.reason || "")) leaveRoom();
  }
}

function statusDot(p) {
  let cls = "off", title = "offline — agent not connected";
  if (p.agent && p.emu) { cls = "on"; title = "online — agent + emulator connected"; }
  else if (p.agent) { cls = "warn"; title = "agent up, emulator offline"; }
  return `<span class="dot ${cls}" title="${title}"></span>`;
}

function renderPlayers(players) {
  $("players").innerHTML = players
    .map((p) => `<li>${statusDot(p)}${p.id === state.you ? "★ " : ""}${escapeHtml(p.name)}</li>`)
    .join("");
}

function renderGrid() {
  const grid = $("grid");
  const items = (state.room && state.room.items) || [];
  grid.innerHTML = items.map((cat) => cardHtml(cat, state.ledger[cat.key])).join("");
  grid.querySelectorAll("button[data-claim]").forEach((b) => {
    b.onclick = () => claim(b.getAttribute("data-claim"));
  });
  grid.querySelectorAll("button[data-disc]").forEach((b) => {
    b.onclick = (ev) => {
      const [key, pid] = b.getAttribute("data-disc").split(":");
      const player_id = Number(pid);
      const e = state.ledger[key];
      if (ev.shiftKey) {
        const isOwner = e && e.owner === player_id;
        sendWS({ type: "admin_set_owner", item: key, player_id: isOwner ? null : player_id });
      } else {
        const found = !!(e && e.discovered.includes(player_id));
        sendWS({ type: "admin_set_discovered", item: key, player_id, found: !found });
      }
    };
  });
}

function renderAdmin() {
  const panel = $("admin");
  if (!isAdmin()) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const cd = $("admin-cooldown");
  if (document.activeElement !== cd) cd.value = Math.round(state.cooldown_s ?? 0);
  $("admin-player-list").innerHTML = state.players.map((p) => {
    const tag = p.id === state.host
      ? '<span class="muted">host</span>'
      : `<button class="small ghost" data-remove="${p.id}">remove</button>`;
    return `<li>${statusDot(p)}${escapeHtml(p.name)} ${tag}</li>`;
  }).join("");
  $("admin-player-list").querySelectorAll("button[data-remove]").forEach((b) => {
    b.onclick = () => {
      const pid = Number(b.getAttribute("data-remove"));
      const p = state.players.find((x) => x.id === pid);
      if (confirm(`Remove ${p ? p.name : "this player"} from the room?`))
        sendWS({ type: "admin_remove_player", player_id: pid });
    };
  });
}

function sendWS(obj) {
  if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj));
}

function shortName(name) {
  return String(name).replace(/\(.*?\)/g, "").trim() || String(name);
}

function adminChips(catKey, e) {
  if (!isAdmin() || !state.players.length) return "";
  const disc = (e && e.discovered) || [];
  const owner = e && e.owner;
  const chips = state.players.map((p) => {
    const on = disc.includes(p.id) ? "on" : "";
    const own = p.id === owner ? "owner" : "";
    return `<button class="chip ${on} ${own}" data-disc="${catKey}:${p.id}"
      title="click: toggle found/un-found · shift-click: set/clear owner">${escapeHtml(shortName(p.name))}</button>`;
  }).join("");
  return `<div class="admin-chips">${chips}</div>`;
}

function iconHtml(cat, e) {
  const img = (e && e.image) || (cat && cat.image);
  return img ? `<img class="item-icon" src="/static/items/${img}" alt="" loading="lazy" />` : "";
}

function cardHtml(cat, e) {
  const chips = adminChips(cat.key, e);
  if (!e) {
    // undiscovered by anyone
    return `<div class="item undiscovered">
      <div class="item-head">${iconHtml(cat, null)}<div class="item-name">${escapeHtml(cat.name)}</div></div>
      <div class="item-sub">undiscovered</div>
      ${chips}
    </div>`;
  }
  const mine = e.owner === state.you;
  const discovered = e.discovered.includes(state.you);
  const cd = e.cooldown_remaining;
  const onCooldown = cd > 0.05;
  const cls = mine ? "mine" : (e.owner ? "owned" : "unowned");
  const canPlay = !state.spectator && state.you != null;  // real player (not watcher/admin)
  let action = "";
  if (!canPlay) {
    action = "";                                 // watcher / admin: read-only board
  } else if (mine) {
    action = `<div class="held">✓ you hold this</div>`;
  } else if (!discovered) {
    action = `<div class="item-sub locked">find one to claim</div>`;
  } else if (onCooldown) {
    action = `<button disabled data-cd="${cat.key}">cooldown ${cd.toFixed(0)}s</button>`;
  } else {
    action = `<button data-claim="${cat.key}">Claim</button>`;
  }
  const owner = e.owner ? escapeHtml(e.owner_name || "?") : "unowned";
  const tier = e.tier && e.tier !== "—" ? ` <span class="tier">${escapeHtml(e.tier)}</span>` : "";
  return `<div class="item ${cls}">
    <div class="item-head">${iconHtml(cat, e)}<div class="item-name">${escapeHtml(cat.name)}${tier}</div></div>
    <div class="item-sub">held by <strong>${owner}</strong></div>
    ${action}
    ${chips}
  </div>`;
}

function claim(key) {
  sendWS({ type: "claim", item: key });
}

// tick cooldown countdowns locally between server pushes
function startCooldownTick() {
  if (state.cooldownTimer) clearInterval(state.cooldownTimer);
  state.cooldownTimer = setInterval(() => {
    let dirty = false;
    for (const k in state.ledger) {
      const e = state.ledger[k];
      if (e.cooldown_remaining > 0) {
        e.cooldown_remaining = Math.max(0, e.cooldown_remaining - 1);
        dirty = true;
      }
    }
    if (dirty && state.room) renderGrid();
  }, 1000);
}

function addLog(text, warn) {
  const li = document.createElement("li");
  li.textContent = text;
  if (warn) li.className = "warn";
  $("log").prepend(li);
  while ($("log").children.length > 60) $("log").lastChild.remove();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ── wire up ─────────────────────────────────────────────────────────────
$("btn-create").onclick = createRoom;
$("btn-join").onclick = () => joinRoom();
$("btn-leave").onclick = leaveRoom;
$("btn-connect-game").onclick = () => $("agent-help").classList.toggle("hidden");
$("rooms-refresh").onclick = loadRooms;
$("admin-cooldown-apply").onclick = () =>
  sendWS({ type: "admin_set_cooldown", seconds: Number($("admin-cooldown").value) || 0 });
$("admin-key").addEventListener("input", (e) => {
  state.adminKey = e.target.value.trim();
  localStorage.setItem("hl_admin", state.adminKey);
  renderRoomsList();
});

if ($("entry-name") && state.name) $("entry-name").value = state.name;
startCooldownTick();
showHome();
// deep link: /?watch=PUB opens straight into watching (pub_id is case-sensitive)
const watchParam = (new URLSearchParams(location.search).get("watch") || "").trim();
if (watchParam) watchRoom(watchParam, null);
