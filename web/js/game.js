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
 * No DOM here: the page hands in callbacks.
 *
 *   var g = HLGame.start({ room: {code, player_id, player_token}, wsUrl,
 *                          urls?, hud, onStatus(snes), onLog(text),
 *                          onNotify(text), onAgentSocket(open), onReject(reason) });
 *   g.setHud(false); g.stop();  // stop resolves once the HUD line is down
 *   g.stop(true);               // the page is going away: close now
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
        if (ws !== sock) return;
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

  var HLGame = { start: start, RECONNECT_MS: RECONNECT_MS };
  root.HLGame = HLGame;
  if (typeof module === 'object' && module.exports) module.exports = HLGame;
})(typeof window !== 'undefined' ? window : globalThis);
