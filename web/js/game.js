/* HyruleLink - this browser as a player's game link.
 *
 * Glue between three things that do not know each other:
 *
 *   snes.js    the usb2snes link to SNI or QUsb2Snes on this PC (the game)
 *   agent.js   pickups, grants, revokes, the save gate (the Python agent's port)
 *   /ws        the room server, as role "agent", exactly like the desktop app
 *
 * The agent socket opens on the FIRST time a game attaches and then stays for
 * the life of the page (reconnecting after 3 s). Its hello makes the server
 * push one grant or revoke for every catalog item, so a page that never finds
 * a game never registers as anybody's game. A reject that says the seat or
 * the room is gone ends the reconnecting; the page's own socket gets the same
 * reject and decides what to show.
 *
 * In-game lines are the one optional write: with `hud` off, say() does
 * nothing and whatever line is on screen is taken down on the next poll.
 * Item writes are never optional while linked.
 *
 * The room page links through link(), not start(): one tab per room per
 * browser links the game at a time (a Web Lock, see link() below).
 *
 * No DOM here: the page hands in callbacks.
 *
 *   var g = HLGame.start({ room: {code, player_id, player_token}, wsUrl,
 *                          urls?, hud, onStatus(snes), onLog(text),
 *                          onNotify(text), onAgentSocket(open), onReject(reason) });
 *   g.setHud(false); g.stop();  // stop resolves once the HUD line is down
 *   g.stop(true);               // the page is going away: close now
 *   HLGame.lineFate(g.snes, hud) // '' drawn, or 'off' | 'hud' | 'link' | 'save'
 *
 *   var h = HLGame.link({ ...start's opts, onLock(state), locks? });
 *   h.state    // 'asking' | 'linked' | 'waiting' | 'stopped'
 *   h.snes     // the linked Snes, null while another tab has the link
 *   h.takeOver(); h.setHud(on); h.stop(now); h.agentOpen();
 */
