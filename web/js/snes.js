/* BombosSwap - the link to the player's game.
 *
 * A browser cannot open a raw TCP or UDP socket, so it cannot talk to an
 * emulator directly. It CAN open a websocket to localhost, and both bridges
 * players already run for their auto-trackers speak the same usb2snes protocol
 * there:
 *
 *   SNI        ws://localhost:23074 (and :8080)   FXPak Pro, snes9x-nwa and
 *   QUsb2Snes  ws://localhost:23074 (and :8080)   other NWA emulators,
 *                                                 RetroArch, Lua (BizHawk,
 *                                                 snes9x-rr)
 *
 * So this file speaks usb2snes, and every one of those is covered by
 * whichever bridge the player has open. Only one can run: they want the same
 * ports (23074, and 65398 for Lua), and the second to start says so. Trackers
 * and bots with their own NWA connector are no clash; snes9x-nwa serves
 * several clients at once. The protocol's traps (RaceConnect
 * found them): Attach never answers, so Info is the confirmation; GetAddress
 * answers in BINARY frames that may be split; one request at a time per
 * socket; and a reply that arrives after its timeout would be read as the
 * next request's answer, so a timeout closes the socket and starts clean.
 *
 * Copied from EtherNet (web/js/snes.js). What changed: the bridge URLs and
 * the client name are options (opts.urls, opts.name); opts.onTick(module,
 * playable) runs every loop after the module bookkeeping and before the HUD
 * tick (the item agent polls there); OUT_OF_GAME and URLS are exported;
 * there is no switch for writes; and a socket gets OPEN_MS (6 s, not 2.5)
 * to open before its bridge counts as jammed (see OPEN_MS).
 *
 * What this page reads: the game's main module ($7E0010), the inventory
 * block $7EF342-$7EF38E, and the HUD strip cells a line is drawn over (to
 * put them back). What it writes: the item bytes in that block
 * (grants and revokes from the room), the run flag for the boots ($7EF379),
 * the bow's equip byte ($7EF340) and arrows ($7EF377), the HUD strip
 * ($7EC700 row 4) and the HUD update flag ($7E0016). Nothing else. Unlike
 * EtherNet, which writes only the HUD, this page owns part of the save:
 * item writes are never optional while the game is linked, or the game
 * drifts out of step with the room. In-game lines are switched off in
 * game.js (say becomes a no-op and the line on screen is aborted), never
 * here.
 *
 * This page spends the game BEHIND a game window, where setInterval is
 * clamped to about one tick a second. Every timer that matters runs off a
 * Worker, which is not clamped.
 */
