/* HyruleLink - the front door: open a room, go to one, get back into one,
 * or watch one.
 *
 * Loaded in <head>, before anything is drawn, so an invite or a watch link
 * that landed here (index.html?room=CODE, index.html?watch=PUB; the desktop
 * app's Spectator view opens {base}/?watch=PUB) goes straight on to
 * room.html with the same query and the front page never flashes up.
 *
 * Seats live in this browser only (docs/BROWSER_PLAN.md 5.5), JSON-encoded:
 *   hyrulelink.name   "Ana"
 *   hyrulelink.rooms  {CODE: {player_id, player_token, name, title, t}}
 * The old page kept hl_name (plain text) and hl_rooms ({CODE: {player_id,
 * player_token}}). The first read here copies them into the new keys; after
 * that the old keys are never read again, so a seat forgotten here stays
 * forgotten. They are left in place for a rollback to the old page.
 *
 * The DOM-free parts are exported for node (tests/test_web_js.py).
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.HLHome = api;
  if (typeof document !== 'undefined' && typeof location !== 'undefined') api.boot(root);
})(this, function () {
  'use strict';

  var NAME_KEY = 'hyrulelink.name';
  var ROOMS_KEY = 'hyrulelink.rooms';
  var LEGACY_NAME = 'hl_name';
  var LEGACY_ROOMS = 'hl_rooms';
  var SEAT_CODE = /^[A-Z0-9]{4,16}$/;   // today's codes are 10; the first ones were 6
  var SHOWN_SEATS = 8;
  var SHOWN_LIVE = 20;
  var LIVE_EVERY_MS = 8000;

  /* ---------------------------------------------------------- pure parts */

  // index.html?room=CODE or ?watch=PUB -> 'room.html' + the same query; else null
  function redirectFor(search) {
    search = String(search || '');
    if (search && search.charAt(0) !== '?') search = '?' + search;
    var q = new URLSearchParams(search);
    if ((q.get('room') || '').trim() || (q.get('watch') || '').trim()) return 'room.html' + search;
    return null;
  }

  // what was typed or pasted into the code door: a bare code, or a whole
  // invite link (…room.html?room=CODE)
  function codeFrom(text) {
    text = String(text || '');
    var m = /[?&]room=([A-Za-z0-9]+)/.exec(text);
    return (m ? m[1] : text).toUpperCase().replace(/[^A-Z0-9]/g, '');
  }

  // localStorage behind try/catch: a private window may throw on any call
  function makeStore(ls) {
    return {
      raw: function (k) { try { return ls ? ls.getItem(k) : null; } catch (e) { return null; } },
      get: function (k, d) {
        var v = this.raw(k);
        if (v == null) return d;
        try { return JSON.parse(v); } catch (e) { return d; }
      },
      set: function (k, v) { try { if (ls) ls.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } }
    };
  }

  function readName(store) {
    var raw = store.raw(NAME_KEY);
    if (raw == null) return '';
    try {
      var v = JSON.parse(raw);
      return typeof v === 'string' ? v : '';
    } catch (e) {
      return raw;               // written as plain text by something else: still a name
    }
  }

  function readSeats(store) {
    var all = store.get(ROOMS_KEY, null);
    return all && typeof all === 'object' && !Array.isArray(all) ? all : {};
  }

  // the old page's seats and name, copied once into the new keys
  function importLegacy(store) {
    if (store.raw(NAME_KEY) == null) {
      var old = String(store.raw(LEGACY_NAME) || '').trim();
      // "Spectator" and "Player" were the old page's stand-ins, not names
      if (old && !/^(spectator|player)$/i.test(old)) store.set(NAME_KEY, old.slice(0, 40));
    }
    if (store.raw(ROOMS_KEY) == null && store.raw(LEGACY_ROOMS) != null) {
      var legacy = store.get(LEGACY_ROOMS, null), rooms = {}, who = readName(store);
      if (legacy && typeof legacy === 'object' && !Array.isArray(legacy)) {
        Object.keys(legacy).forEach(function (code) {
          var seat = legacy[code] || {}, c = String(code).toUpperCase(), id = Number(seat.player_id);
          if (!SEAT_CODE.test(c) || !(id > 0) || typeof seat.player_token !== 'string' || !seat.player_token) return;
          rooms[c] = { player_id: id, player_token: seat.player_token, name: who, title: '', t: 0 };
        });
      }
      store.set(ROOMS_KEY, rooms);
    }
  }

  function saveSeat(store, code, seat, now) {
    var all = readSeats(store);
    all[code] = {
      player_id: seat.player_id, player_token: seat.player_token,
      name: seat.name || '', title: seat.title || '', t: now == null ? Date.now() : now
    };
    store.set(ROOMS_KEY, all);
  }

  function forgetSeat(store, code) {
    var all = readSeats(store);
    if (!(code in all)) return;
    delete all[code];
    store.set(ROOMS_KEY, all);
  }

  function retitleSeat(store, code, title) {
    var all = readSeats(store);
    if (!all[code] || all[code].title === title) return;
    all[code].title = title;
    store.set(ROOMS_KEY, all);
  }

  // newest first; seats without a token are not seats
  function seatList(store) {
    var all = readSeats(store);
    return Object.keys(all)
      .filter(function (code) { return all[code] && all[code].player_token; })
      .sort(function (a, b) { return (all[b].t || 0) - (all[a].t || 0); })
      .map(function (code) { return { code: code, seat: all[code] }; });
  }

  function ago(seconds) {
    var s = Math.max(0, seconds);
    if (s < 90) return 'active now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function roster(list) {
    var names = (list || []).map(function (p) { return p && p.name; }).filter(Boolean);
    if (names.length <= 5) return names.join(', ');
    return names.slice(0, 4).join(', ') + ' and ' + (names.length - 4) + ' more';
  }

  /* ----------------------------------------------------------- the page */

  function boot(win) {
    var go = redirectFor(win.location.search);
    if (go) { win.location.replace(go); return; }
    var doc = win.document;
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', function () { page(win); });
    else page(win);
  }

  function page(win) {
    var doc = win.document;
    var $ = function (id) { return doc.getElementById(id); };
    var ls = null;
    try { ls = win.localStorage; } catch (e) { /* storage switched off */ }
    var store = makeStore(ls);
    importLegacy(store);

    function el(tag, cls, text) {
      var n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    }
    function fail(node, text) { node.textContent = text; node.hidden = false; }
    function said(e) {
      return e instanceof TypeError ? 'The server is not answering.' : (e && e.message) || 'That did not work.';
    }

    // a small red button that wants a second click inside five seconds
    function armedButton(label, title, act) {
      var b = el('button', 'btn small quiet danger', label), until = 0;
      b.type = 'button';
      b.title = title;
      b.addEventListener('click', function () {
        if (Date.now() > until) {
          until = Date.now() + 5000;
          b.textContent = 'Click again';
          b.classList.add('armed');
          win.setTimeout(function () {
            if (Date.now() > until) { b.textContent = label; b.classList.remove('armed'); }
          }, 5200);
          return;
        }
        act();
      });
      return b;
    }

    /* Open a room */
    var createBtn = $('createBtn');
    $('createName').value = readName(store);
    $('formCreate').addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var err = $('createErr');
      err.hidden = true;
      var name = $('createName').value.trim();
      if (!name) { $('createName').focus(); return; }
      createBtn.disabled = true;
      try {
        var r = await fetch('api/rooms', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: name, name: $('createTitle').value.trim() })
        });
        var body = await r.json().catch(function () { return {}; });
        if (r.status === 429) throw new Error('Too many new rooms from here. Wait a minute and try again.');
        if (!r.ok) throw new Error('The server answered ' + r.status + '.');
        if (!body.code || !body.player_token) throw new Error('That did not work.');
        store.set(NAME_KEY, name);
        saveSeat(store, body.code, { player_id: body.player_id, player_token: body.player_token,
          name: name, title: body.name });
        win.location.href = 'room.html?room=' + encodeURIComponent(body.code);
      } catch (e) {
        fail(err, said(e));
        createBtn.disabled = false;
      }
    });

    /* Got a code? */
    var codeField = $('joinCode');
    codeField.addEventListener('paste', function (ev) {
      var text = (ev.clipboardData && ev.clipboardData.getData('text')) || '';
      if (!/room=/.test(text)) return;          // a whole invite link: keep just its code
      ev.preventDefault();
      codeField.value = codeFrom(text).slice(0, 10);
    });
    $('formJoin').addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var err = $('joinErr');
      err.hidden = true;
      var code = codeFrom(codeField.value);
      if (!/^[A-Z0-9]{4,12}$/.test(code)) { fail(err, 'A room code is ten letters and numbers.'); return; }
      try {
        var r = await fetch('api/rooms/' + encodeURIComponent(code), { cache: 'no-store' });
        if (r.status === 404) { fail(err, 'No room has that code. It may have expired.'); return; }
        if (r.status === 429) { fail(err, 'Too many tries from here. Wait a minute and try again.'); return; }
        if (!r.ok) { fail(err, 'The server answered ' + r.status + '.'); return; }
        win.location.href = 'room.html?room=' + encodeURIComponent(code);
      } catch (e) {
        fail(err, 'The server is not answering.');
      }
    });

    /* Your rooms on this browser: the way back in for anyone who lost the
       tab. Each is looked up; a room the server no longer has drops off. */
    function tidyRecent() { $('recent').hidden = !$('recentList').children.length; }

    function recentRow(code, seat) {
      var row = el('div', 'recent-row');
      var a = el('a');
      a.href = 'room.html?room=' + encodeURIComponent(code);
      var title = el('span', 't', seat.title || 'A room');
      a.appendChild(title);
      a.appendChild(el('span', 'mono', seat.name ? code + ' · ' + seat.name : code));
      var state = el('span', 'recent-state');
      row.appendChild(a);
      row.appendChild(state);
      row.appendChild(armedButton('Forget', 'Forget your seat in this room on this browser. The room stays.',
        function () { forgetSeat(store, code); row.remove(); tidyRecent(); }));

      fetch('api/rooms/' + encodeURIComponent(code), { cache: 'no-store' }).then(function (r) {
        if (r.status === 404) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            // only the server's own "no such room" ends a seat, not a stray 404
            if (body && body.detail === 'no such room') { forgetSeat(store, code); row.remove(); tidyRecent(); }
            return null;
          });
        }
        return r.ok ? r.json() : null;
      }).then(function (room) {
        if (!room || !room.code) return;
        if (room.name) { title.textContent = room.name; retitleSeat(store, code, room.name); }
        var live = room.live || {}, n = live.uis || live.agents || 0;
        state.textContent = n ? n + ' in it now' : 'nobody in it';
        state.classList.toggle('on', n > 0);
      }).catch(function () { /* the server is not answering: the link still works later */ });
      return row;
    }

    function drawRecent() {
      var list = $('recentList');
      list.textContent = '';
      seatList(store).slice(0, SHOWN_SEATS).forEach(function (s) { list.appendChild(recentRow(s.code, s.seat)); });
      tidyRecent();
    }

    /* Live rooms: the public list (watch handles only, never a join code),
       refreshed while the page is on screen. Rows are kept and updated in
       place so a refresh never moves a button from under the pointer. */
    var liveRows = {};

    function liveRow(pub) {
      var row = el('div', 'recent-row');
      var meta = el('div', 'meta');
      meta.appendChild(el('span', 't'));
      meta.appendChild(el('small'));
      var watch = el('a', 'btn small', 'Watch');
      watch.href = 'room.html?watch=' + encodeURIComponent(pub);
      row.appendChild(meta);
      row.appendChild(el('span', 'recent-state'));
      row.appendChild(watch);
      return row;
    }

    function fillLive(row, r, now) {
      var since = now - (r.last_active || 0), n = r.players || 0, who = roster(r.player_list);
      row.querySelector('.t').textContent = r.name || 'Co-op';
      row.querySelector('small').textContent = (who ? who + ' · ' : '') + ago(since);
      var state = row.querySelector('.recent-state');
      state.textContent = n + (n === 1 ? ' player' : ' players');
      state.classList.toggle('on', since < 120);
    }

    function drawLive(rooms) {
      var list = $('liveList'), now = Date.now() / 1000, keep = {};
      rooms = rooms.filter(function (r) { return r && r.pub_id; }).slice(0, SHOWN_LIVE);
      rooms.forEach(function (r, i) {
        var row = liveRows[r.pub_id] || (liveRows[r.pub_id] = liveRow(r.pub_id));
        fillLive(row, r, now);
        keep[r.pub_id] = true;
        if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
      });
      Object.keys(liveRows).forEach(function (pub) {
        if (!keep[pub]) { liveRows[pub].remove(); delete liveRows[pub]; }
      });
      $('liveEmpty').textContent = 'No live rooms right now.';
      $('liveEmpty').hidden = rooms.length > 0;
    }

    var loading = false;
    async function loadLive() {
      if (loading) return;
      loading = true;
      try {
        var r = await fetch('api/rooms', { cache: 'no-store' });
        if (!r.ok) throw new Error('status ' + r.status);
        var body = await r.json();
        drawLive(Array.isArray(body.rooms) ? body.rooms : []);
      } catch (e) {
        if (!$('liveList').children.length) {
          $('liveEmpty').textContent = 'The server is not answering.';
          $('liveEmpty').hidden = false;
        }
      } finally {
        loading = false;
      }
    }

    drawRecent();
    loadLive();
    win.setInterval(function () { if (!doc.hidden) loadLive(); }, LIVE_EVERY_MS);
    doc.addEventListener('visibilitychange', function () { if (!doc.hidden) loadLive(); });
    // back from a room: the page may come out of the back/forward cache as it was left
    win.addEventListener('pageshow', function (ev) {
      if (!ev.persisted) return;
      createBtn.disabled = false;
      drawRecent();
      loadLive();
    });
  }

  return {
    NAME_KEY: NAME_KEY, ROOMS_KEY: ROOMS_KEY,
    redirectFor: redirectFor, codeFrom: codeFrom, makeStore: makeStore,
    readName: readName, readSeats: readSeats, importLegacy: importLegacy,
    saveSeat: saveSeat, forgetSeat: forgetSeat, retitleSeat: retitleSeat, seatList: seatList,
    ago: ago, roster: roster, boot: boot
  };
});
