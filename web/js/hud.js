/* HyruleLink - text on the game's own HUD, no ROM patch.
 *
 * Copied verbatim from EtherNet (web/js/hud.js); only this comment differs.
 * It is a port of this repo's own agent/sni/hud_text.py (by way of the Twitch
 * bot) plus the ':' glyph, which worked this out against the zelda3 source
 * and proved it on a live seed:
 *
 *   ALTTP keeps its HUD as a BG3 tilemap in WRAM at $7EC700, 32 words a row.
 *   Every NMI it looks at $7E0016 and, when that is nonzero, copies the whole
 *   buffer to VRAM. The randomizer's HUD sheet carries an uppercase font
 *   (tiles 0x15D..) beside the classic digits (0x90..), so writing letter
 *   words into cells the game never touches and raising the flag draws text
 *   the same way the game draws its own - over any link that can write WRAM,
 *   real hardware included.
 *
 *   Row 4, columns 5-24 is the widest strip nothing else writes to. The item
 *   box owns columns 0-7 of the rows above, the counters the middle, hearts
 *   the right; columns 25-26 of rows 3-4 are re-stamped every frame.
 *
 * Rules that were learned the hard way and are kept here:
 *   - A failed read or write means "try again next tick", never lost state.
 *   - The game may rewrite SOME cells under the strip. Those are folded into
 *     the restore snapshot cell by cell, so expiry puts back what the game
 *     wants there now, not what was there when the line went up.
 *   - Draw only while a save is loaded; reset() after a file load, because
 *     the game rebuilt the HUD and the snapshot is void.
 *
 * Nothing here touches the DOM or a socket: the transport is handed in
 * ({read(addr, len) -> Promise<Uint8Array|null>, write(addr, bytes) ->
 * Promise<bool>}), so node runs the whole thing against a fake WRAM.
 */