(function (root) {
  'use strict';

  var RECONNECT_MS = 3000;
  var STOP_HUD_MS = 1500;
  var FATAL = /room not found|room closed|bad room\/player token/i;

  function noop() {}

  function start(opts) {
    opts = opts || {};
    var Snes = root.Snes, HLAgent = root.HLAgent;
    var seat = opts.room || {};
    var cb = {
      onStatus: opts.onStatus || noop,
      onLog: opts.onLog || noop,
      onNotify: opts.onNotify || noop,
      onAgentSocket: opts.onAgentSocket || noop,
      onReject: opts.onReject || noop
    };
    function call(name, arg) { try { cb[name](arg); } catch (e) { /* the page's problem */ } }

    var hudOn = opts.hud !== false;
    var abortWanted = !hudOn;
    var stopped = false, fatal = false;
    var ws = null, helloSent = false, retry = null;
    var attachedOnce = false, wasAttached = false;
    var snes = null, agent = null;

    /* ---------------------------------------------------------- the room */

    function send(obj) {
      if (!ws || ws.readyState !== 1 || !helloSent) return false;
      try { ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
    }

    function openSocket() {
      retry = null;
      if (stopped || fatal) return;
      var sock;
      try { sock = new WebSocket(opts.wsUrl); } catch (e) { schedule(); return; }
      ws = sock;
      helloSent = false;
      sock.onopen = function () {
        if (ws !== sock) return;
        try {
          sock.send(JSON.stringify({ type: 'hello', role: 'agent', room: seat.code,
            player_id: seat.player_id, token: seat.player_token }));
        } catch (e) { return; }
        helloSent = true;
        call('onAgentSocket', true);
        agent.onServerOpen();        // status, then any finds made while it was down
      };
      sock.onmessage = function (ev) {
        /* stopping: nothing more reaches the game from here. The link that
           comes next (another tab, say) gets everything again in its hello. */
        if (ws !== sock || stopped) return;
        var m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (!m || typeof m !== 'object') return;
        if (m.type === 'reject' && FATAL.test(String(m.reason || ''))) {
          fatal = true;
          call('onReject', String(m.reason || ''));
        }
        agent.handle(m);
      };
      sock.onerror = function () {};
      sock.onclose = function () {
        if (ws !== sock) return;
        ws = null;
        helloSent = false;
        call('onAgentSocket', false);
        schedule();
      };
    }

    function schedule() {
      if (stopped || fatal || retry) return;
      retry = setTimeout(openSocket, RECONNECT_MS);
    }

    /* ---------------------------------------------------------- the game */

    function linkChanged() {
      var att = snes.state === 'attached';
      if (att && !wasAttached) {
        wasAttached = true;
        var first = !attachedOnce;
        attachedOnce = true;
        if (first) openSocket();
        agent.onGameAttached(first);
      } else if (!att && wasAttached) {
        wasAttached = false;
        agent.onGameLost();
      }
    }

    async function onTick(mod) {
      if (stopped) return;
      if (abortWanted) {
        try { if (await snes.hud.abort()) abortWanted = false; } catch (e) { /* next poll */ }
      }
      await agent.tick(mod);
    }

    snes = new Snes({
      urls: opts.urls,
      name: 'HyruleLink',
      onStatus: function () {
        if (stopped) return;
        linkChanged();
        call('onStatus', snes);
      },
      onModule: function () { if (!stopped) call('onStatus', snes); },
      onTick: onTick
    });

    agent = new HLAgent.Agent({
      game: snes,
      send: send,
      say: function (text) { return hudOn ? snes.say(text) : false; },
      onNotify: function (text) { call('onNotify', text); },
      onLog: function (text) { call('onLog', text); }
    });

    snes.start();

    /* ------------------------------------------------------------ handle */

    function setHud(on) {
      hudOn = !!on;
      abortWanted = !hudOn;
    }

    /* Resolves once the line on screen (if any) is down and the sockets are
       closed. Nothing is revoked: the game keeps what it holds. `now` skips
       the HUD clean-up and closes at once (the page is going away). */
    function stop(now) {
      if (stopped) return Promise.resolve();
      stopped = true;
      if (retry) { clearTimeout(retry); retry = null; }
      var clean = Promise.resolve();
      if (!now && snes.state === 'attached' && snes.hud.active()) {
        clean = Promise.race([
          snes.hud.abort().catch(noop),
          new Promise(function (r) { setTimeout(r, STOP_HUD_MS); })
        ]);
      }
      function close() {
        var sock = ws;
        ws = null;
        if (sock) {
          try { if (sock.readyState === 1) sock.send(JSON.stringify({ type: 'bye' })); } catch (e) { /* gone */ }
          try { sock.close(); } catch (e) { /* gone */ }
          call('onAgentSocket', false);
        }
        snes.stop();
      }
      if (now) { close(); return Promise.resolve(); }
      return clean.then(close);
    }

    return {
      stop: stop,
      setHud: setHud,
      snes: snes,
      agent: agent,
      agentOpen: function () { return !!ws && ws.readyState === 1 && helloSent; }
    };
  }

  /* ------------------------------------ one link per room per browser */

  /* Two tabs of one room in one browser are one player (the seat is in
     localStorage), so both would link the game. The server keeps one agent
     per player and the newest hello wins: when the newer tab closed, the
     older one went on saying "Save loaded" while no item reached it. Two
     linked tabs are also two pollers on one game, each blind to the other's
     writes, so a grant one tab wrote could look like a find to the other.

     So the whole linked lifetime (the bridge, the agent socket, the HUD) runs
     inside a Web Lock named for the room, 'hyrulelink.link.' + CODE, held
     until this page stops linking: link switched off, Leave, the page hidden
     or closed. A second tab waits in the lock's queue without opening the
     bridge or the room, and links when the first one lets go.

     takeOver() takes the lock with {steal: true}. The tab it was taken from
     sees its request reject (AbortError), stops linking cleanly (a bye on
     the agent socket, the bridge closed, any HUD line taken down) and goes
     back to the end of the queue. It never steals it back by itself, so two
     tabs cannot take it from each other in a loop: it links again only when
     the other tab lets go, or on a click.

     No navigator.locks (an http page on the LAN, an old browser): it links
     at once, as before. Other browsers and devices are not covered by the
     lock; there the newest hello wins, and Settings can switch a page off.

     `locks` stands in for navigator.locks (the units pass a fake; null means
     none). onLock(state) fires on every change after link() returns:
       asking   the first "is it free?" is not answered yet (a moment)
       linked   this page links the game; h.snes is its Snes
       waiting  another tab of this browser has it, and this one is queued
       stopped  stop() was called */
  var LOCK_PREFIX = 'hyrulelink.link.';

  function link(opts) {
    opts = opts || {};
    var nav = root.navigator;
    var locks = 'locks' in opts ? opts.locks : (nav && nav.locks) || null;
    if (locks && typeof locks.request !== 'function') locks = null;
    var name = LOCK_PREFIX + String((opts.room || {}).code || '');
    var onLock = opts.onLock || noop;
    var hudOn = opts.hud !== false;
    var session = null;       // start()'s handle while this page links
    var release = null;       // resolves the held lock's callback: lets it go
    var queued = null;        // the AbortController of the request in the queue
    var gen = 0;              // bumped by every request; an older one's answer is ignored
    var held = 0;             // the gen of the request that holds the lock
    var stealing = false, stopped = false, quiet = true;

    var h = {
      state: 'asking',
      snes: null,
      takeOver: takeOver,
      setHud: setHud,
      stop: stop,
      agentOpen: function () { return !!session && session.agentOpen(); }
    };

    function set(state) {
      if (h.state === state) return;
      h.state = state;
      if (!quiet) { try { onLock(state); } catch (e) { /* the page's problem */ } }
    }

    function begin() {
      var o = {};
      for (var k in opts) if (Object.prototype.hasOwnProperty.call(opts, k)) o[k] = opts[k];
      o.hud = hudOn;
      session = start(o);
      h.snes = session.snes;
      set('linked');
    }

    /* Stop linking; resolves once the HUD line is down and the sockets closed. */
    function end(now) {
      var s = session;
      session = null;
      h.snes = null;
      return s ? s.stop(now) : Promise.resolve();
    }

    /* The lock is ours: link, and hold it until release() is called. */
    function hold(my) {
      if (my !== gen || stopped) return undefined;       // superseded while queued: let go
      held = my;
      stealing = false;
      queued = null;
      begin();
      return new Promise(function (r) { release = r; });
    }

    function request(opt, cb) {
      var my = ++gen, p;
      try { p = locks.request(name, opt, function (lock) { return cb(my, lock); }); }
      catch (e) { p = Promise.reject(e); }
      Promise.resolve(p).then(noop, function (err) { lost(my, err); });
    }

    /* First look: take it if it is free, else get in line. */
    function ask() {
      request({ ifAvailable: true }, function (my, lock) {
        if (my !== gen || stopped) return undefined;
        if (!lock) { queue(); return undefined; }
        return hold(my);
      });
    }

    function queue() {
      var ctl = typeof AbortController === 'function' ? new AbortController() : null;
      queued = ctl;
      request(ctl ? { signal: ctl.signal } : {}, hold);
      set('waiting');
    }

    function takeOver() {
      if (stopped || !locks || h.state !== 'waiting' || stealing) return;
      stealing = true;
      var old = queued;
      queued = null;
      request({ steal: true }, hold);                  // the queued request is stale from here
      if (old) { try { old.abort(); } catch (e) { /* gone */ } }
    }

    /* A request's promise rejected. */
    function lost(my, err) {
      if (my !== gen || stopped) return;               // an older request, or our own stop
      stealing = false;
      if (held === my) {
        /* granted, then taken away: another tab stole it (AbortError) */
        held = 0;
        var r = release, stolen = !!err && err.name === 'AbortError';
        release = null;
        set(stolen ? 'waiting' : 'stopped');
        end(false).then(function () {
          if (r) r();
          /* back in line behind the tab that took it; never ahead of it */
          if (stolen && my === gen && !stopped) queue();
        });
        return;
      }
      /* refused outright (an opaque origin, say): link as with no locks */
      locks = null;
      begin();
    }

    function setHud(on) {
      hudOn = !!on;
      if (session) session.setHud(hudOn);
    }

    /* Resolves once linking has stopped; then the next tab in line links. */
    function stop(now) {
      if (stopped) return Promise.resolve();
      stopped = true;
      gen++;
      stealing = false;
      var q = queued, r = release;
      queued = null; release = null; held = 0;
      if (q) { try { q.abort(); } catch (e) { /* gone */ } }
      set('stopped');
      return end(now).then(function () { if (r) r(); });
    }

    if (locks) ask(); else begin();
    quiet = false;
    return h;
  }

  /* Does a line said to the game right now reach its screen? '' when it
     does (or waits behind a door or a death), else why not: 'off' no game
     link on this browser, 'hud' in-game messages switched off, 'link' no
     game attached, 'save' no save loaded. snes.js draws only while a save
     is loaded and drops every queued line when a file loads, so a line said
     on the title or file select, or before the first poll, is never drawn.
     The room page's "Last line" row says which. */
  function lineFate(snes, hudOn) {
    if (!snes) return 'off';
    if (!hudOn) return 'hud';
    if (snes.state !== 'attached') return 'link';
    var out = (root.Snes && root.Snes.OUT_OF_GAME) || {};
    if (snes._outOfGame || snes.module == null || out[snes.module]) return 'save';
    return '';
  }

  var HLGame = { start: start, link: link, lineFate: lineFate, RECONNECT_MS: RECONNECT_MS,
    LOCK_PREFIX: LOCK_PREFIX };
  root.HLGame = HLGame;
  if (typeof module === 'object' && module.exports) module.exports = HLGame;
})(typeof window !== 'undefined' ? window : globalThis);