(function (root) {
  'use strict';

  var URLS = ['ws://localhost:23074', 'ws://localhost:8080'];
  var WRAM = 0xF50000;
  var MODULE = 0x0010;
  /* a save is loaded and the HUD is the game's: dungeon, overworld, their
     transitions, and the text/menu module that draws over them */
  var PLAYABLE = { 0x06: 1, 0x07: 1, 0x08: 1, 0x09: 1, 0x0A: 1, 0x0B: 1, 0x0E: 1 };
  /* title, file select, copy/erase, name entry, loading, attract, save-and-quit:
     coming back from one of these the game has rebuilt its HUD from scratch.
     Everything else non-playable (door spotlights, the mirror, a boss victory)
     is a transition that leaves the HUD buffer alone, so a line that is up
     stays tracked and is put back properly once play resumes. */
  var OUT_OF_GAME = { 0x00: 1, 0x01: 1, 0x02: 1, 0x03: 1, 0x04: 1, 0x05: 1, 0x14: 1, 0x17: 1, 0x1B: 1 };
  var LOOP_MS = 500;
  /* How long a socket may take to open before the bridge behind it counts
     as there-but-jammed. Chrome holds each new WebSocket back 1 to 5 s once a
     page has had many more failed ones than good ones (its per-process
     throttle), and a page with no bridge running fails two a round: at 2.5 s
     it called a missing bridge a jammed one ('stuck') within a minute, and
     closed a bridge started after that before its socket could open.
     Measured in Chrome 2026-09-21: refusals and opens alike took 1.0-4.9 s. */
  var OPEN_MS = 6000;

  function makeTicker(ms, fn) {
    var stopped = false, worker = null, iv = null;
    try {
      var src = 'var t=null;onmessage=function(e){if(e.data&&e.data.ms){t=setInterval('
        + 'function(){postMessage(1);},e.data.ms);}else{clearInterval(t);}};';
      var url = URL.createObjectURL(new Blob([src], { type: 'text/javascript' }));
      worker = new Worker(url);
      URL.revokeObjectURL(url);
      worker.onmessage = function () { if (!stopped) fn(); };
      worker.postMessage({ ms: ms });
    } catch (e) { worker = null; }
    if (!worker) iv = setInterval(function () { if (!stopped) fn(); }, ms);
    return { stop: function () {
      stopped = true;
      if (worker) { try { worker.terminate(); } catch (e) { /* gone */ } }
      if (iv) clearInterval(iv);
    } };
  }

  /* 'emunwa://localhost:48879' or 'SD2SNES COM3' -> words a racer recognises. */
  function friendly(dev) {
    var d = String(dev || ''), low = d.toLowerCase();
    if (low.indexOf('fxpak') >= 0 || low.indexOf('sd2snes') >= 0 || /^com\d/i.test(d)) return 'FXPak Pro';
    if (low.indexOf('emunwa') >= 0 || low.indexOf('nwa') >= 0) return 'Emulator (NWA)';
    if (low.indexOf('ra://') === 0 || low.indexOf('retroarch') >= 0) return 'RetroArch';
    if (low.indexOf('lua') >= 0 || low.indexOf('snes9x') >= 0 || low.indexOf('bizhawk') >= 0) return 'Emulator (Lua)';
    return d.slice(0, 32) || 'Game';
  }

  function Snes(opts) {
    this.o = opts || {};
    this.ws = null;
    this.state = 'searching';   // searching | no-bridge | stuck | no-device | attached
    this.bridge = '';
    this.device = '';
    this.module = null;
    this.playable = false;
    this.hud = new root.Hud.HudStrip({
      read: this.read.bind(this), write: this.write.bind(this) });
    this._pending = null;
    this._chain = Promise.resolve();
    this._stopped = false;
    this._ticker = null;
    this._misses = 0;
    this._outOfGame = true;
    this._silent = 0;           // rounds in a row where a bridge was there and did not answer
    this._timedOut = false;
  }

  Snes.friendly = friendly;
  Snes.PLAYABLE = PLAYABLE;
  Snes.OUT_OF_GAME = OUT_OF_GAME;
  Snes.URLS = URLS;
  Snes.OPEN_MS = OPEN_MS;

  Snes.prototype.start = function () {
    var self = this;
    this._stopped = false;
    this._try(0);
    if (!this._ticker) this._ticker = makeTicker(LOOP_MS, function () { self._loop(); });
  };

  Snes.prototype.stop = function () {
    this._stopped = true;
    if (this._ticker) { this._ticker.stop(); this._ticker = null; }
    if (this.ws) { try { this.ws.close(); } catch (e) { /* gone */ } }
  };

  Snes.prototype._set = function (state) {
    var was = this.state + '|' + this.device + '|' + this.bridge;
    this.state = state;
    if (state !== 'attached') { this.device = ''; this.module = null; this.playable = false; }
    if (was !== state + '|' + this.device + '|' + this.bridge && this.o.onStatus) this.o.onStatus(this);
  };

  /* One round tries each URL once. A bridge that takes the connection but
     never answers is jammed, not missing, and the page must say so: "not
     found" sends people to start a second bridge, which then fails with "port
     in use". The retry comes from onclose and nowhere else, once per socket.
     A second retry path (the timer, a duplicate close event) doubles the loops
     every round the bridge is slow, and that flood is what jams it. */
  Snes.prototype._try = function (i, slow) {
    if (this._stopped) return;
    var self = this, urls = this.o.urls || URLS;
    if (i >= urls.length) {
      if (slow) {
        this._quiet(true);
      } else {
        this._silent = 0;
        this.bridge = '';
        this._set('no-bridge');
      }
      setTimeout(function () { self._try(0); }, 4000);
      return;
    }
    var sock;
    try { sock = new WebSocket(urls[i]); } catch (e) { this._try(i + 1, slow); return; }
    sock.binaryType = 'arraybuffer';
    var opened = false, closed = false, late = false;
    var to = setTimeout(function () {
      if (!opened) { late = true; try { sock.close(); } catch (e) { /* already */ } }
    }, OPEN_MS);
    sock.onopen = function () {
      opened = true;
      clearTimeout(to);
      self.ws = sock;
      self._attach().catch(function () { try { sock.close(); } catch (e) { /* gone */ } });
    };
    sock.onmessage = function (ev) { self._msg(ev); };
    sock.onerror = function () {};
    sock.onclose = function () {
      if (closed) return;       // node's WebSocket fires close twice for a socket closed while connecting
      closed = true;
      clearTimeout(to);
      if (self.ws === sock) {
        self.ws = null;
        self._fail(new Error('closed'));
        self.hud.reset();
        self._quiet(self._timedOut);
        self._timedOut = false;
        setTimeout(function () { self._try(0); }, 2000);
      } else if (!opened) {
        self._try(i + 1, slow || late);
      }
    };
  };

  /* A connection ended; timedOut = a bridge was there and did not answer.
     Two such rounds in a row is 'stuck', which the page shows as a bridge to
     restart rather than one to go and get. */
  Snes.prototype._quiet = function (timedOut) {
    this._silent = timedOut ? this._silent + 1 : 0;
    this._set(this._silent >= 2 ? 'stuck' : 'searching');
  };

  Snes.prototype._send = function (op, operands) {
    this.ws.send(JSON.stringify({ Opcode: op, Space: 'SNES', Operands: operands || [] }));
  };

  /* One request at a time: everything that expects an answer queues here. */
  Snes.prototype._ask = function (kind, need, send) {
    var self = this;
    var run = function () {
      return new Promise(function (res, rej) {
        if (!self.ws || self.ws.readyState !== 1) { rej(new Error('no socket')); return; }
        self._pending = {
          kind: kind, need: need, got: 0, buf: kind === 'bin' ? new Uint8Array(need) : null,
          res: res, rej: rej,
          timer: setTimeout(function () {
            self._pending = null;
            self._timedOut = true;
            rej(new Error('timeout'));
            try { self.ws.close(); } catch (e) { /* gone */ }   // see header: start clean
          }, 3000)
        };
        try { send(); } catch (e) { self._fail(e); }
      });
    };
    var next = this._chain.then(run, run);
    this._chain = next.catch(function () {});
    return next;
  };

  Snes.prototype._fail = function (err) {
    var p = this._pending;
    if (!p) return;
    clearTimeout(p.timer);
    this._pending = null;
    p.rej(err);
  };

  Snes.prototype._msg = function (ev) {
    var p = this._pending;
    if (!p) return;
    if (typeof ev.data === 'string') {
      if (p.kind !== 'json') return;
      clearTimeout(p.timer); this._pending = null;
      try { p.res(JSON.parse(ev.data)); } catch (e) { p.rej(e); }
      return;
    }
    if (p.kind !== 'bin') return;
    var b = new Uint8Array(ev.data);
    var n = Math.min(b.length, p.need - p.got);
    p.buf.set(b.subarray(0, n), p.got);
    p.got += n;
    if (p.got >= p.need) { clearTimeout(p.timer); this._pending = null; p.res(p.buf); }
  };

  Snes.prototype._json = function (op, operands) {
    var self = this;
    return this._ask('json', 0, function () { self._send(op, operands); });
  };

  Snes.prototype._attach = async function () {
    var self = this;
    try {
      var ver = await this._json('AppVersion');
      var v = String((ver.Results || [''])[0]);
      this.bridge = /sni/i.test(v) ? 'SNI' : (/qusb/i.test(v) ? 'QUsb2Snes' : 'usb2snes');
    } catch (e) { this.bridge = 'usb2snes'; throw e; }
    this._send('Name', [this.o.name || 'BombosSwap']);
    for (;;) {
      if (this._stopped || !this.ws) return;
      var list = (await this._json('DeviceList')).Results || [];
      this._silent = 0;                         // it answers: whatever happens next, it is not jammed
      if (list.length) {
        var want = this.o.prefer && list.indexOf(this.o.prefer) >= 0 ? this.o.prefer : list[0];
        this._send('Attach', [want]);           // never answers...
        await this._json('Info');               // ...so this is the confirmation
        this.device = String(want);
        this.hud.reset();
        this._misses = 0;
        this._set('attached');
        return;
      }
      this._set('no-device');
      await new Promise(function (r) { setTimeout(r, 2000); });
      if (self.ws === null) return;
    }
  };

  /* WRAM offset -> bytes, or null. Never throws: the HUD treats null as
     "try again next tick". */
  Snes.prototype.read = async function (addr, len) {
    if (this.state !== 'attached') return null;
    var self = this;
    try {
      return await this._ask('bin', len, function () {
        self._send('GetAddress', [(WRAM + addr).toString(16), len.toString(16)]);
      });
    } catch (e) { return null; }
  };

  Snes.prototype.write = async function (addr, bytes) {
    if (this.state !== 'attached') return false;
    var self = this;
    try {
      /* PutAddress has no reply; going through the chain keeps it from
         landing between another request and its answer. */
      var put = function () {
        self._send('PutAddress', [(WRAM + addr).toString(16), bytes.length.toString(16)]);
        self.ws.send(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
      };
      var sent = this._chain.then(put, put);
      this._chain = sent.catch(function () {});
      await sent;
      return true;
    } catch (e) { return false; }
  };

  Snes.prototype._loop = async function () {
    if (this.state !== 'attached' || this._looping) return;
    this._looping = true;
    try {
      var got = await this.read(MODULE, 1);
      if (!got) {
        if (++this._misses >= 4 && this.ws) { try { this.ws.close(); } catch (e) { /* gone */ } }
        return;
      }
      this._misses = 0;
      var mod = got[0];
      var playable = !!PLAYABLE[mod];
      if (OUT_OF_GAME[mod]) this._outOfGame = true;
      if (playable && this._outOfGame) {                  // a file just loaded: the HUD is new
        this._outOfGame = false;
        this.hud.reset();
      }
      var changed = mod !== this.module;
      this.module = mod;
      this.playable = playable;
      if (changed && this.o.onModule) this.o.onModule(mod, playable);
      if (this.o.onTick) {
        try { await this.o.onTick(mod, playable); } catch (e) { /* the agent logs its own */ }
      }
      if (playable) await this.hud.tick();
    } finally {
      this._looping = false;
    }
  };

  /* A line for this racer's screen. False = it cannot be shown right now. */
  Snes.prototype.say = function (text, key) {
    if (this.state !== 'attached') return false;
    return this.hud.show(text, key);
  };

  root.Snes = Snes;
  if (typeof module === 'object' && module.exports) module.exports = Snes;
})(typeof window !== 'undefined' ? window : globalThis);
