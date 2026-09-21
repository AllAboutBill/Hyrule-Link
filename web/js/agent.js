/* BombosSwap - the player agent, in the browser.
 *
 * A port of agent/agent.py (HyruleAgent) without its sockets and threads:
 * snes.js owns the game link and calls tick() with the module byte every
 * loop; game.js owns the server socket and hands messages to handle(). What
 * it does, in the Python order:
 *
 *   - Pickups: every item address is read in ONE contiguous read
 *     ($7EF342-$7EF38E). First sight seeds the baseline silently; a byte
 *     equal to `expected` is our own write echoing back (accepted, not
 *     reported); otherwise each item on that byte is reported when its level
 *     went up. Several items share $7EF38C.
 *   - Save gate: only playable modules are polled. Passing through an
 *     out-of-game module (title, file select, loading, save-and-quit) means
 *     the next playable poll may be a different or reloaded save, so the
 *     baseline and expected bytes are dropped and the server is asked to
 *     re-push ownership (resync). Death, door spotlights, the mirror and
 *     boss victories are not out-of-game.
 *   - Grants and revokes: the module is read first; outside a playable
 *     module the command waits in `pending` (newest per item wins) and the
 *     next playable poll applies it before it takes the snapshot. Otherwise
 *     up to 3 tries, the result recorded in `expected`, and an `applied` ack.
 *   - Boots: the run flag ($7EF379 bit 0x04) is set every poll while the
 *     boots are ours and cleared while they are not; written only when wrong.
 *
 * Differences from the Python agent, on purpose:
 *   - A pickup that cannot be sent (server socket down) waits in `outbox`
 *     and goes out right after the next hello, so a find made during an
 *     outage is not lost to the reconnect's ownership push.
 *   - A module read that fails defers a grant or revoke too (Python only
 *     defers on a readable non-playable module and writes blind otherwise).
 *   - Every piece of game work (apply, tick) runs through one promise chain,
 *     so a grant never interleaves with a poll.
 *   - The HUD strip belongs to snes.js: it is reset there on a save load and
 *     ticked there after tick(). `say` hands a line to it.
 *
 * No DOM, no socket: node runs it against a fake WRAM.
 */