(function (root) {
  'use strict';

  var BUFFER = 0xC700;          // $7EC700 hud_tile_indices_buffer (WRAM offset)
  var FLAG = 0x0016;            // $7E0016 flag_update_hud_in_nmi
  var COLS = 32;
  var SPACE = 0x247F;           // the blank the randomizer's own timer clears with
  var LETTER_A = 0x255D;
  var DIGIT_0 = 0x2490;
  /* The one piece of punctuation that is resident during play: the separator
     the randomizer's own HUD clock draws between hours, minutes and seconds
     (z3randomizer timer.asm: LDA.w #$2806). Same word, palette and all. */
  var COLON = 0x2806;

  var ROW = 4, COL = 5, WIDTH = 20;
  var HOLD_S = 4.0;             // a line that fits stays this long
  var SCROLL_HOLD_S = 1.5;      // show the head of a long line before it moves
  var SCROLL_STEP_S = 0.75;
  var MAX_QUEUE = 6;

  function wordOf(ch) {
    var c = ch.charCodeAt(0);
    if (c >= 65 && c <= 90) return LETTER_A + c - 65;
    if (c >= 48 && c <= 57) return DIGIT_0 + c - 48;
    if (c === 58) return COLON;
    return SPACE;
  }

  /* What the strip can draw: A-Z, 0-9, single spaces. The server cleans every
     line too (text.py hud_clean); this is the same fold for the live preview
     and for lines the page makes itself. */
  function clean(text, max, keepColon) {
    var s = String(text == null ? '' : text);
    if (s.normalize) s = s.normalize('NFKD');
    s = s.replace(/[^\x00-\x7F]/g, '').toUpperCase();
    if (keepColon) s = s.replace(/:/g, '\u0001');       // lines the server built keep theirs
    s = s.replace(/[-_.\/\\|:;,+*=<>()\[\]{}!?&@#$%^~`'"]+/g, ' ')
      .replace(/[^A-Z0-9 \u0001]+/g, '').replace(/\u0001/g, ':').replace(/\s+/g, ' ').trim();
    if (max && s.length > max) {
      var cut = s.slice(0, max), sp = cut.lastIndexOf(' ');
      if (sp > max / 2) cut = cut.slice(0, sp);
      s = cut.trim();
    }
    return s;
  }

  function words(text) {
    var s = clean(text, null, true), out = [];
    for (var i = 0; i < s.length; i++) out.push(wordOf(s[i]));
    return out;
  }

  /* `width` cells as little-endian bytes: centred when it fits, a window
     starting at `offset` when it does not. */
  function strip(ws, width, offset) {
    var cells = [];
    if (ws.length <= width) {
      var pad = Math.floor((width - ws.length) / 2);
      for (var i = 0; i < width; i++) {
        var k = i - pad;
        cells.push(k >= 0 && k < ws.length ? ws[k] : SPACE);
      }
    } else {
      cells = ws.slice(offset, offset + width);
    }
    var out = new Uint8Array(width * 2);
    for (var j = 0; j < width; j++) {
      out[j * 2] = cells[j] & 0xFF;
      out[j * 2 + 1] = cells[j] >> 8;
    }
    return out;
  }

  function same(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  function HudStrip(transport, opts) {
    opts = opts || {};
    this.t = transport;
    this.width = opts.width || WIDTH;
    this.addr = BUFFER + ((opts.row == null ? ROW : opts.row) * COLS
      + (opts.col == null ? COL : opts.col)) * 2;
    this.seconds = opts.seconds || HOLD_S;
    this.queue = [];
    this.msg = null;            // words of the line on screen
    this.key = null;
    this.replace = null;
    this.offset = 0;
    this.nextShift = 0;
    this.until = 0;
    this.lastWritten = null;
    this.saved = null;
    this.busy = false;
  }

  /* Queue a line. `key` marks a LIVE line (the countdown): a newer value
     replaces the older one in place instead of lining up behind it. */
  HudStrip.prototype.show = function (text, key) {
    text = clean(text, null, true);
    if (!text) return false;
    if (key != null) {
      for (var i = 0; i < this.queue.length; i++) {
        if (this.queue[i].key === key) { this.queue[i].text = text; return true; }
      }
      if (this.msg && this.key === key) { this.replace = text; return true; }
    }
    var last = this.queue[this.queue.length - 1];
    if (last && last.text === text) return true;
    if (this.queue.length >= MAX_QUEUE) this.queue.shift();
    this.queue.push({ text: text, key: key == null ? null : key });
    return true;
  };

  HudStrip.prototype.active = function () { return !!this.msg || this.queue.length > 0; };

  /* The game rebuilt its HUD (file load, reconnect): forget, write nothing. */
  HudStrip.prototype.reset = function () {
    this.queue = [];
    this.replace = null;
    this.msg = this.key = this.lastWritten = this.saved = null;
    this.until = 0;
  };

  /* Take the line off the screen NOW and drop the queue. Resolves true once
     the strip is clean; false means the write was dropped - call again. */
  HudStrip.prototype.abort = async function () {
    this.queue = [];
    this.replace = null;
    if (!this.msg) return true;
    await this._merge();
    return this._restore();
  };

  /* Advance the strip. Call only while the game is in a playable module. */
  HudStrip.prototype.tick = async function (nowS) {
    if (this.busy) return;
    this.busy = true;
    try {
      var now = nowS == null ? Date.now() / 1000 : nowS;
      if (this.msg) {
        if (this.replace != null) { this._retarget(this.replace, now); this.replace = null; }
        await this._merge();
        if (now >= this.until) {
          if (!(await this._restore())) return;
        } else {
          await this._advance(now);
          return;
        }
      }
      if (!this.msg && this.queue.length) {
        var entry = this.queue[0];
        if (await this._start(entry, now)) this.queue.shift();
      }
    } finally {
      this.busy = false;
    }
  };

  HudStrip.prototype._scrollTime = function (n) {
    return n <= this.width ? 0 : SCROLL_HOLD_S + (n - this.width) * SCROLL_STEP_S;
  };

  HudStrip.prototype._retarget = function (text, now) {
    this.msg = words(text);
    this.offset = 0;
    this.nextShift = now + SCROLL_HOLD_S;
    this.until = now + this.seconds + this._scrollTime(this.msg.length);
  };

  HudStrip.prototype._read = async function () {
    var data = await this.t.read(this.addr, this.width * 2);
    return data && data.length >= this.width * 2 ? data.slice(0, this.width * 2) : null;
  };

  HudStrip.prototype._put = async function (bytes) {
    if (!(await this.t.write(this.addr, bytes))) return false;
    await this.t.write(FLAG, new Uint8Array([1]));
    return true;
  };

  HudStrip.prototype._start = async function (entry, now) {
    var saved = await this._read();
    if (!saved) return false;
    var ws = words(entry.text);
    var bytes = strip(ws, this.width, 0);
    if (!(await this._put(bytes))) return false;
    this.saved = saved;
    this.msg = ws;
    this.key = entry.key;
    this.offset = 0;
    this.nextShift = now + SCROLL_HOLD_S;
    this.lastWritten = bytes;
    this.until = now + this.seconds + this._scrollTime(ws.length);
    return true;
  };

  HudStrip.prototype._merge = async function () {
    var cur = await this._read();
    if (!cur || !this.lastWritten || same(cur, this.lastWritten)) return;
    for (var i = 0; i < cur.length; i += 2) {
      if (cur[i] !== this.lastWritten[i] || cur[i + 1] !== this.lastWritten[i + 1]) {
        this.saved[i] = cur[i];             // the game wants this cell
        this.saved[i + 1] = cur[i + 1];
      }
    }
  };

  HudStrip.prototype._advance = async function (now) {
    if (this.msg.length > this.width && now >= this.nextShift
        && this.offset < this.msg.length - this.width) {
      this.offset += 1;
      this.nextShift = now + SCROLL_STEP_S;
    }
    var bytes = strip(this.msg, this.width, this.offset);
    if (await this._put(bytes)) this.lastWritten = bytes;
  };

  HudStrip.prototype._restore = async function () {
    if (this.saved && !(await this._put(this.saved))) return false;
    this.msg = this.key = this.lastWritten = this.saved = null;
    return true;
  };

  var Hud = {
    HudStrip: HudStrip, clean: clean, words: words, strip: strip, wordOf: wordOf,
    BUFFER: BUFFER, FLAG: FLAG, SPACE: SPACE, COLON: COLON, WIDTH: WIDTH, ROW: ROW, COL: COL,
    ADDR: BUFFER + (ROW * COLS + COL) * 2
  };
  root.Hud = Hud;
  if (typeof module === 'object' && module.exports) module.exports = Hud;
})(typeof window !== 'undefined' ? window : globalThis);
