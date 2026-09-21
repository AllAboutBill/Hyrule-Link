/* HyruleLink - the room page.
 *
 *   room.html?room=CODE   a player (or someone about to join, or a watcher
 *                         who has the code)
 *   room.html?watch=PUB   a watcher: the public handle, never the code
 *
 * A player's seat (player_id + player_token) lives in this browser under
 * hyrulelink.rooms, so reopening the link resumes the same player. The page
 * then keeps two sockets to the room server:
 *
 *   ui      always: the board, claims, the host controls. Every change
 *           arrives as a whole state document and the page is redrawn from it.
 *   agent   only through game.js, only once a game is attached on this PC,
 *           and only while "Link a game on this browser" is on. It is the
 *           same role the desktop app's agent uses.
 *
 * Watchers open one spectator socket and never start game.js.
 *
 * Everything another person typed (names, the room name, server lines) goes
 * in through textContent or esc().
 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var params = new URLSearchParams(location.search);
  var code = (params.get('room') || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  var watchParam = (params.get('watch') || '').trim();      // pub_id is case-sensitive
  var Policy = window.HyruleLinkClaimPolicy;

  var MODE_LABELS = { normal: 'Normal', hot_potato: 'Hot Potato', chaos: 'Chaos', custom: 'Custom' };
  var PRESET_LABELS = { normal: 'Normal', hot_potato: 'Hot Potato', chaos: 'Chaos', cutthroat: 'Cutthroat',
    lease: 'Lease', raid: 'Raid', siege: 'Siege' };
  var LOG_MAX = 60;
  var UI_RETRY_MS = 2500;

  // ------------------------------------------------------------ storage

  /* Values are JSON, as on EtherNet's pages. A raw string left by an older
     page (hl_name was one) reads back as itself. */
  var store = {
    get: function (k, d) {
      var v;
      try { v = localStorage.getItem(k); } catch (e) { return d; }
      if (v == null) return d;
      try { return JSON.parse(v); } catch (e) { return v; }
    },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } }
  };

  function raw(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  /* hyrulelink.name = "Ana"; hyrulelink.rooms = {CODE: {player_id,
     player_token, name, title, t}}. The old page kept hl_name (plain text)
     and hl_rooms ({CODE: {player_id, player_token}}): each is copied over
     only while its new key is missing, and the old keys stay where they are,
     so a seat forgotten here never comes back. The same rules as home.js. */
  function importLegacy() {
    if (raw('hyrulelink.name') == null) {
      var old = String(raw('hl_name') || '').trim();
      // "Spectator" and "Player" were the old page's stand-ins, not names
      if (old && !/^(spectator|player)$/i.test(old)) store.set('hyrulelink.name', old.slice(0, 40));
    }
    if (raw('hyrulelink.rooms') == null && raw('hl_rooms') != null) {
      var legacy = store.get('hl_rooms', null), rooms = {}, who = savedName();
      if (legacy && typeof legacy === 'object' && !Array.isArray(legacy)) {
        Object.keys(legacy).forEach(function (c) {
          var s = legacy[c] || {}, up = String(c).toUpperCase(), id = Number(s.player_id);
          if (!/^[A-Z0-9]{4,16}$/.test(up) || !(id > 0) || typeof s.player_token !== 'string' || !s.player_token) return;
          rooms[up] = { player_id: id, player_token: s.player_token, name: who, title: '', t: 0 };
        });
      }
      store.set('hyrulelink.rooms', rooms);
    }
  }
  function allSeats() {
    var all = store.get('hyrulelink.rooms', null);
    return all && typeof all === 'object' && !Array.isArray(all) ? all : {};
  }
  function seatGet() {
    var s = allSeats()[code];
    return s && s.player_id && s.player_token ? s : null;
  }
  function seatSet(s) {
    var all = allSeats();
    if (s) all[code] = s;
    else if (code in all) delete all[code];
    else return;
    store.set('hyrulelink.rooms', all);
  }
  function savedName() {
    var n = store.get('hyrulelink.name', '');
    return typeof n === 'string' ? n : '';
  }
  function flag(key) { return store.get(key, 'on') !== 'off'; }
  function setFlag(key, on) { store.set(key, on ? 'on' : 'off'); }
  function linkOn() { return flag('hyrulelink.link'); }
  function hudOn() { return flag('hyrulelink.hud'); }

  // -------------------------------------------------------------- state

  var mode = null;          // 'player' | 'watch'
  var seat = null;          // {player_id, player_token, name, title, t}
  var pubId = '';           // the room's public handle
  var info = null;          // GET api/rooms/CODE, for the join cover
  var ws = null, closing = false, dead = false, retryTimer = null;
  var st = null;            // the last state document
  var catalog = [];         // [{key, name, image}] in catalog order
  var game = null;          // HLGame handle while this browser links a game
  var agentOpen = false;
  var lastLine = null;
  var RULE_DEFAULTS = {
    claiming: true, require_found_to_claim: true, open_season_scope: 'owned',
    steal_cooldown_s: 5, cooldown_scope: 'item', steal_back_lock_s: 0, steal_budget_per_min: 0,
    hold_limit_s: 0, hold_expiry: 'next_finder', tenure_lock_s: 0, idle_release_s: 0,
    borrow_s: 0, borrow_revert: 'prev_owner', auto_shuffle_s: 0, shuffle_scope: 'all', shared_discovery: false
  };
  var RULE_PRESETS = {
    normal: {},
    hot_potato: { claiming: false, hold_limit_s: 120, hold_expiry: 'next_finder' },
    chaos: { claiming: false, auto_shuffle_s: 120, shuffle_scope: 'all' },
    cutthroat: { require_found_to_claim: false, open_season_scope: 'owned', cooldown_scope: 'thief',
      steal_cooldown_s: 20, steal_budget_per_min: 3, steal_back_lock_s: 30 },
    lease: { claiming: true, hold_limit_s: 300, hold_expiry: 'release' },
    raid: { require_found_to_claim: false, borrow_s: 120, borrow_revert: 'prev_owner' },
    siege: { claiming: true, tenure_lock_s: 240 }
  };

  function isAdmin() { return !!st && (!!st.admin || (st.you != null && st.you === st.host)); }
  function send(msg) {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(msg)); return true; }
    toast('Not connected to the room. Trying again.', 'bad');
    return false;
  }

  // ---------------------------------------------------------------- dom

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') n.textContent = attrs[k];
      else if (k === 'on') Object.keys(attrs.on).forEach(function (ev) { n.addEventListener(ev, attrs.on[ev]); });
      else if (attrs[k] === true) n.setAttribute(k, '');
      else if (attrs[k] !== false && attrs[k] != null) n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(text, kind) {
    var t = el('div', { class: 'toast' + (kind ? ' ' + kind : ''), text: text });
    $('toasts').appendChild(t);
    setTimeout(function () { t.remove(); }, kind === 'bad' ? 5200 : 3200);
    while ($('toasts').childNodes.length > 4) $('toasts').firstChild.remove();
  }
  function stamp(ms) {
    var d = new Date(ms);
    return (d.getHours() < 10 ? '0' : '') + d.getHours() + ':' + (d.getMinutes() < 10 ? '0' : '') + d.getMinutes();
  }
  function clock(s) {
    s = Math.max(0, Math.ceil(s));
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }
  function left(until) { return until == null ? 0 : Math.max(0, (until - Date.now()) / 1000); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function shortName(name) { return String(name).replace(/\(.*?\)/g, '').trim() || String(name); }
  function setStrip(node, text, idle) {
    node.textContent = '';
    node.className = 'strip' + (idle ? ' idle' : '');
    var w = 20, s = text.length > w ? text.slice(0, w) : text;
    var pad = Math.floor((w - s.length) / 2);
    for (var i = 0; i < w; i++) node.appendChild(el('i', { text: s[i - pad] || '' }));
  }
  function copy(text, done) {
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(done, function () {
      window.prompt('Copy this:', text);
    });
  }
  /* A button that needs a second click within a few seconds. */
  function armed(until) { return Date.now() < until; }

  // -------------------------------------------------------- the activity log

  function addLog(text, kind, ms) {
    var li = el('li', { class: kind || null }, [el('span', { class: 't', text: stamp(ms || Date.now()) }), String(text)]);
    var log = $('log');
    log.insertBefore(li, log.firstChild);
    while (log.childNodes.length > LOG_MAX) log.lastChild.remove();
    $('logEmpty').hidden = true;
  }

  // ------------------------------------------------------------ joining

  async function post(path, body) {
    var r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    var b = await r.json().catch(function () { return {}; });
    return { ok: r.ok, status: r.status, body: b };
  }
  function detail(b, fallback) { return (b && typeof b.detail === 'string' && b.detail) || fallback; }

  function showMsg(title, sub, retry) {
    $('layout').hidden = true;
    $('joinCover').hidden = true;
    $('msgCover').hidden = false;
    $('msgTitle').textContent = title;
    $('msgSub').textContent = sub;
    $('msgRetry').hidden = !retry;
  }
  function showGone() {
    seatSet(null);
    showMsg('That room is gone', 'No room has that code. Rooms are forgotten two weeks after the last activity.', false);
  }
  function showOffline() {
    showMsg('The server is not answering', 'Check your connection and try again.', true);
  }

  async function lookup(note) {
    try {
      var r = await fetch('api/rooms/' + code);
      if (r.status === 404) { showGone(); return; }
      var b = await r.json().catch(function () { return {}; });
      if (!r.ok) { showMsg('That did not work', detail(b, 'The server said no (' + r.status + ').'), true); return; }
      info = b;
      showJoin(note);
    } catch (e) {
      showOffline();
    }
  }

  function showJoin(note) {
    $('layout').hidden = true;
    $('msgCover').hidden = true;
    $('joinCover').hidden = false;
    $('joinLabel').textContent = (info && info.name) || ('Room ' + code);
    var n = info && info.players ? info.players.length : 0;
    $('joinSub').textContent = n ? plural(n, 'player', 'players') + ' in the room.' : 'Nobody is in the room yet.';
    $('joinName').value = savedName();
    $('joinErr').hidden = !note;
    $('joinErr').textContent = note || '';
    $('btnJoin').disabled = false;
    $('joinName').focus();
  }

  async function join() {
    var name = $('joinName').value.trim();
    if (!name) { $('joinName').focus(); return; }
    $('joinErr').hidden = true;
    $('btnJoin').disabled = true;
    try {
      var r = await post('api/rooms/' + code + '/join', { display_name: name });
      if (r.status === 404) { showGone(); return; }
      if (!r.ok) throw new Error(detail(r.body, 'That did not work.'));
      store.set('hyrulelink.name', name);
      enterPlayer(r.body);
    } catch (e) {
      $('joinErr').textContent = e.message === 'Failed to fetch' ? 'The server is not answering.' : e.message;
      $('joinErr').hidden = false;
    } finally {
      $('btnJoin').disabled = false;
    }
  }

  function playerName(doc, id) {
    var list = (doc && doc.players) || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i].name;
    return '';
  }

  function enterPlayer(p) {
    mode = 'player';
    dead = false; closing = false;
    seat = { player_id: p.player_id, player_token: p.player_token,
      name: playerName(p, p.player_id), title: p.name || '', t: Date.now() };
    seatSet(seat);
    pubId = p.pub_id || '';
    if (p.items && p.items.length) setCatalog(p.items);
    $('joinCover').hidden = true;
    $('msgCover').hidden = true;
    $('watchBanner').hidden = true;
    $('btnInvite').hidden = false;
    $('btnSettings').hidden = false;
    $('roomCode').textContent = code;
    setTitle(p.name);
    connect();
    if (linkOn()) startGame();
    drawGame();
  }

  function enterWatch(pub) {
    mode = 'watch';
    dead = false; closing = false;
    pubId = pub;
    stopGame();
    if (!catalog.length) setCatalog(fallbackCatalog());
    $('joinCover').hidden = true;
    $('msgCover').hidden = true;
    $('btnInvite').hidden = true;
    $('btnSettings').hidden = true;
    $('watchBanner').hidden = false;
    $('btnWatchJoin').hidden = !code;
    connect();
    drawGame();
  }

  // ---------------------------------------------------------- websocket

  function wsUrl() {
    var u = new URL('ws', location.href);
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    u.search = ''; u.hash = '';
    return u.href;
  }
  function netLamp(kind) { $('netLamp').className = 'net-lamp' + (kind ? ' ' + kind : ''); }

  function dropSocket() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    var sock = ws;
    ws = null;
    if (sock) { try { sock.close(); } catch (e) { /* gone */ } }
  }

  function connect() {
    dropSocket();
    if (closing || dead) return;
    var sock;
    try { sock = new WebSocket(wsUrl()); } catch (e) { retryTimer = setTimeout(connect, UI_RETRY_MS); return; }
    ws = sock;
    netLamp('');
    sock.onopen = function () {
      if (ws !== sock) return;
      var hello = mode === 'watch'
        ? { type: 'hello', role: 'spectator', watch: pubId }
        : { type: 'hello', role: 'ui', room: code, player_id: seat.player_id, token: seat.player_token };
      sock.send(JSON.stringify(hello));
    };
    sock.onmessage = function (ev) {
      if (ws !== sock) return;
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m && typeof m === 'object') onMessage(m);
    };
    sock.onerror = function () {};
    sock.onclose = function () {
      if (ws !== sock) return;
      ws = null;
      netLamp('bad');
      if (!closing && !dead) retryTimer = setTimeout(connect, UI_RETRY_MS);
    };
  }

  function onMessage(m) {
    if (m.type === 'state') {
      netLamp('ok');
      applyState(m);
    } else if (m.type === 'event') {
      /* the server marks a game that could not apply an item with a warning sign */
      addLog(m.text, /^\u26a0/.test(String(m.text || '')) ? 'warn' : null, m.ts ? m.ts * 1000 : null);
    } else if (m.type === 'reject') {
      onReject(String(m.reason || ''));
    }
  }

  function onReject(reason) {
    if (/room (not found|closed)/i.test(reason)) {
      dead = true;
      if (mode === 'player') seatSet(null);
      stopGame();
      dropSocket();
      location.href = './';
      return;
    }
    if (/bad room\/player token/i.test(reason)) {
      /* removed by the host, or the seat was never valid here */
      dead = true;
      seatSet(null);
      seat = null;
      stopGame();
      dropSocket();
      st = null;
      lookup('You are no longer in this room. Join again to play.');
      return;
    }
    toast(reason, 'bad');
    addLog(reason, 'warn');
  }

  // ------------------------------------------------------------ drawing

  function fallbackCatalog() {
    var I = window.HLItems;
    if (!I) return [];
    return I.ITEMS.map(function (it) { return { key: it.key, name: it.name, image: I.itemImage(it.key, it.present) }; });
  }

  function setTitle(name) {
    var t = name || 'Co-op';
    $('roomTitle').textContent = t;
    document.title = t + ' - HyruleLink';
  }

  function applyState(m) {
    var now = Date.now();
    if (m.items && m.items.length) setCatalog(m.items);
    if (m.rule_defaults) RULE_DEFAULTS = m.rule_defaults;
    if (m.rule_presets) RULE_PRESETS = m.rule_presets;
    var ledger = m.ledger || {};
    Object.keys(ledger).forEach(function (k) {
      var e = ledger[k];
      e.discovered = e.discovered || [];
      e._cd = now + (e.cooldown_remaining || 0) * 1000;
      e._hold = e.hold_remaining != null ? now + e.hold_remaining * 1000 : null;
      e._borrow = e.borrow_remaining != null ? now + e.borrow_remaining * 1000 : null;
      e._tenure = e.tenure_remaining != null ? now + e.tenure_remaining * 1000 : null;
    });
    m.ledger = ledger;
    m.players = m.players || [];
    m._shuffle = m.shuffle_remaining > 0 ? now + m.shuffle_remaining * 1000 : null;
    st = m;
    if (mode === 'player' && seat) {
      var me = playerName(m, seat.player_id);
      if (seat.title !== m.name || (me && seat.name !== me)) {
        seat.title = m.name; if (me) seat.name = me; seat.t = Date.now();
        seatSet(seat);
      }
    }
    setTitle(m.name);
    $('roomCount').textContent = mode === 'watch' ? 'Watching' : plural(m.players.length, 'player', 'players');
    $('layout').hidden = false;
    drawModeBanner();
    drawGrid();
    drawPlayers();
    drawHost();
    drawGame();
  }

  function setCatalog(items) {
    var keys = items.map(function (c) { return c.key; }).join(',');
    if (keys === catalog.map(function (c) { return c.key; }).join(',')) { catalog = items; return; }
    catalog = items;
    var grid = $('grid');
    grid.textContent = '';
    catalog.forEach(function (c) { grid.appendChild(el('div', { class: 'item undiscovered', 'data-key': c.key })); });
  }

  /* the mode banner: Hot Potato, Chaos, Custom */
  function drawModeBanner() {
    var show = !!st && !!st.mode && st.mode !== 'normal';
    $('modeBanner').hidden = !show;
    if (!show) return;
    $('modeName').textContent = MODE_LABELS[st.mode] || 'Custom';
    $('modeSummary').textContent = st.rules_summary || '';
    var every = st.rules && st.rules.auto_shuffle_s;
    if (every) {
      /* the server re-arms its own timer even when a shuffle moves nothing,
         and then sends no state: re-arm this one the same way */
      if (st._shuffle == null) st._shuffle = Date.now() + every * 1000;
      while (st._shuffle <= Date.now()) st._shuffle += every * 1000;
    }
    $('modeNext').hidden = st._shuffle == null;
    if (st._shuffle != null) $('modeNextClock').textContent = clock(left(st._shuffle));
  }

  /* ------------------------------------------------------------ the board

     Every card is redrawn from a string, but a card whose only change is a
     countdown keeps its nodes and has just its numbers rewritten, so the
     once-a-second tick never swaps a button out from under a click. */

  var tvVals = [];
  var TV = /(<span class="tv">)[^<]*(<\/span>)/g;
  function tv(text) { tvVals.push(String(text)); return '<span class="tv">' + esc(text) + '</span>'; }

  function isLocked(e) {
    if (e.owner == null) return false;
    return !!e.locked || (e._tenure != null && left(e._tenure) <= 0);
  }

  function hostChips(key, e) {
    if (!isAdmin() || !st.players.length) return '';
    var disc = (e && e.discovered) || [];
    var owner = e ? e.owner : null;
    return '<div class="chips">' + st.players.map(function (p) {
      var on = disc.indexOf(p.id) >= 0;
      return '<button class="chip' + (on ? ' on' : '') + (p.id === owner ? ' owner' : '') + '" type="button"'
        + ' data-chip="' + esc(key) + ':' + p.id + '" aria-pressed="' + on + '"'
        + ' title="' + esc(p.name) + (on ? ' found it' : ' has not found it') + (p.id === owner ? ' and holds it' : '')
        + '. Click: found or not. Shift-click: holder.">' + esc(shortName(p.name)) + '</button>';
    }).join('') + '</div>';
  }

  function claimButton(key) {
    return '<button class="btn small" type="button" data-claim="' + esc(key) + '">Claim</button>';
  }

  /* -> [className, innerHTML]; the logic is app.js cardHtml's */
  function card(cat, e) {
    var you = st ? st.you : null;
    var rules = (st && st.rules) || {};
    var claiming = !!st && st.claiming !== false;
    var canPlay = mode === 'player' && you != null && claiming;
    var allows = Policy.canClaim(e || null, rules, you);
    var chips = hostChips(cat.key, e);
    var img = (e && e.image) || cat.image;
    var icon = img ? '<img class="item-icon" src="items/' + esc(img) + '" alt="" loading="lazy">' : '';
    var head, sub, extra = '', action = '', cls;
    if (!e) {
      cls = 'undiscovered';
      head = esc(cat.name);
      sub = 'Not found yet';
      if (canPlay && allows) action = claimButton(cat.key);
    } else {
      var mine = you != null && e.owner === you;
      var locked = isLocked(e);
      var cd = left(e._cd);
      cls = mine ? 'mine' : (e.owner != null ? 'owned' : 'unowned');
      if (mine && !claiming) action = '<div class="held">Yours</div>';
      else if (!canPlay) action = '';
      else if (mine) action = '<div class="held">You hold this</div>';
      else if (locked) action = '<div class="item-sub held">Secured</div>';
      else if (!allows) action = '<div class="item-sub locked">Find one to claim</div>';
      else if (cd > 0.05) action = '<button class="btn small" type="button" disabled>Cooldown ' + tv(Math.ceil(cd)) + 's</button>';
      else action = claimButton(cat.key);
      if (e.owner != null && e._hold != null) {
        var every = rules.hold_limit_s;
        if (every && !locked) while (e._hold <= Date.now()) e._hold += every * 1000;   // a hold that restarted without a state
        extra += '<div class="item-sub hold">Hold ' + tv(clock(left(e._hold))) + '</div>';
      }
      if (e._borrow != null) extra += '<div class="item-sub hold">Borrowed ' + tv(clock(left(e._borrow))) + '</div>';
      if (e.owner != null && e._tenure != null && !locked) extra += '<div class="item-sub hold">Secured in ' + tv(clock(left(e._tenure))) + '</div>';
      if (locked && (!canPlay || mine)) extra += '<div class="item-sub held">Secured</div>';
      var tier = e.owner != null && e.tier && e.tier !== '\u2014' && e.tier !== 'owned'
        ? ' <span class="tier">' + esc(e.tier) + '</span>' : '';
      head = esc(cat.name) + tier;
      sub = e.owner != null ? 'Held by <b>' + esc(e.owner_name || '?') + '</b>' : 'Nobody holds it';
    }
    var foot = action || chips ? '<div class="item-foot">' + action + chips + '</div>' : '';
    return ['item ' + cls,
      '<div class="item-head">' + icon + '<div class="item-name">' + head + '</div></div>'
      + '<div class="item-sub">' + sub + '</div>' + extra + foot];
  }

  function drawGrid() {
    if (!st) return;
    var nodes = $('grid').children;
    for (var i = 0; i < catalog.length && i < nodes.length; i++) {
      var cat = catalog[i], node = nodes[i];
      tvVals = [];
      var c = card(cat, st.ledger[cat.key]);
      var vals = tvVals;
      var shape = c[0] + '|' + c[1].replace(TV, '$1$2');
      if (node._shape === shape) {
        var spans = node.querySelectorAll('.tv');
        for (var j = 0; j < spans.length && j < vals.length; j++) {
          if (spans[j].textContent !== vals[j]) spans[j].textContent = vals[j];
        }
        continue;
      }
      node._shape = shape;
      node.className = c[0];
      node.innerHTML = c[1];
    }
  }

  $('grid').addEventListener('click', function (ev) {
    var b = ev.target.closest('button');
    if (!b || b.disabled || !st) return;
    if (b.dataset.claim) {
      send({ type: 'claim', item: b.dataset.claim });
    } else if (b.dataset.chip) {
      var parts = b.dataset.chip.split(':');
      var key = parts[0], pid = Number(parts[1]);
      var e = st.ledger[key];
      if (ev.shiftKey) {
        send({ type: 'admin_set_owner', item: key, player_id: e && e.owner === pid ? null : pid });
      } else {
        var found = !!(e && e.discovered.indexOf(pid) >= 0);
        send({ type: 'admin_set_discovered', item: key, player_id: pid, found: !found });
      }
    }
  });

  /* ---------------------------------------------------------- the players */

  function status(p) {
    if (p.agent && p.emu) return ['on', 'Game linked', 'game linked'];
    if (p.agent) return ['warn', 'Linked, no game running', 'no game'];
    return ['off', 'No game link', 'not linked'];
  }

  function playerRow(p, remove) {
    var s = status(p);
    var li = el('li', { class: p.id === st.you ? 'me' : null }, [
      el('span', { class: 'dot ' + s[0], title: s[1] }),
      el('span', { class: 'name', text: p.name, title: p.name }),
      p.id === st.host ? el('span', { class: 'pill host', text: 'Host' }) : null,
      el('small', { text: s[2] })
    ]);
    if (remove && p.id !== st.host) li.appendChild(remove);
    return li;
  }

  function drawPlayers() {
    var ul = $('players');
    ul.textContent = '';
    st.players.forEach(function (p) { ul.appendChild(playerRow(p, null)); });
    $('playersCount').textContent = String(st.players.length);
  }

  /* ------------------------------------------------------------- the host */

  var removeArm = { id: null, until: 0 };

  function setIfIdle(node, v) { if (document.activeElement !== node) node.value = v; }

  function drawHost() {
    var on = isAdmin();
    $('hostPanel').hidden = !on;
    $('setHost').hidden = !on;
    if (!on) return;
    setIfIdle($('hostName'), st.name || '');
    setIfIdle($('hostMode'), st.mode || 'normal');
    setIfIdle($('hostCooldown'), Math.round(st.cooldown_s || 0));
    setIfIdle($('hostShuffle'), Math.round(st.shuffle_s || 120));
    hostRows();
    drawHostPlayers();
  }

  function drawHostPlayers() {
    var ul = $('hostPlayers');
    ul.textContent = '';
    st.players.forEach(function (p) {
      var hot = removeArm.id === p.id && armed(removeArm.until);
      var b = el('button', { class: 'btn small quiet danger' + (hot ? ' armed' : ''), type: 'button',
        text: hot ? 'Click again' : 'Remove', title: 'Remove ' + p.name + ' from the room',
        on: { click: function () {
          if (removeArm.id === p.id && armed(removeArm.until)) {
            removeArm = { id: null, until: 0 };
            send({ type: 'admin_remove_player', player_id: p.id });
          } else {
            removeArm = { id: p.id, until: Date.now() + 4000 };
            setTimeout(function () { if (st) drawHostPlayers(); }, 4100);
          }
          drawHostPlayers();
        } } });
      ul.appendChild(playerRow(p, b));
    });
  }

  function hostRows() {
    var m = $('hostMode').value;
    $('hostTimingRow').hidden = m === 'custom';
    $('hostCustomRow').hidden = m !== 'custom';
    $('hostCooldown').hidden = m !== 'normal';
    $('hostShuffle').hidden = m === 'normal' || m === 'custom';
    $('hostTimingLabel').textContent = m === 'normal' ? 'Steal cooldown' : (m === 'hot_potato' ? 'Pass every' : 'Shuffle every');
  }

  /* One Apply for the mode row, sending only what changed, so re-applying
     the same mode does not restart the shuffle timer. */
  function applyMode() {
    if (!st) return;
    var m = $('hostMode').value;
    if (m === 'custom') { openRules(); return; }
    var shuffle = Number($('hostShuffle').value) || 120;
    var cooldown = Number($('hostCooldown').value) || 0;
    var sent = false;
    var modeChanged = m !== st.mode;
    var shuffleChanged = m !== 'normal' && Math.round(shuffle) !== Math.round(st.shuffle_s || 120);
    if (modeChanged || shuffleChanged) sent = send({ type: 'admin_set_mode', mode: m, seconds: shuffle }) || sent;
    if (m === 'normal' && Math.round(cooldown) !== Math.round(st.cooldown_s || 0)) {
      sent = send({ type: 'admin_set_cooldown', seconds: cooldown }) || sent;
    }
    if (!sent && ws && ws.readyState === 1) toast('Nothing to change.');
  }

  $('hostMode').addEventListener('change', function () {
    hostRows();
    if (this.value === 'custom') openRules();
  });
  $('hostModeApply').addEventListener('click', applyMode);
  ['hostCooldown', 'hostShuffle'].forEach(function (id) {
    $(id).addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); applyMode(); } });
  });
  $('hostCustomize').addEventListener('click', openRules);
  $('hostNameForm').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var v = $('hostName').value.trim();
    if (!v) { $('hostName').focus(); return; }
    if (st && v === st.name) { toast('That is already the name.'); return; }
    send({ type: 'admin_set_name', name: v });
    $('hostName').blur();
  });
  $('hostResetWord').addEventListener('input', function () {
    $('hostReset').disabled = this.value.trim().toUpperCase() !== 'RESET';
  });
  $('hostReset').addEventListener('click', function () {
    if ($('hostResetWord').value.trim().toUpperCase() !== 'RESET') return;
    if (send({ type: 'admin_reset_room' })) {
      $('hostResetWord').value = '';
      $('hostReset').disabled = true;
    }
  });

  /* ------------------------------------------------------ custom ruleset */

  var NUM_RULES = ['steal_cooldown_s', 'steal_back_lock_s', 'steal_budget_per_min', 'hold_limit_s',
    'tenure_lock_s', 'idle_release_s', 'borrow_s', 'auto_shuffle_s'];

  function rulesFromForm() {
    return {
      claiming: $('r-claiming').checked, require_found_to_claim: $('r-found').checked,
      open_season_scope: $('r-openseason').value,
      steal_cooldown_s: Number($('r-cd').value) || 0, cooldown_scope: $('r-cdscope').value,
      steal_back_lock_s: Number($('r-sbl').value) || 0, steal_budget_per_min: Number($('r-budget').value) || 0,
      hold_limit_s: Number($('r-hold').value) || 0, hold_expiry: $('r-holdexp').value,
      tenure_lock_s: Number($('r-tenure').value) || 0, idle_release_s: Number($('r-idle').value) || 0,
      borrow_s: Number($('r-borrow').value) || 0, borrow_revert: $('r-borrowrev').value,
      auto_shuffle_s: Number($('r-shuffle').value) || 0, shuffle_scope: $('r-shufscope').value,
      shared_discovery: $('r-shared').checked
    };
  }
  function rulesToForm(r) {
    r = Object.assign({}, RULE_DEFAULTS, r || {});
    $('r-claiming').checked = !!r.claiming; $('r-found').checked = !!r.require_found_to_claim;
    $('r-openseason').value = r.open_season_scope;
    $('r-cd').value = Math.round(r.steal_cooldown_s); $('r-cdscope').value = r.cooldown_scope;
    $('r-sbl').value = Math.round(r.steal_back_lock_s); $('r-budget').value = Math.round(r.steal_budget_per_min);
    $('r-hold').value = Math.round(r.hold_limit_s); $('r-holdexp').value = r.hold_expiry;
    $('r-tenure').value = Math.round(r.tenure_lock_s); $('r-idle').value = Math.round(r.idle_release_s);
    $('r-borrow').value = Math.round(r.borrow_s); $('r-borrowrev').value = r.borrow_revert;
    $('r-shuffle').value = Math.round(r.auto_shuffle_s); $('r-shufscope').value = r.shuffle_scope;
    $('r-shared').checked = !!r.shared_discovery;
    refreshRules();
  }
  function sameRules(a, b) {
    return Object.keys(RULE_DEFAULTS).every(function (k) {
      if (NUM_RULES.indexOf(k) >= 0) return Math.round(Number(a[k]) || 0) === Math.round(Number(b[k]) || 0);
      return a[k] === b[k];
    });
  }
  function summarizeRules(r) {
    var p = [];
    if (r.claiming) {
      p.push(r.require_found_to_claim ? 'claim found items'
        : (r.open_season_scope === 'owned' ? 'steal anything someone holds' : 'claim anything'));
      if (r.steal_cooldown_s && r.cooldown_scope !== 'none') p.push(Math.round(r.steal_cooldown_s) + 's ' + r.cooldown_scope + ' cooldown');
      if (r.steal_back_lock_s) p.push(Math.round(r.steal_back_lock_s) + 's steal-back lock');
      if (r.steal_budget_per_min) p.push('at most ' + Math.round(r.steal_budget_per_min) + ' steals a minute');
    } else {
      p.push('no manual claiming');
    }
    if (r.hold_limit_s) {
      p.push('hold ' + Math.round(r.hold_limit_s) + 's, then ' + ({ next_finder: 'the next finder',
        release: 'back to the pool', return_finder: 'the first finder' }[r.hold_expiry] || 'the next finder'));
    }
    if (r.tenure_lock_s) p.push('unstealable after ' + Math.round(r.tenure_lock_s) + 's');
    if (r.idle_release_s) p.push('offline ' + Math.round(r.idle_release_s) + 's drops items');
    if (!r.require_found_to_claim && r.borrow_s) {
      p.push('borrows go back ' + (r.borrow_revert === 'prev_owner' ? 'to the holder' : 'to the pool')
        + ' after ' + Math.round(r.borrow_s) + 's');
    }
    if (r.auto_shuffle_s) {
      p.push('reshuffle ' + ({ all: 'everything', unowned: 'unheld items', idle: 'idle items' }[r.shuffle_scope] || 'everything')
        + ' every ' + Math.round(r.auto_shuffle_s) + 's');
    }
    if (r.shared_discovery) p.push('shared discovery');
    return p.join(' \u00b7 ');
  }
  function refreshRules() {
    var r = rulesFromForm();
    $('r-openseason-wrap').hidden = r.require_found_to_claim || !r.claiming;
    $('r-holdexp-wrap').hidden = !r.hold_limit_s;
    $('r-raid-wrap').hidden = r.require_found_to_claim;
    $('r-borrowrev-wrap').hidden = !r.borrow_s;
    $('r-shufscope-wrap').hidden = !r.auto_shuffle_s;
    $('rules-summary').textContent = summarizeRules(r) || '-';
    Array.prototype.forEach.call($('rulesPresets').querySelectorAll('[data-preset]'), function (b) {
      var preset = Object.assign({}, RULE_DEFAULTS, RULE_PRESETS[b.dataset.preset] || {});
      b.classList.toggle('on', sameRules(r, preset));
    });
  }
  function drawPresets() {
    var box = $('rulesPresets');
    Array.prototype.slice.call(box.querySelectorAll('[data-preset]')).forEach(function (b) { b.remove(); });
    Object.keys(RULE_PRESETS).forEach(function (k) {
      var label = PRESET_LABELS[k] || k.replace(/_/g, ' ').replace(/^./, function (c) { return c.toUpperCase(); });
      box.appendChild(el('button', { class: 'btn small quiet', type: 'button', 'data-preset': k, text: label,
        on: { click: function () { rulesToForm(Object.assign({}, RULE_DEFAULTS, RULE_PRESETS[k] || {})); } } }));
    });
  }
  var rulesApplied = false;
  function openRules() {
    if (!isAdmin()) return;
    rulesApplied = false;
    drawPresets();
    rulesToForm((st && st.rules) || RULE_DEFAULTS);
    if (!$('dlgRules').open) $('dlgRules').showModal();
  }
  /* Backing out of Custom puts the mode picker back on the room's mode.
     Called straight from every way the page closes the dialog, and from its
     close event for Escape: Chrome holds that event back in a hidden tab. */
  function rulesClosed() {
    if (!rulesApplied && st && $('hostMode').value === 'custom' && st.mode !== 'custom') {
      $('hostMode').value = st.mode || 'normal';
      hostRows();
    }
  }
  $('dlgRules').addEventListener('input', refreshRules);
  $('dlgRules').addEventListener('change', refreshRules);
  $('rules-cancel').addEventListener('click', function () { closeDialog($('dlgRules')); });
  $('rules-apply').addEventListener('click', function () {
    if (send({ type: 'admin_set_rules', rules: rulesFromForm() })) {
      rulesApplied = true;
      closeDialog($('dlgRules'));
    }
  });
  $('dlgRules').addEventListener('close', rulesClosed);

  /* ------------------------------------------------------------- the game */

  function startGame() {
    if (game || mode !== 'player' || !seat || !window.HLGame) return;
    agentOpen = false;
    game = window.HLGame.start({
      room: { code: code, player_id: seat.player_id, player_token: seat.player_token },
      wsUrl: wsUrl(),
      hud: hudOn(),
      onStatus: function () { drawGame(); },
      onLog: function (text) { if (window.console && console.debug) console.debug('[game] ' + text); },
      onNotify: onNotify,
      onAgentSocket: function (open) { agentOpen = open; drawGame(); },
      onReject: function (reason) { if (window.console) console.warn('[game] rejected: ' + reason); }
    });
  }

  function stopGame() {
    if (!game) return Promise.resolve();
    var g = game;
    game = null;
    agentOpen = false;
    drawGame();
    return g.stop();
  }

  function onNotify(text) {
    toast(text, 'link');
    addLog(text, 'link');
    lastLine = text;
    drawLine();
  }

  function drawGame() {
    var player = mode === 'player';
    $('links').hidden = !player;
    if (!player) return;
    var lamp = $('gameLamp'), say = $('gameSay'), box = $('gameDo');
    say.textContent = '';
    box.textContent = '';
    var help = el('button', { class: 'btn small quiet', type: 'button', text: 'How to link',
      on: { click: function () { $('dlgGame').showModal(); } } });
    function line(text, small) {
      say.appendChild(document.createTextNode(text));
      if (small) say.appendChild(el('small', { text: small }));
    }
    if (!game) {
      lamp.className = 'lamp';
      line('Game linking is off on this browser.',
        'Another browser or the desktop app can be your game link. Switch it on in Settings to link here.');
      box.appendChild(el('button', { class: 'btn small', type: 'button', text: 'Settings', on: { click: openSettings } }));
      box.appendChild(help);
      drawLine();
      return;
    }
    var s = game.snes;
    var bridge = /^(SNI|QUsb2Snes)$/.test(s.bridge) ? s.bridge : 'SNI or QUsb2Snes';
    if (s.state === 'attached') {
      lamp.className = 'lamp ok';
      var small;
      if (!agentOpen) small = 'Not connected to the room yet. Finds are sent once it is.';
      else if (!s.playable) small = 'Items are applied once a save file is loaded.';
      else if (!hudOn()) small = 'Save loaded. Items go straight into your game. In-game messages are off.';
      else small = 'Save loaded. Items and messages go straight into your game.';
      line(window.Snes.friendly(s.device) + ' through ' + (s.bridge || 'usb2snes'), small);
      box.appendChild(help);
    } else if (s.state === 'stuck') {
      lamp.className = 'lamp wait';
      line(bridge + ' is running, but it is not answering.',
        'Quit it from its tray icon and start it again. Do not start a second copy, or SNI next to QUsb2Snes: only one can run.');
      box.appendChild(help);
    } else if (s.state === 'no-device') {
      lamp.className = 'lamp wait';
      line(bridge + ' is running, but it has no game yet.',
        'Load your ROM in the emulator, or switch on the console. This links by itself.');
      box.appendChild(help);
    } else if (s.state === 'no-bridge') {
      lamp.className = 'lamp';
      line('No SNI or QUsb2Snes found on this PC.',
        'A browser cannot reach an emulator directly, NWA included; SNI is the small free program in between, and it needs no setup.');
      box.appendChild(el('a', { class: 'btn small', href: 'https://github.com/alttpo/sni/releases/latest',
        target: '_blank', rel: 'noopener noreferrer', text: 'Get SNI' }));
      box.appendChild(help);
    } else {
      lamp.className = 'lamp';
      line('Looking for SNI or QUsb2Snes on this PC.');
      box.appendChild(help);
    }
    drawLine();
  }

  /* the last line sent to this player's game, as the HUD draws it */
  function drawLine() {
    $('linkLine').hidden = !lastLine || mode !== 'player';
    if (!lastLine) return;
    var drawn = !!game && hudOn() && game.snes.state === 'attached';
    var text = window.Hud ? window.Hud.clean(lastLine, null, true) : lastLine.toUpperCase();
    $('lineLamp').className = 'lamp' + (drawn ? ' ok' : '');
    setStrip($('lineStrip'), text, !drawn);
    $('lineStrip').title = text;
    $('lineNote').textContent = drawn ? (text.length > 20 ? 'On your HUD. Longer than its 20 cells, so it scrolls across.' : 'On your HUD.')
      : (!game ? 'Not drawn: no game is linked on this browser.'
        : (!hudOn() ? 'Not drawn: in-game messages are off.' : 'Not drawn: no game is linked yet.'));
  }

  // ------------------------------------------------------------- settings

  function drawSwitches() {
    $('setLink').checked = linkOn();
    $('setLinkWord').textContent = linkOn() ? 'On' : 'Off';
    $('setHud').checked = hudOn();
    $('setHudWord').textContent = hudOn() ? 'On' : 'Off';
  }
  var leaveArm = 0;
  function drawLeave() {
    var hot = armed(leaveArm);
    $('btnLeave').textContent = hot ? 'Click again to leave' : 'Leave';
    $('btnLeave').classList.toggle('armed', hot);
  }
  function openSettings() {
    drawSwitches();
    leaveArm = 0; drawLeave();
    $('setHost').hidden = !isAdmin();
    $('dlgSettings').showModal();
  }

  $('btnSettings').addEventListener('click', openSettings);
  $('setLink').addEventListener('change', function () {
    setFlag('hyrulelink.link', this.checked);
    drawSwitches();
    if (this.checked) startGame(); else stopGame();
    drawGame();
  });
  $('setHud').addEventListener('change', function () {
    setFlag('hyrulelink.hud', this.checked);
    drawSwitches();
    if (game) game.setHud(this.checked);
    drawGame();
  });
  $('btnLeave').addEventListener('click', function () {
    if (!armed(leaveArm)) {
      leaveArm = Date.now() + 4000;
      drawLeave();
      setTimeout(drawLeave, 4100);
      return;
    }
    closing = true;
    this.disabled = true;
    seatSet(null);
    dropSocket();
    var done = function () { location.href = './'; };
    Promise.race([stopGame(), new Promise(function (r) { setTimeout(r, 2000); })]).then(done, done);
  });
  $('btnShowHost').addEventListener('click', function () {
    $('dlgSettings').close();
    $('hostPanel').open = true;
    $('hostPanel').scrollIntoView({ block: 'start' });
  });
  $('btnInvite').addEventListener('click', function () {
    copy(new URL('room.html?room=' + code, location.href).href, function () {
      $('inviteWord').textContent = 'Copied';
      setTimeout(function () { $('inviteWord').textContent = 'Copy invite'; }, 1600);
    });
  });
  function closeDialog(d) {
    if (d.open) d.close();
    if (d.id === 'dlgRules') rulesClosed();
  }
  document.querySelectorAll('dialog [data-close]').forEach(function (b) {
    b.addEventListener('click', function () { closeDialog(b.closest('dialog')); });
  });
  document.querySelectorAll('dialog').forEach(function (d) {
    d.addEventListener('click', function (ev) { if (ev.target === d) closeDialog(d); });
  });

  $('joinForm').addEventListener('submit', function (ev) { ev.preventDefault(); join(); });
  $('btnJustWatch').addEventListener('click', function () {
    if (info && info.pub_id) enterWatch(info.pub_id);
  });
  $('btnWatchJoin').addEventListener('click', function () {
    dropSocket();
    closing = true;          // no reconnect as a watcher while the cover is up
    lookup();
  });
  $('msgRetry').addEventListener('click', function () { location.reload(); });

  // ----------------------------------------------------------------- boot

  /* Leaving, or going into the back/forward cache: let go of the room and
     the bridge now. A page kept in that cache holds its sockets open, and
     the room would go on showing this player's game as linked. Coming back
     out of it starts clean. */
  window.addEventListener('pagehide', function () {
    closing = true;
    if (game) { var g = game; game = null; g.stop(true); }
    dropSocket();
  });
  window.addEventListener('pageshow', function (ev) { if (ev.persisted) location.reload(); });

  /* The countdowns tick here between server pushes. Behind a game window
     Chrome slows this timer to once a minute; nothing needs it there (the
     game link runs off snes.js's Worker), and the page is redrawn the moment
     it is shown again. */
  function tickClocks() {
    if (!st) return;
    drawGrid();
    drawModeBanner();
  }
  setInterval(tickClocks, 1000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tickClocks(); });

  async function boot() {
    importLegacy();
    if (!code && watchParam) { enterWatch(watchParam); return; }
    if (!code) { location.replace('./'); return; }
    $('roomCode').textContent = code;
    var s = seatGet();
    if (s) {
      try {
        var r = await post('api/rooms/' + code + '/resume', { player_id: s.player_id, player_token: s.player_token });
        if (r.ok) { enterPlayer(r.body); return; }
        if (r.status !== 404) {
          showMsg('That did not work', detail(r.body, 'The server said no (' + r.status + ').'), true);
          return;
        }
        seatSet(null);        // the room or the seat is gone: start over from the cover
      } catch (e) {
        showOffline();
        return;
      }
    }
    lookup();
  }

  boot();
})();