(function (root) {
  'use strict';

  var I = root.HLItems || (typeof require === 'function' ? require('./items.js') : null);
  var E = root.HLEffects || (typeof require === 'function' ? require('./effects.js') : null);

  var MODE_ADDR = I.GAME_MODE_ADDR;
  var PLAYABLE = I.PLAYABLE_MODES;
  var OUT_OF_GAME = I.OUT_OF_GAME_MODES;
  var START = I.TRACKED_START;
  var SIZE = I.TRACKED_SIZE;
  var ADDRS = I.TRACKED_ADDRS;
  var ABILITY_ADDR = E.ABILITY_ADDR;
  var RUN_ABILITY_MASK = E.RUN_ABILITY_MASK;
  var APPLY_TRIES = 3;
  var NOTIFY_MAX = 120;
  var ERROR_MAX = 200;

  var has = Object.prototype.hasOwnProperty;
  function noop() {}
  function hex2(n) { return ('0' + n.toString(16).toUpperCase()).slice(-2); }
  function errText(e) { return String(e && e.message ? e.message : e); }

  /* opts: {game: {read, write}, send(obj) -> bool (false = socket down),
            say(text), onNotify(text), onLog(text)} */
  function Agent(opts) {
    opts = opts || {};
    this.game = opts.game;
    this.fx = new E.Effects(opts.game);
    this.o = {
      send: opts.send || function () { return false; },
      say: opts.say || noop,
      onNotify: opts.onNotify || noop,
      onLog: opts.onLog || noop
    };
    this.baseline = new Map();    // addr -> last raw byte accepted as known
    this.expected = new Map();    // addr -> raw byte we just wrote (suppressed once)
    this.pending = new Map();     // item key -> [level, enable], waiting for a loaded save
    this.outOfGame = false;       // passed through title/file select/loading since the last poll
    this.bootsOwned = false;      // keeps the run flag in step every poll
    this.emu = false;             // what status reports
    this.outbox = [];             // pickups the server has not heard yet
    this._chain = Promise.resolve();
  }

  Agent.prototype._log = function (text) {
    try { this.o.onLog(text); } catch (e) { /* the page's problem */ }
  };

  /* True when the message left; false when the server socket is down. */
  Agent.prototype._send = function (obj) {
    try { return !!this.o.send(obj); } catch (e) { return false; }
  };

  /* One game job at a time: apply and tick never interleave. */
  Agent.prototype._enqueue = function (fn) {
    var self = this;
    var run = function () { return fn.call(self); };
    var next = this._chain.then(run, run);
    this._chain = next.catch(noop);
    return next;
  };

  /* server -> agent */

  /* grant | revoke | notify | reject. Resolves once a grant/revoke is done
     (or deferred). */
  Agent.prototype.handle = function (msg) {
    if (!msg || typeof msg !== 'object') return Promise.resolve();
    if (msg.type === 'grant') {
      var level = Math.trunc(Number(msg.level == null ? 1 : msg.level));
      if (!isFinite(level)) return Promise.resolve();
      return this._apply(msg.item, level, true);
    }
    if (msg.type === 'revoke') return this._apply(msg.item, 0, false);
    if (msg.type === 'notify') this._notify(msg.text);
    else if (msg.type === 'reject') this._log('Server rejected: ' + msg.reason);
    return Promise.resolve();
  };

  Agent.prototype._notify = function (text) {
    text = String(text == null ? '' : text).split(/\s+/).filter(Boolean).join(' ').slice(0, NOTIFY_MAX);
    if (!text) return;
    try { this.o.onNotify(text); } catch (e) { /* the page's problem */ }
    try { this.o.say(text); } catch (e) { /* the HUD's problem */ }
  };

  Agent.prototype._apply = function (key, level, enable) {
    return this._enqueue(function () { return this._applyNow(key, level, enable); });
  };

  /* Runs inside the chain (from _apply, or from a poll flushing `pending`). */
  Agent.prototype._applyNow = async function (key, level, enable, tries) {
    if (typeof key !== 'string' || !has.call(I.BY_KEY, key)) return;
    tries = tries || APPLY_TRIES;
    /* Only write while a save is loaded: outside a playable module the
       $7EF000 mirror is not the live save and the next file load wipes the
       write. An unreadable module byte waits too. */
    var gm = null;
    try { gm = await this.game.read(MODE_ADDR, 1); } catch (e) { gm = null; }
    var mod = gm && gm.length ? gm[0] : null;
    if (mod === null || !PLAYABLE[mod]) {
      this.pending.set(key, [level, enable]);
      this._log('Deferred ' + (enable ? 'grant' : 'revoke') + ' of ' + key + ' - '
        + (mod === null ? 'game mode unreadable' : 'no save loaded (mode 0x' + hex2(mod) + ')'));
      return;
    }
    this.pending.delete(key);     // this command supersedes any deferred one
    var last = null;
    for (var i = 0; i < tries; i++) {
      try {
        var raw = enable ? await this.fx.enable(key, level) : await this.fx.disable(key);
        this._log(enable ? 'Granted ' + key + ' (lvl ' + level + ')' : 'Revoked ' + key);
        if (key === 'boots') this.bootsOwned = enable;
        this.expected.set(I.BY_KEY[key].addr, raw);   // our write, not a pickup
        this._sendApplied(key, enable, true);
        return;
      } catch (e) {
        last = e;
      }
    }
    this._log('apply ' + key + ' failed after ' + tries + ' tries: ' + errText(last));
    this._sendApplied(key, enable, false, errText(last));
  };

  Agent.prototype._sendApplied = function (key, enable, ok, error) {
    var payload = { type: 'applied', item: key, action: enable ? 'grant' : 'revoke', ok: !!ok };
    if (error) payload.error = String(error).slice(0, ERROR_MAX);
    this._send(payload);
  };

  /* the poll */

  /* One poll, with the module byte snes.js just read. Never rejects. */
  Agent.prototype.tick = function (module) {
    return this._enqueue(function () { return this._poll(module); });
  };

  Agent.prototype._poll = async function (mod) {
    try {
      await this._pollOnce(mod);
    } catch (e) {
      this._log('poll error: ' + errText(e));
    }
  };

  Agent.prototype._pollOnce = async function (mod) {
    if (mod == null || !PLAYABLE[mod]) {
      if (mod != null && OUT_OF_GAME[mod]) this.outOfGame = true;
      return;
    }
    if (this.outOfGame) {
      this.outOfGame = false;
      /* A (re)loaded save reverts WRAM to what was last saved. Re-seed so the
         reverted bytes are not reported, and have the server re-push. */
      this.resync('Save (re)loaded', true);
    }
    await this._flushPending();

    var snap = await this.game.read(START, SIZE);
    if (!snap || snap.length < SIZE) return;

    for (var a = 0; a < ADDRS.length; a++) {
      var addr = ADDRS[a];
      var raw = snap[addr - START];
      var old = this.baseline.has(addr) ? this.baseline.get(addr) : null;
      var exp = this.expected.has(addr) ? this.expected.get(addr) : null;
      if (old === null) {
        this.baseline.set(addr, raw);             // first sight: seed
        continue;
      }
      if (exp !== null && raw === exp) {
        this.baseline.set(addr, raw);             // our own write echoing back
        this.expected.delete(addr);
        continue;
      }
      if (raw === old) continue;
      var pickups = [];
      var items = I.ITEMS_BY_ADDR[addr];
      for (var k = 0; k < items.length; k++) {
        var nl = I.discoveredLevel(items[k], raw);
        var ol = I.discoveredLevel(items[k], old);
        if (nl > ol) pickups.push([items[k].key, nl]);   // 0 -> 1, or a tier up
      }
      this.baseline.set(addr, raw);
      for (var p = 0; p < pickups.length; p++) this._sendPickup(pickups[p][0], pickups[p][1]);
    }

    var ability = START <= ABILITY_ADDR && ABILITY_ADDR < START + SIZE ? snap[ABILITY_ADDR - START] : null;
    await this._enforceBootsAbility(ability);
  };

  Agent.prototype._sendPickup = function (key, level) {
    var msg = { type: 'pickup', item: key, level: level };
    if (this._send(msg)) {
      this._log('Reported pickup: ' + key + ' lvl ' + level);
    } else {
      this.outbox.push(msg);
      this._log('Pickup of ' + key + ' held until the server is back');
    }
  };

  /* The run flag follows ownership: ALTTP can clear it on a transition
     (boots but no dash) and a lost revoke can leave it set. */
  Agent.prototype._enforceBootsAbility = async function (ability) {
    if (ability == null) {
      var data = await this.game.read(ABILITY_ADDR, 1);
      if (!data || !data.length) return;
      ability = data[0];
    }
    var have = !!(ability & RUN_ABILITY_MASK);
    if (this.bootsOwned && !have) {
      await this.game.write(ABILITY_ADDR, new Uint8Array([(ability | RUN_ABILITY_MASK) & 0xFF]));
    } else if (!this.bootsOwned && have) {
      await this.game.write(ABILITY_ADDR, new Uint8Array([ability & ~RUN_ABILITY_MASK & 0xFF]));
    }
  };

  Agent.prototype._flushPending = async function () {
    var list = Array.from(this.pending.entries());
    this.pending.clear();
    for (var i = 0; i < list.length; i++) {
      await this._applyNow(list[i][0], list[i][1][0], list[i][1][1]);
    }
  };

  /* link events */

  /* Ask the server to re-push ownership. wipeBaseline (default true) re-seeds
     detection from the game without reporting it: right for a save reload,
     wrong for a link blip on the same save, where the old baseline is still
     valid and a pickup made during the blip must still be reported. */
  Agent.prototype.resync = function (reason, wipeBaseline) {
    var wipe = wipeBaseline !== false;
    this._log((reason || 'Resync') + ' - '
      + (wipe ? 're-seeding state and requesting resync' : 'requesting resync') + '.');
    if (wipe) {
      this.baseline.clear();
      this.expected.clear();
    }
    this._send({ type: 'resync' });
  };

  /* The agent socket just said hello. */
  Agent.prototype.onServerOpen = function () {
    this._send({ type: 'status', emu: !!this.emu });
    var box = this.outbox;
    this.outbox = [];
    for (var i = 0; i < box.length; i++) {
      if (!this._send(box[i])) {
        this.outbox = box.slice(i).concat(this.outbox);
        return;
      }
      this._log('Reported pickup: ' + box[i].item + ' lvl ' + box[i].level + ' (held)');
    }
  };

  /* first = the first attach of this page: the hello's ownership push is
     still to come, so there is nothing to resync. */
  Agent.prototype.onGameAttached = function (first) {
    this.emu = true;
    if (!first) this.resync('Game link back', false);
    this._send({ type: 'status', emu: true });
  };

  Agent.prototype.onGameLost = function () {
    this.emu = false;
    this._send({ type: 'status', emu: false });
  };

  Agent.prototype.setEmu = function (on) {
    this.emu = !!on;
  };

  var HLAgent = { Agent: Agent, APPLY_TRIES: APPLY_TRIES };
  root.HLAgent = HLAgent;
  if (typeof module === 'object' && module.exports) module.exports = HLAgent;
})(typeof window !== 'undefined' ? window : globalThis);
