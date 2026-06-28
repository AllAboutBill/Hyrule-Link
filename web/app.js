"use strict";
// HyruleLink single-page UI: auth → lobby → room. Talks REST for accounts/rooms
// and one WebSocket (role "ui") for live ledger state + claim requests.

const $ = (id) => document.getElementById(id);
const show = (id) => $(id).classList.remove("hidden");
const hide = (id) => $(id).classList.add("hidden");

const state = {
  name: localStorage.getItem("hl_name") || "",
  room: null,        // {code, name, player_id, player_token, items:[...]}
  ws: null,
  ledger: {},
  you: null,
  host: null,
  players: [],
  cooldownTimer: null,
};

const isAdmin = () => state.you != null && state.you === state.host;

// ── REST (no auth — name + room code) ────────────────────────────────────
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

async function joinRoom() {
  $("entry-err").textContent = "";
  try {
    const code = $("entry-code").value.trim().toUpperCase();
    if (!code) { $("entry-err").textContent = "enter a room code"; return; }
    enterRoom(await api(`/api/rooms/${code}/join`, { display_name: entryName() }));
  } catch (e) { $("entry-err").textContent = e.message; }
}

// ── room ────────────────────────────────────────────────────────────────
function enterRoom(room) {
  state.room = room;
  localStorage.setItem("hl_name", entryName());
  hide("entry"); show("room");
  $("room-title").textContent = room.name;
  $("room-code").textContent = room.code;
  $("whoami").textContent = `${entryName()} · room ${room.code}`;
  renderAgentHelp(room);
  connectWS();
}

function leaveRoom() {
  if (state.ws) state.ws.close();
  state.room = null;
  hide("room"); show("entry");
  $("whoami").textContent = "";
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

function connectWS() {
  if (state.ws) state.ws.close();
  const url = location.origin.replace(/^http/, "ws") + "/ws";
  const ws = new WebSocket(url);
  state.ws = ws;
  $("ws-status").textContent = "connecting…";
  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "hello", role: "ui",
      room: state.room.code, player_id: state.room.player_id,
      token: state.room.player_token,
    }));
    $("ws-status").textContent = "live";
    $("ws-status").className = "pill ok";
  };
  ws.onclose = () => {
    $("ws-status").textContent = "disconnected";
    $("ws-status").className = "pill bad";
    if (state.room) setTimeout(connectWS, 2500);
  };
  ws.onmessage = (ev) => handleMsg(JSON.parse(ev.data));
}

function handleMsg(msg) {
  if (msg.type === "state") {
    state.ledger = msg.ledger;
    state.you = msg.you;
    state.host = msg.host;
    state.players = msg.players || [];
    state.cooldown_s = msg.cooldown_s;
    renderPlayers(msg.players);
    renderAdmin();
    renderGrid();
  } else if (msg.type === "event") {
    addLog(msg.text);
  } else if (msg.type === "reject") {
    addLog("⚠ " + msg.reason, true);
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
  const items = state.room.items; // full catalog order
  grid.innerHTML = items.map((cat) => {
    const e = state.ledger[cat.key];
    return cardHtml(cat, e);
  }).join("");
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

function cardHtml(cat, e) {
  const chips = adminChips(cat.key, e);
  if (!e) {
    // undiscovered by anyone
    return `<div class="item undiscovered">
      <div class="item-name">${escapeHtml(cat.name)}</div>
      <div class="item-sub">undiscovered</div>
      ${chips}
    </div>`;
  }
  const mine = e.owner === state.you;
  const discovered = e.discovered.includes(state.you);
  const cd = e.cooldown_remaining;
  const onCooldown = cd > 0.05;
  const cls = mine ? "mine" : (e.owner ? "owned" : "unowned");
  let action = "";
  if (mine) {
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
    <div class="item-name">${escapeHtml(cat.name)}${tier}</div>
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
$("btn-join").onclick = joinRoom;
$("btn-leave").onclick = leaveRoom;
$("btn-connect-game").onclick = () => $("agent-help").classList.toggle("hidden");
$("admin-cooldown-apply").onclick = () =>
  sendWS({ type: "admin_set_cooldown", seconds: Number($("admin-cooldown").value) || 0 });

if ($("entry-name") && state.name) $("entry-name").value = state.name;
startCooldownTick();
show("entry");
