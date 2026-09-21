/* BombosSwap - the operator page: every room, who is in it, and Delete.
 *
 * The key travels as the X-Operator-Key header on each call (never in a URL:
 * nginx logs URLs) and is kept in this browser's localStorage
 * (hyrulelink.operator, plain text) until Forget key. The server answers 404
 * when no key file is set up, 403 for a wrong key, and 429 after 20 wrong
 * ones from this address in an hour (server/operator.py). The listing
 * carries no player tokens; room codes are in it, because deleting a room is
 * the point.
 *
 * Delete asks twice, and a third time when somebody is in the room: it
 * closes every connection to it and erases its players and progress.
 */
(function () {
  'use strict';

  var KEY = 'hyrulelink.operator';
  var MODES = { normal: 'Normal', hot_potato: 'Hot Potato', chaos: 'Chaos', custom: 'Custom' };
  var $ = function (id) { return document.getElementById(id); };
  var key = '';
  try { key = localStorage.getItem(KEY) || ''; } catch (e) { /* private window */ }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  // server times are Unix seconds
  function span(seconds) {
    var s = Math.max(0, Math.round(seconds));
    if (s < 90) return s + ' s';
    var m = Math.round(s / 60);
    if (m < 90) return m + ' min';
    var h = Math.round(m / 60);
    return h < 48 ? h + ' h' : Math.round(h / 24) + ' d';
  }
  function names(list) {
    return list.length < 2 ? list.join('') : list.slice(0, -1).join(', ') + ' and ' + list[list.length - 1];
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  async function call(method, path) {
    var r = await fetch(path, { method: method, headers: { 'X-Operator-Key': key }, cache: 'no-store' });
    var body = await r.json().catch(function () { return {}; });
    return { status: r.status, body: body };
  }

  function askKey(text) {
    $('board').hidden = true;
    $('keyForm').hidden = false;
    $('keyErr').textContent = text || '';
    $('keyErr').hidden = !text;
    $('keyInput').focus();
  }

  async function load() {
    var got;
    try {
      got = await call('GET', 'api/operator/rooms');
    } catch (e) {
      if ($('board').hidden) return askKey('The server is not answering.');
      $('boardErr').textContent = 'The server is not answering.';
      $('boardErr').hidden = false;
      return;
    }
    if (got.status === 404) return askKey('The operator page is switched off: there is no key on the server.');
    if (got.status === 403) {
      key = '';
      try { localStorage.removeItem(KEY); } catch (e) { /* private window */ }
      return askKey('That key is wrong.');
    }
    if (got.status === 429) return askKey('Too many wrong keys from here. Try again in an hour.');
    if (got.status !== 200) {
      if ($('board').hidden) return askKey('The server answered ' + got.status + '.');
      $('boardErr').textContent = 'The server answered ' + got.status + '.';
      $('boardErr').hidden = false;
      return;
    }
    try { localStorage.setItem(KEY, key); } catch (e) { /* private window */ }
    $('keyForm').hidden = true;
    $('board').hidden = false;
    $('boardErr').hidden = true;
    draw(got.body);
  }

  function draw(doc) {
    var rooms = doc.rooms || [], used = rooms.filter(function (r) { return r.in_use; }).length;
    $('sum').textContent = plural(rooms.length, 'room', 'rooms') + ', ' + used + ' with somebody in it';
    var host = $('rooms');
    host.textContent = '';
    if (!rooms.length) { host.appendChild(el('p', 'op-empty', 'No rooms.')); return; }
    rooms.forEach(function (r) { host.appendChild(roomCard(r, doc.now, doc.ttl_days)); });
  }

  function roomCard(r, now, ttlDays) {
    var people = r.players || [];
    var linked = people.filter(function (p) { return p.agent; });
    var games = people.filter(function (p) { return p.agent && p.emu; }).length;

    var card = el('div', 'panel op-room' + (r.in_use ? ' live' : ''));
    var top = el('div', 'op-top');
    var title = el('div', 'op-title');
    title.appendChild(el('b', null, r.name || 'Co-op'));
    title.appendChild(el('span', 'mono', r.code));
    top.appendChild(title);
    top.appendChild(el('span', 'recent-state' + (r.in_use ? ' on' : ''), r.in_use ? 'in use' : 'nobody in it'));
    card.appendChild(top);

    var facts = [MODES[r.mode] || r.mode || 'Normal',
      'made ' + span(now - r.created_at) + ' ago',
      'last used ' + span(now - r.last_active) + ' ago'];
    if (r.uis) facts.push(plural(r.uis, 'page open', 'pages open'));
    if (games) facts.push(plural(games, 'game linked', 'games linked'));
    if (ttlDays) {
      var left = r.last_active + ttlDays * 86400 - now;
      facts.push(left > 0 ? 'removed in ' + span(left) + ' if unused' : 'due for removal');
    }
    card.appendChild(el('p', 'op-facts', facts.join(' · ')));

    var row = el('p', 'op-people');
    people.forEach(function (p) {
      var who = el('span', 'op-person' + (p.agent && p.emu ? ' on' : p.agent ? ' warn' : ''), p.name);
      who.title = p.agent && p.emu ? 'game linked' : p.agent ? 'connected, no game yet' : 'no game link';
      if (p.host) who.appendChild(el('small', null, ' host'));
      row.appendChild(who);
    });
    if (!people.length) row.appendChild(el('span', null, 'No players.'));
    card.appendChild(row);

    var err = el('p', 'recent-err');
    err.setAttribute('role', 'alert');
    err.hidden = true;

    var watch = el('a', 'btn small quiet', 'Watch');
    watch.href = 'room.html?watch=' + encodeURIComponent(r.pub_id);
    watch.target = '_blank';
    watch.rel = 'noopener';

    var b = el('button', 'btn small quiet danger', 'Delete');
    b.type = 'button';
    var armed = 0, told = false;
    function disarm() { b.textContent = 'Delete'; b.classList.remove('armed'); told = false; err.hidden = true; }
    b.addEventListener('click', async function () {
      if (Date.now() > armed) {
        armed = Date.now() + 5000;
        told = false;
        err.hidden = true;
        b.textContent = 'Click again';
        b.classList.add('armed');
        setTimeout(function () { if (Date.now() > armed) disarm(); }, 5200);
        return;
      }
      if (r.in_use && !told) {
        told = true;
        armed = Date.now() + 10000;
        setTimeout(function () { if (Date.now() > armed) disarm(); }, 10200);
        var inIt = [];
        if (linked.length) inIt.push(names(linked.map(function (p) { return p.name; })) + ' connected');
        if (r.uis) inIt.push(plural(r.uis, 'page open', 'pages open'));
        err.textContent = 'In use right now: ' + (inIt.join(', ') || 'somebody is connected') +
          '. Deleting it disconnects them. Click once more to delete it anyway.';
        err.hidden = false;
        b.textContent = 'Delete it anyway';
        return;
      }
      b.disabled = true;
      try {
        var got = await call('POST', 'api/operator/rooms/' + encodeURIComponent(r.code) + '/delete');
        if (got.status === 200 || got.status === 404 || got.status === 403 || got.status === 429) { load(); return; }
        err.textContent = 'The server answered ' + got.status + '.';
        err.hidden = false;
      } catch (e) {
        err.textContent = 'The server is not answering.';
        err.hidden = false;
      } finally {
        b.disabled = false;
      }
    });

    var foot = el('div', 'op-foot');
    foot.appendChild(watch);
    foot.appendChild(b);
    card.appendChild(foot);
    card.appendChild(err);
    return card;
  }

  $('keyForm').addEventListener('submit', function (ev) {
    ev.preventDefault();
    key = $('keyInput').value.trim();
    $('keyInput').value = '';
    if (key) load();
  });
  $('btnRefresh').addEventListener('click', load);
  $('btnForget').addEventListener('click', function () {
    key = '';
    try { localStorage.removeItem(KEY); } catch (e) { /* private window */ }
    askKey('');
  });

  if (key) { load(); } else { askKey(''); }
  // every 15 s, but never under a Delete that is waiting for another click
  setInterval(function () {
    if (key && !$('board').hidden && !document.hidden && !document.querySelector('#rooms .armed')) load();
  }, 15000);
})();
