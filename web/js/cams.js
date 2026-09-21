/* HyruleLink - QuakeCast cams on the room page (the Cams row).
 *
 * QuakeCast (https://www.billogna.lol/connect) puts two to four players'
 * cams, game windows and trackers into each other's OBS as browser sources.
 * The host opens a QuakeCast room from here for the players they pick
 * (POST api/rooms/CODE/cams) and can close it (.../cams/close). Each picked
 * player gets their private seat link from the server on their own ui
 * socket, {type: "cams", url}; it is never in the state document, which
 * only says who has a seat (state.cams = null | {seats: [player ids], error}).
 * There is no clock: HyruleLink has no race.
 *
 * The row is there only when the server has QuakeCast (state.cams present,
 * null or not), and only for players. Others than the host see it only while
 * cams are open, or after the cam room ended.
 *
 *   HLCams.mount({code, seat() -> {player_id, player_token}, post(path, body)
 *                 -> Promise<{ok, status, body}>, toast(text, kind), el?(id)})
 *     -> {draw(state, host, player), seat(url)}
 *   HLCams.view(state, host, player, url)   what the row says; no DOM, for node
 *
 * Names other people typed go in through textContent. The seat link opens
 * with rel="noopener noreferrer": it is a credential.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.HLCams = api;
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MIN = 2, MAX = 4;
  var ARM_MS = 4000;

  function nameOf(players, id) {
    for (var i = 0; i < players.length; i++) if (players[i].id === id) return players[i].name;
    return 'someone who left';
  }

  /* "Ana", "Ana and Bo", "Ana, Bo and Cy" */
  function list(names) {
    if (names.length < 2) return names.join('');
    return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
  }

  /* What the row shows, from the state document, whether this page is the
     host's, whether it is a player's (not a watcher's), and this player's
     own seat link from the last cams message. */
  function view(st, host, player, url) {
    if (!st || !('cams' in st) || !player) return { show: false };
    var c = st.cams, players = st.players || [];
    if (c && !c.error) {
      var seats = c.seats || [];
      var mine = url && st.you != null && seats.indexOf(st.you) >= 0 ? url : null;
      return {
        show: true, lamp: 'gold',
        text: 'Cams are open for ' + list(seats.map(function (id) { return nameOf(players, id); })) + ' through QuakeCast.',
        small: mine
          ? 'Your seat shares your cam, game window and tracker, and gives you OBS sources of the others. The link is yours alone.'
          : 'Each of them has a private seat link on their own page.',
        open: mine, close: !!host, pick: null
      };
    }
    var ended = !!(c && c.error);
    if (!host) {
      return ended ? { show: true, lamp: '', text: 'The cam room has ended.', small: 'The host can open a new one.',
        open: null, close: false, pick: null } : { show: false };
    }
    var v = {
      show: true, lamp: '',
      text: ended ? 'The cam room has ended.' : 'Put players\' cams, games and trackers in each other\'s OBS.',
      small: 'Uses QuakeCast. Pick two to four players; each gets a private seat link here that nobody else sees.',
      open: null, close: false, pick: players.map(function (p) { return { id: p.id, name: p.name }; })
    };
    if (players.length < MIN) { v.small = 'Needs two players in the room.'; v.pick = null; }
    return v;
  }

  function mount(opts) {
    var $ = opts.el || function (id) { return document.getElementById(id); };
    var url = null;          // this player's own seat link
    var heard = false;       // a cams message has come since the page opened
    var picks = {};          // player id -> ticked
    var busy = '';           // the request that is out: 'cams' (open) or 'cams/close'
    var armUntil = 0;        // Close cams needs a second click
    var last = { st: null, host: false, player: false };
    var shape = '';

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

    function said(r, fallback) { return (r && r.body && typeof r.body.detail === 'string' && r.body.detail) || fallback; }

    async function call(path, body) {
      var s = opts.seat() || {};
      body.player_id = s.player_id;
      body.player_token = s.player_token;
      busy = path; redraw();
      try {
        var r = await opts.post('api/rooms/' + opts.code + '/' + path, body);
        if (!r.ok) opts.toast(said(r, 'That did not work (' + r.status + ').'), 'bad');
        return r;
      } catch (e) {
        opts.toast('The server is not answering.', 'bad');
        return null;
      } finally {
        busy = ''; redraw();
      }
    }

    function openCams() {
      var v = view(last.st, last.host, last.player, url);
      var ids = (v.pick || []).filter(function (p) { return picks[p.id]; }).map(function (p) { return p.id; });
      if (ids.length < MIN || ids.length > MAX) { opts.toast('Pick two to four players.', 'bad'); return; }
      call('cams', { seats: ids });
    }

    function closeCams() {
      if (Date.now() >= armUntil) {
        armUntil = Date.now() + ARM_MS;
        setTimeout(redraw, ARM_MS + 100);
        redraw();
        return;
      }
      armUntil = 0;
      call('cams/close', {}).then(function (r) {
        if (r && r.ok && r.body && r.body.warning) opts.toast(r.body.warning, 'bad');
      });
    }

    function redraw() {
      var v = view(last.st, last.host, last.player, url);
      var row = $('cams');
      if (!row) return;
      row.hidden = !v.show;
      if (!v.show) { shape = ''; return; }
      var armed = Date.now() < armUntil;
      (v.pick || []).forEach(function (p, i) { if (picks[p.id] == null) picks[p.id] = i < MIN; });
      var key = JSON.stringify([v, picks, busy, armed]);
      if (key === shape) return;          // a state push that changes nothing here keeps the row's nodes
      shape = key;

      var act = $('camDo'), say = $('camSay');
      var was = document.activeElement;
      var focus = was && act.contains(was) ? was.getAttribute('data-cam') : null;
      $('camLamp').className = 'lamp' + (v.lamp ? ' ' + v.lamp : '');
      say.textContent = '';
      act.textContent = '';
      say.appendChild(document.createTextNode(v.text));
      if (v.small) say.appendChild(el('small', { text: v.small }));
      if (v.open) {
        act.appendChild(el('a', { class: 'btn small primary', href: v.open, target: '_blank',
          rel: 'noopener noreferrer', 'data-cam': 'seat', text: 'Open my cam seat' }));
      }
      if (v.close) {
        act.appendChild(el('button', { class: 'btn small quiet danger' + (armed ? ' armed' : ''), type: 'button',
          'data-cam': 'close', disabled: !!busy,
          text: busy === 'cams/close' ? 'Closing' : (armed ? 'Click again to close' : 'Close cams'),
          title: 'Ends the QuakeCast room for everyone in it', on: { click: closeCams } }));
      }
      if (v.pick) {
        v.pick.forEach(function (p) {
          var box = el('input', { type: 'checkbox', 'data-cam': 'pick:' + p.id, disabled: !!busy });
          box.checked = !!picks[p.id];
          box.addEventListener('change', function () { picks[p.id] = box.checked; redraw(); });
          act.appendChild(el('label', { class: 'check' }, [box, p.name]));
        });
        act.appendChild(el('button', { class: 'btn small', type: 'button', 'data-cam': 'open', disabled: !!busy,
          text: busy === 'cams' ? 'Opening' : 'Open cams', on: { click: openCams } }));
      }
      if (focus) {
        var again = act.querySelector('[data-cam="' + focus + '"]');
        if (again && !again.disabled) again.focus({ preventScroll: true });
      }
    }

    return {
      /* every state document: the row follows it */
      draw: function (st, host, player) {
        last = { st: st, host: !!host, player: !!player };
        redraw();
      },
      /* {type:"cams", url}: this player's own seat link, or null. One comes
         after every hello, so only a link that arrives later is announced. */
      seat: function (u) {
        var had = !!url, first = !heard;
        heard = true;
        url = typeof u === 'string' && /^https?:\/\//.test(u) ? u : null;
        if (url && !had && !first) opts.toast('Your cam seat is ready. It is in the Cams row.');
        redraw();
      }
    };
  }

  return { mount: mount, view: view, list: list, MIN: MIN, MAX: MAX };
}));
