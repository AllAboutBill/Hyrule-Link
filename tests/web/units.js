/* node tests/web/units.js - the DOM-free parts of the browser client.
 *
 * The hud and snes suites are EtherNet's (tests/web/units.js there), the snes
 * fake bridge extended with a WRAM and opts; items, effects and agent are the
 * browser port of tests/test_effects.py. No real bridge, game or server: the
 * units fake the WebSocket and the WRAM. */
'use strict';
const assert = require('assert');
const path = require('path');
const Hud = require(path.join(__dirname, '../../web/js/hud.js'));
const Snes = require(path.join(__dirname, '../../web/js/snes.js'));
const Items = require(path.join(__dirname, '../../web/js/items.js'));
const Effects = require(path.join(__dirname, '../../web/js/effects.js'));
const Agent = require(path.join(__dirname, '../../web/js/agent.js'));
const HLGame = require(path.join(__dirname, '../../web/js/game.js'));

let passed = 0;
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

/* A WRAM that behaves like the game's: reads and writes by offset, a switch
   to drop the next write, and a count of how often the HUD flag was raised. */
function fakeWram() {
  const mem = new Uint8Array(0x10000);
  for (let i = 0; i < 32 * 5; i++) { mem[Hud.BUFFER + i * 2] = 0x7F; mem[Hud.BUFFER + i * 2 + 1] = 0x24; }
  const t = {
    mem, flags: 0, dropWrites: 0, dropReads: 0,
    read: async (addr, len) => (t.dropReads-- > 0 ? null : mem.slice(addr, addr + len)),
    write: async (addr, bytes) => {
      if (t.dropWrites-- > 0) return false;
      mem.set(bytes, addr);
      if (addr === Hud.FLAG) { t.flags++; mem[Hud.FLAG] = 0; }   // the NMI consumes it
      return true;
    },
    text: () => {
      let s = '';
      for (let i = 0; i < Hud.WIDTH; i++) {
        const w = mem[Hud.ADDR + i * 2] | (mem[Hud.ADDR + i * 2 + 1] << 8);
        if (w >= 0x255D && w < 0x255D + 26) s += String.fromCharCode(65 + w - 0x255D);
        else if (w >= 0x2490 && w < 0x2490 + 10) s += String(w - 0x2490);
        else if (w === Hud.COLON) s += ':';
        else if (w === Hud.SPACE) s += ' ';
        else s += '?';
      }
      return s;
    }
  };
  return t;
}

test('cleaning matches the server: accents fold, punctuation spaces, symbols drop', () => {
  assert.strictEqual(Hud.clean('  nice   death!! '), 'NICE DEATH');
  assert.strictEqual(Hud.clean('cool_streamer.99'), 'COOL STREAMER 99');
  assert.strictEqual(Hud.clean('Él está aquí 😀'), 'EL ESTA AQUI');
  assert.strictEqual(Hud.clean('!!! ???'), '');
  assert.strictEqual(Hud.clean('one two three four five six seven eight', 20), 'ONE TWO THREE FOUR');
});

test('a colon is the randomizer clock tile, kept only in lines the server built', async () => {
  assert.strictEqual(Hud.wordOf(':'), 0x2806);
  assert.strictEqual(Hud.clean('gt: big key?'), 'GT BIG KEY');
  assert.strictEqual(Hud.clean('BO: BOOK ON PED LOL', null, true), 'BO: BOOK ON PED LOL');
  const t = fakeWram(), h = new Hud.HudStrip(t);
  h.show('BO: BOOK ON PED LOL');
  await h.tick(0);
  assert.strictEqual(t.text(), 'BO: BOOK ON PED LOL ');
});

test('glyph words are the randomizer HUD sheet', () => {
  assert.strictEqual(Hud.wordOf('A'), 0x255D);
  assert.strictEqual(Hud.wordOf('Z'), 0x255D + 25);
  assert.strictEqual(Hud.wordOf('0'), 0x2490);
  assert.strictEqual(Hud.wordOf(' '), 0x247F);
  assert.strictEqual(Hud.ADDR, 0xC700 + (4 * 32 + 5) * 2);
});

test('a short line is centred, flagged, held, then the game cells come back', async () => {
  const t = fakeWram(), h = new Hud.HudStrip(t);
  h.show('ana finished 1st');
  await h.tick(100);
  assert.strictEqual(t.text(), '  ANA FINISHED 1ST  ');
  assert.ok(t.flags >= 1);
  await h.tick(103.9);
  assert.strictEqual(t.text().trim(), 'ANA FINISHED 1ST');
  await h.tick(104.1);
  assert.strictEqual(t.text(), ' '.repeat(20));
  assert.strictEqual(h.active(), false);
});

test('a long line scrolls to its end', async () => {
  const t = fakeWram(), h = new Hud.HudStrip(t);
  const line = 'BILLOGNA FINISHED 1ST 1H23M45S';
  h.show(line);
  await h.tick(0);
  assert.strictEqual(t.text(), line.slice(0, 20));
  for (let s = 1.5; s < 12; s += 0.5) await h.tick(s);
  assert.strictEqual(t.text(), line.slice(line.length - 20));
  await h.tick(40);
  assert.strictEqual(t.text(), ' '.repeat(20));
});

test('cells the game rewrites under the line are what gets restored', async () => {
  const t = fakeWram(), h = new Hud.HudStrip(t);
  h.show('GG');
  await h.tick(0);
  t.mem[Hud.ADDR + 2] = 0x34; t.mem[Hud.ADDR + 3] = 0x12;      // the game stamps cell 1
  await h.tick(1);
  assert.strictEqual(t.text().trim(), 'GG');                    // re-asserted over it
  await h.tick(5);
  assert.strictEqual(t.mem[Hud.ADDR + 2] | (t.mem[Hud.ADDR + 3] << 8), 0x1234);
  assert.strictEqual(t.mem[Hud.ADDR] | (t.mem[Hud.ADDR + 1] << 8), Hud.SPACE);
});

test('a dropped write is retried, never lost', async () => {
  const t = fakeWram(), h = new Hud.HudStrip(t);
  h.show('HELLO');
  t.dropWrites = 1;
  await h.tick(0);
  assert.strictEqual(t.text(), ' '.repeat(20));
  assert.strictEqual(h.queue.length, 1);
  await h.tick(0.5);
  assert.strictEqual(t.text().trim(), 'HELLO');
  t.dropWrites = 1;                       // and the restore too
  await h.tick(10);
  assert.strictEqual(t.text().trim(), 'HELLO');
  await h.tick(10.5);
  assert.strictEqual(t.text(), ' '.repeat(20));
});

test('a keyed line replaces itself in place instead of queueing', async () => {
  const t = fakeWram(), h = new Hud.HudStrip(t);
  h.show('RACE STARTS IN 3', 'cd');
  await h.tick(0);
  h.show('RACE STARTS IN 2', 'cd');
  h.show('RACE STARTS IN 1', 'cd');
  assert.strictEqual(h.queue.length, 0);
  await h.tick(1);
  assert.strictEqual(t.text().trim(), 'RACE STARTS IN 1');
});

test('the queue is bounded and drops immediate duplicates', () => {
  const h = new Hud.HudStrip(fakeWram());
  h.show('A'); h.show('A');
  assert.strictEqual(h.queue.length, 1);
  for (let i = 0; i < 12; i++) h.show('LINE ' + i);
  assert.strictEqual(h.queue.length, 6);
  assert.strictEqual(h.queue[5].text, 'LINE 11');
});

test('abort puts the game cells back at once; reset writes nothing', async () => {
  const t = fakeWram(), h = new Hud.HudStrip(t);
  h.show('TROLL'); h.show('MORE');
  await h.tick(0);
  assert.strictEqual(await h.abort(), true);
  assert.strictEqual(t.text(), ' '.repeat(20));
  assert.strictEqual(h.active(), false);
  h.show('AGAIN');
  await h.tick(1);
  const flags = t.flags;
  h.reset();
  assert.strictEqual(t.flags, flags);
  assert.strictEqual(h.active(), false);
});

/* snes.js against fake bridges on a virtual clock. Each port is one of:
   refuse (nothing listening), hang (takes the connection, never opens it; its
   close event fires twice, as node's does), jam (answers AppVersion, then
   nothing: QUsb2Snes on 2026-09-21), ok (a bridge with snes9x-nwa behind it).
   HyruleLink: an ok bridge also has a WRAM behind it (GetAddress answers in
   two binary frames, PutAddress lands, the NMI consumes the HUD flag), and
   `opts` goes to the Snes (urls, name, onTick). */
async function withBridges(modes, fn, opts) {
  const real = { setTimeout: global.setTimeout, clearTimeout: global.clearTimeout, WebSocket: global.WebSocket };
  let now = 0, seq = 0, open = 0;
  const due = new Map();
  const made = [];
  const bridge = { wram: new Uint8Array(0x10000), ops: [] };
  for (let i = 0; i < 32 * 5; i++) { bridge.wram[Hud.BUFFER + i * 2] = 0x7F; bridge.wram[Hud.BUFFER + i * 2 + 1] = 0x24; }
  const clock = {
    get now() { return now; },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        await new Promise(r => setImmediate(r));
        let next = null;
        for (const [id, [at]] of due) if (at <= end && (next === null || at < due.get(next)[0])) next = id;
        if (next === null) break;
        const [at, f] = due.get(next);
        due.delete(next);
        now = at;
        f();
      }
      now = end;
    }
  };
  global.setTimeout = (f, ms) => { due.set(++seq, [now + (ms || 0), f]); return seq; };
  global.clearTimeout = id => { due.delete(id); };
  global.WebSocket = function (url) {
    /* two more, for Chrome's throttle on a page with many failed sockets:
       throttled (refused, but only after 4.5 s) and slowok (an ok bridge
       whose socket takes 4 s to open) */
    const asked = modes[/:(\d+)/.exec(url)[1]] || 'refuse';
    const mode = asked === 'slowok' ? 'ok' : asked;
    const ws = this;
    let shut = false, put = null;
    ws.url = url;
    ws.readyState = 0;
    made.push(ws);
    clock.maxOpen = Math.max(clock.maxOpen || 0, ++open);
    const end = () => {
      if (shut) return;
      shut = true; open--; ws.readyState = 3;
      if (ws.onclose) ws.onclose({});
      if (mode === 'hang' && ws.onclose) ws.onclose({});
    };
    ws.close = () => { global.setTimeout(end, 0); };
    ws.send = data => {
      if (typeof data !== 'string') {                   // the bytes after a PutAddress
        if (put) {
          bridge.wram.set(new Uint8Array(data).subarray(0, put.len), put.addr);
          if (put.addr === Hud.FLAG) bridge.wram[Hud.FLAG] = 0;
          put = null;
        }
        return;
      }
      const msg = JSON.parse(data), op = msg.Opcode;
      bridge.ops.push([op, msg.Operands]);
      const reply = res => global.setTimeout(() => { if (!shut) ws.onmessage({ data: JSON.stringify({ Results: res }) }); }, 1);
      if (op === 'AppVersion') reply([mode === 'ok' ? 'SNI-v0.0.102a' : 'QUsb2Snes-0.7.35']);
      if (mode === 'ok' && op === 'DeviceList') reply(['emunwa://127.0.0.1:48879']);
      if (mode === 'ok' && op === 'Info') reply(['1.63-nwa', 'NWAccess', 'ALTTPR']);
      if (mode === 'ok' && op === 'GetAddress') {
        const addr = parseInt(msg.Operands[0], 16) - 0xF50000, len = parseInt(msg.Operands[1], 16);
        const bytes = bridge.wram.slice(addr, addr + len), cut = Math.max(1, len >> 1);
        global.setTimeout(() => { if (!shut) ws.onmessage({ data: bytes.slice(0, cut).buffer }); }, 1);
        if (cut < len) global.setTimeout(() => { if (!shut) ws.onmessage({ data: bytes.slice(cut).buffer }); }, 2);
      }
      if (mode === 'ok' && op === 'PutAddress') {
        put = { addr: parseInt(msg.Operands[0], 16) - 0xF50000, len: parseInt(msg.Operands[1], 16) };
      }
    };
    if (mode === 'refuse') global.setTimeout(end, 1);
    else if (mode === 'throttled') global.setTimeout(end, 4500);
    else if (mode !== 'hang') global.setTimeout(() => { if (!shut) { ws.readyState = 1; ws.onopen({}); } }, asked === 'slowok' ? 4000 : 1);
  };
  const states = [];
  const snes = new Snes(Object.assign({ onStatus: s => states.push(s.state) }, opts || {}));
  try {
    snes._try(0);                     // not start(): no game loop here
    await fn(snes, clock, made, states, bridge);
  } finally {
    snes._stopped = true;
    Object.assign(global, real);
  }
}

test('snes: nothing listening is no-bridge', () => withBridges({}, async (snes, clock) => {
  await clock.advance(1000);
  assert.strictEqual(snes.state, 'no-bridge');
}));

test('snes: a bridge that answers but never lists devices is stuck, not missing', () =>
  withBridges({ 23074: 'jam' }, async (snes, clock, made, states) => {
    await clock.advance(12000);
    assert.strictEqual(snes.state, 'stuck');
    assert.strictEqual(snes.bridge, 'QUsb2Snes');
    assert.ok(states.indexOf('no-bridge') < 0, 'never told to go and get a bridge: ' + states);
    assert.ok(made.length <= 3, made.length + ' sockets in 12 s');
    made.length = 0;
    await clock.advance(10000);
    assert.ok(made.length <= 2, made.length + ' sockets in the next 10 s');
    assert.strictEqual(snes.state, 'stuck');
  }));

test('snes: a port that never opens is one socket at a time, however its close events arrive', () =>
  withBridges({ 23074: 'hang', 8080: 'hang' }, async (snes, clock, made) => {
    await clock.advance(60000);
    assert.strictEqual(clock.maxOpen, 1);
    assert.ok(made.length <= 14, made.length + ' sockets in 60 s');
    assert.strictEqual(snes.state, 'stuck');
  }));

test('snes: a jammed bridge restarted as a working one attaches', () => {
  const modes = { 23074: 'jam' };
  return withBridges(modes, async (snes, clock) => {
    await clock.advance(12000);
    assert.strictEqual(snes.state, 'stuck');
    modes[23074] = 'ok';
    await clock.advance(8000);
    assert.strictEqual(snes.state, 'attached');
    assert.strictEqual(snes.bridge, 'SNI');
    assert.strictEqual(Snes.friendly(snes.device), 'Emulator (NWA)');
  });
});

test('snes: no bridge behind Chrome\'s socket throttle is still no-bridge, never stuck', () =>
  withBridges({ 23074: 'throttled', 8080: 'throttled' }, async (snes, clock, made, states) => {
    await clock.advance(120000);
    assert.ok(states.indexOf('stuck') < 0, 'a missing bridge was called jammed: ' + states);
    assert.strictEqual(snes.state, 'no-bridge');
    assert.ok(Snes.OPEN_MS > 5000, 'Chrome holds a socket back up to 5 s: ' + Snes.OPEN_MS);
  }));

test('snes: a bridge whose socket is held back 4 s still attaches', () =>
  withBridges({ 23074: 'slowok' }, async (snes, clock, made, states) => {
    await clock.advance(6000);
    assert.strictEqual(snes.state, 'attached');
    assert.strictEqual(made.length, 1, 'the slow socket was not given up on');
    assert.ok(states.indexOf('stuck') < 0);
  }));

/* HUD strip text straight out of a WRAM image (what the player would read). */
function stripText(mem) {
  let s = '';
  for (let i = 0; i < Hud.WIDTH; i++) {
    const w = mem[Hud.ADDR + i * 2] | (mem[Hud.ADDR + i * 2 + 1] << 8);
    if (w >= 0x255D && w < 0x255D + 26) s += String.fromCharCode(65 + w - 0x255D);
    else if (w >= 0x2490 && w < 0x2490 + 10) s += String(w - 0x2490);
    else if (w === Hud.SPACE) s += ' ';
    else s += '?';
  }
  return s;
}

test('snes: by default it looks on 23074 then 8080', () => withBridges({}, async (snes, clock, made) => {
  await clock.advance(1000);
  assert.strictEqual(snes.state, 'no-bridge');
  assert.deepStrictEqual(made.slice(0, 2).map(ws => ws.url), ['ws://localhost:23074', 'ws://localhost:8080']);
  assert.deepStrictEqual(Snes.URLS, ['ws://localhost:23074', 'ws://localhost:8080']);
}));

test('snes: opts.urls replaces the bridge list and the client is named HyruleLink', () =>
  withBridges({ 23174: 'ok', 23074: 'ok' }, async (snes, clock, made, states, bridge) => {
    await clock.advance(1000);
    assert.strictEqual(snes.state, 'attached');
    assert.ok(made.length >= 1 && made.every(ws => ws.url === 'ws://localhost:23174'), made.map(ws => ws.url).join());
    assert.deepStrictEqual(bridge.ops.filter(o => o[0] === 'Name'), [['Name', ['HyruleLink']]]);
  }, { urls: ['ws://localhost:23174'] }));

test('snes: onTick gets the module before the HUD ticks, and item writes need no switch', () => {
  const seen = [];
  let me = null, br = null;
  return withBridges({ 23074: 'ok' }, async (snes, clock, made, states, bridge) => {
    me = snes; br = bridge;
    assert.strictEqual(await snes.write(0xF342, new Uint8Array([1])), false);   // not attached yet
    await clock.advance(1000);
    assert.strictEqual(snes.state, 'attached');
    assert.strictEqual('writes' in snes, false);
    assert.strictEqual(typeof snes.allowWrites, 'undefined');
    bridge.wram[0x10] = 0x01;                       // file select
    let p = snes._loop(); await clock.advance(1000); await p;
    bridge.wram[0x10] = 0x07;                       // a save is loaded
    p = snes._loop(); await clock.advance(1000); await p;
    assert.deepStrictEqual(seen.map(s => [s[0], s[1]]), [[1, false], [7, true]]);
    assert.strictEqual(seen[1][2], ' '.repeat(20));  // onTick ran before the strip was drawn
    assert.strictEqual(bridge.wram[0xF342], 1);      // the write from inside onTick landed
    assert.strictEqual(stripText(bridge.wram), '  SWORD SENT TO BO  ');
    assert.strictEqual(bridge.wram[Hud.FLAG], 0);    // consumed, as the NMI does
    assert.strictEqual(snes.module, 7);
    assert.strictEqual(snes.playable, true);
  }, { onTick: async (mod, playable) => {
    seen.push([mod, playable, stripText(br.wram)]);
    if (playable) {
      await me.write(0xF342, new Uint8Array([1]));
      me.say('Sword sent to Bo');
    }
  } });
});

test('snes: the mode tables are exported', () => {
  assert.strictEqual(Snes.PLAYABLE[0x07], 1);
  assert.strictEqual(Snes.PLAYABLE[0x12], undefined);
  assert.strictEqual(Snes.OUT_OF_GAME[0x01], 1);
  assert.strictEqual(Snes.OUT_OF_GAME[0x12], undefined);
  assert.deepStrictEqual(Object.keys(Snes.PLAYABLE).map(Number), Object.keys(Items.PLAYABLE_MODES).map(Number));
  assert.deepStrictEqual(Object.keys(Snes.OUT_OF_GAME).map(Number), Object.keys(Items.OUT_OF_GAME_MODES).map(Number));
});

/* ---- items.js ---- */

test('items: discoveredLevel for every kind', () => {
  const lvl = (key, raw) => Items.discoveredLevel(Items.BY_KEY[key], raw);
  // progressive: the byte is the tier, capped
  assert.deepStrictEqual([0, 1, 2, 3, 4, 5, 0xFF].map(r => lvl('sword', r)), [0, 1, 2, 3, 4, 4, 4]);
  assert.deepStrictEqual([0, 1, 2, 3].map(r => lvl('armor', r)), [0, 1, 2, 2]);
  // simple: anything nonzero is present (the mirror's 2 too)
  assert.deepStrictEqual([0, 1, 2, 0x80].map(r => lvl('mirror', r)), [0, 1, 1, 1]);
  assert.deepStrictEqual([0, 1].map(r => lvl('hookshot', r)), [0, 1]);
  // boots
  assert.deepStrictEqual([0, 1, 2].map(r => lvl('boots', r)), [0, 1, 1]);
  // bitfield: one bit (or the flute's two) of $7EF38C
  assert.deepStrictEqual([0x00, 0x80, 0x7F, 0xFF].map(r => lvl('blue_boomerang', r)), [0, 1, 0, 1]);
  assert.deepStrictEqual([0x00, 0x01, 0x02, 0x03, 0x04].map(r => lvl('flute', r)), [0, 1, 1, 1, 0]);
  assert.deepStrictEqual([0x08, 0x20, 0x28].map(r => lvl('mushroom', r)), [0, 1, 1]);
  assert.deepStrictEqual([0x04, 0x03].map(r => lvl('shovel', r)), [1, 0]);
  // bow: BowTracking, not the equip byte
  assert.deepStrictEqual([0x00, 0x40, 0x80, 0xC0, 0x3F, 0xBF].map(r => lvl('bow', r)), [0, 0, 1, 2, 0, 1]);
  assert.strictEqual(Items.discoveredLevel('bow', 0xC0), 2);    // a key works too
});

test('items: itemImage and tierLabel match shared.items', () => {
  assert.strictEqual(Items.itemImage('sword', 0), 'sword-1.png');
  assert.strictEqual(Items.itemImage('sword', 2), 'sword-2.png');
  assert.strictEqual(Items.itemImage('sword', 9), 'sword-4.png');
  assert.strictEqual(Items.itemImage('bow', 2), 'bow-2.png');
  assert.strictEqual(Items.itemImage('armor'), 'armor-1.png');
  assert.strictEqual(Items.itemImage('lamp', 1), 'lamp.png');
  assert.strictEqual(Items.itemImage('flute', 0), 'flute.png');
  assert.strictEqual(Items.tierLabel(Items.BY_KEY.sword, 2), 'Master');
  assert.strictEqual(Items.tierLabel(Items.BY_KEY.armor, 0), 'Green');
  assert.strictEqual(Items.tierLabel(Items.BY_KEY.lamp, 1), 'owned');
  assert.strictEqual(Items.tierLabel(Items.BY_KEY.lamp, 0), '—');
  assert.strictEqual(Items.tierLabel(Items.BY_KEY.sword, 7), 'owned');
});

test('items: one contiguous tracked range, shared bytes grouped', () => {
  assert.strictEqual(Items.TRACKED_START, 0xF342);
  assert.strictEqual(Items.TRACKED_SIZE, 0xF38E - 0xF342 + 1);
  assert.strictEqual(Items.TRACKED_ADDRS[0], Items.TRACKED_START);
  assert.strictEqual(Items.TRACKED_ADDRS[Items.TRACKED_ADDRS.length - 1], 0xF38E);
  assert.deepStrictEqual(Items.ITEMS_BY_ADDR[0xF38C].map(it => it.key),
    ['blue_boomerang', 'red_boomerang', 'mushroom', 'powder', 'shovel', 'flute']);
  assert.strictEqual(Items.GAME_MODE_ADDR, 0x10);
  assert.strictEqual(Items.ITEMS.length, Object.keys(Items.BY_KEY).length);
  assert.ok(Effects.ABILITY_ADDR > Items.TRACKED_START && Effects.ABILITY_ADDR < Items.TRACKED_START + Items.TRACKED_SIZE);
});

/* ---- effects.js ---- */

/* A game WRAM for the item code: every read and write is logged in `ops`;
   `dropReads` drops the next reads whatever the address (FlakyReadTransport),
   `drop[addr]` drops reads of one address, `writable = false` loses every
   write, `slow` makes each call yield to the event loop first. */
function fakeGame() {
  const mem = new Uint8Array(0x10000);
  const g = {
    mem, ops: [], dropReads: 0, drop: {}, writable: true, slow: false,
    read: async (addr, len) => {
      if (g.slow) await new Promise(r => setImmediate(r));
      g.ops.push(['r', addr, len]);
      if (g.dropReads > 0) { g.dropReads--; return null; }
      if (g.drop[addr] > 0) { g.drop[addr]--; return null; }
      return mem.slice(addr, addr + len);
    },
    write: async (addr, bytes) => {
      if (g.slow) await new Promise(r => setImmediate(r));
      g.ops.push(['w', addr, Array.from(bytes)]);
      if (!g.writable) return false;
      mem.set(bytes, addr);
      return true;
    },
    writesTo: addr => g.ops.filter(o => o[0] === 'w' && o[1] === addr).map(o => o[2][0])
  };
  return g;
}
const hex = n => n.toString(16).toUpperCase();
const trace = g => g.ops.map(o => o[0] + ' ' + hex(o[1]) + (o[0] === 'w' ? '=' + o[2].map(hex).join(',') : '/' + o[2]));

test('effects: a wood bow sets BowTracking 0x80 only and equips 1', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  const raw = await fx.enable('bow', 1);
  assert.strictEqual(raw & 0xC0, 0x80);
  assert.strictEqual(g.mem[Items.BOW_EQUIP_ADDR], 0x01);
  assert.strictEqual(g.mem[Effects.ARROWS_ADDR], 0);
  assert.strictEqual(Items.discoveredLevel(Items.BY_KEY.bow, g.mem[0xF38E]), 1);
});

test('effects: a silver bow sets 0xC0, equips 4 and brings 30 arrows only when there are none', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  await fx.enable('bow', 2);
  assert.strictEqual(g.mem[0xF38E] & 0xC0, 0xC0);
  assert.strictEqual(g.mem[Items.BOW_EQUIP_ADDR], 0x04);
  assert.strictEqual(g.mem[Effects.ARROWS_ADDR], Effects.DEFAULT_ARROWS);
  g.mem[Effects.ARROWS_ADDR] = 12;
  await fx.enable('bow', 2);
  assert.strictEqual(g.mem[Effects.ARROWS_ADDR], 12);
});

test('effects: revoking the bow clears 0xC0 and keeps the other BowTracking bits', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  g.mem[0xF38E] = 0x05;
  await fx.enable('bow', 2);
  assert.strictEqual(g.mem[0xF38E], 0xC5);
  assert.strictEqual(await fx.disable('bow'), 0x05);
  assert.strictEqual(g.mem[Items.BOW_EQUIP_ADDR], 0x00);
  assert.strictEqual(Items.discoveredLevel(Items.BY_KEY.bow, g.mem[0xF38E]), 0);
});

test('effects: shared-slot grants do not clobber each other', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g), T = Items.INV_TRACK_ADDR;
  await fx.enable('shovel', 1);
  await fx.enable('flute', 1);
  assert.ok(g.mem[T] & Effects.SHOVEL_BIT);
  assert.ok(g.mem[T] & Effects.FLUTE_ACTIVE_BIT);
  assert.strictEqual(g.mem[Effects.FLUTE_EQUIP_ADDR], 0x03);
  await fx.enable('powder', 1);
  await fx.enable('mushroom', 1);
  assert.ok(g.mem[T] & Effects.POWDER_BIT);
  assert.ok(g.mem[T] & Effects.MUSHROOM_OWN);
  await fx.enable('blue_boomerang', 1);
  await fx.enable('red_boomerang', 1);
  assert.ok(g.mem[T] & Effects.BOOM_BLUE_BIT);
  assert.ok(g.mem[T] & Effects.BOOM_RED_BIT);
  assert.strictEqual(g.mem[Effects.BOOM_EQUIP_ADDR], 0x02);   // red equipped last
  for (const k of ['shovel', 'flute', 'powder', 'mushroom', 'blue_boomerang', 'red_boomerang']) {
    assert.strictEqual(Items.discoveredLevel(Items.BY_KEY[k], g.mem[T]), 1, k);
  }
  // the enum bytes were set, never OR-ed
  assert.deepStrictEqual(g.writesTo(Effects.FLUTE_EQUIP_ADDR), [1, 3]);
  assert.deepStrictEqual(g.writesTo(Effects.POWDER_EQUIP_ADDR), [2, 1]);
});

test('effects: revoking the flute keeps the shovel and falls the slot back to it', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g), T = Items.INV_TRACK_ADDR;
  await fx.enable('shovel', 1);
  await fx.enable('flute', 1);
  await fx.disable('flute');
  assert.strictEqual(g.mem[T] & Effects.FLUTE_ANY_BITS, 0);
  assert.ok(g.mem[T] & Effects.SHOVEL_BIT);
  assert.strictEqual(g.mem[Effects.FLUTE_EQUIP_ADDR], 0x01);
  await fx.enable('flute', 1);                                  // and the other way round
  await fx.enable('shovel', 1);
  await fx.disable('shovel');
  assert.strictEqual(g.mem[Effects.FLUTE_EQUIP_ADDR], 0x03);
  assert.strictEqual(Items.discoveredLevel(Items.BY_KEY.flute, g.mem[T]), 1);
});

test('effects: revoking the powder keeps the mushroom', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g), T = Items.INV_TRACK_ADDR;
  await fx.enable('mushroom', 1);
  await fx.enable('powder', 1);
  await fx.disable('powder');
  assert.strictEqual(g.mem[T] & Effects.POWDER_BIT, 0);
  assert.ok(g.mem[T] & Effects.MUSHROOM_OWN);
  assert.strictEqual(g.mem[Effects.POWDER_EQUIP_ADDR], 0x01);
  assert.strictEqual(Items.discoveredLevel(Items.BY_KEY.mushroom, g.mem[T]), 1);
});

test('effects: revoking the red boomerang falls back to blue; an unequipped half leaves the slot alone', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  await fx.enable('blue_boomerang', 1);
  await fx.enable('red_boomerang', 1);
  await fx.disable('red_boomerang');
  assert.strictEqual(g.mem[Effects.BOOM_EQUIP_ADDR], 0x01);
  await fx.enable('red_boomerang', 1);
  g.mem[Effects.BOOM_EQUIP_ADDR] = 0x01;                        // player picked blue in the menu
  g.ops.length = 0;
  await fx.disable('red_boomerang');
  assert.deepStrictEqual(g.writesTo(Effects.BOOM_EQUIP_ADDR), []);
  assert.strictEqual(g.mem[Effects.BOOM_EQUIP_ADDR], 0x01);
});

test('effects: a dropped read aborts a boots grant without clobbering 0x68', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  g.mem[Effects.ABILITY_ADDR] = 0x68;
  g.dropReads = 1000;
  await assert.rejects(fx.enable('boots', 1), /could not read WRAM \$F379/);
  assert.strictEqual(g.mem[Effects.ABILITY_ADDR], 0x68);
  assert.deepStrictEqual(g.writesTo(Effects.ABILITY_ADDR), []);
  assert.strictEqual(g.ops.filter(o => o[0] === 'r').length, 6);   // six tries, then give up
});

test('effects: transient dropped reads are retried, not clobbered', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  g.mem[Effects.ABILITY_ADDR] = 0x68;
  g.dropReads = 3;
  await fx.enable('boots', 1);
  assert.strictEqual(g.mem[Effects.BOOTS_ADDR], 0x01);
  assert.strictEqual(g.mem[Effects.ABILITY_ADDR], 0x68 | 0x04);
});

test('effects: boots write for write as item_effects.py, and the revoke clears both', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  g.mem[Effects.ABILITY_ADDR] = 0x68;
  assert.strictEqual(await fx.enable('boots', 1), 1);
  assert.deepStrictEqual(trace(g), ['w F355=1', 'r F379/1', 'w F379=6C', 'r F355/1']);
  g.ops.length = 0;
  assert.strictEqual(await fx.disable('boots'), 0);
  assert.deepStrictEqual(trace(g), ['w F355=0', 'r F379/1', 'w F379=68', 'r F355/1']);
});

test('effects: the mirror writes 2, not 1', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  assert.strictEqual(await fx.enable('mirror', 1), 2);
  assert.strictEqual(g.mem[0xF353], 2);
  assert.strictEqual(await fx.disable('mirror'), 0);
});

test('effects: progressive writes the exact level, never +1', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  g.mem[0xF359] = 1;
  assert.strictEqual(await fx.enable('sword', 3), 3);
  assert.strictEqual(await fx.enable('sword', 2), 2);             // down is exact too
  assert.deepStrictEqual(g.writesTo(0xF359), [3, 2]);
  assert.strictEqual(await fx.disable('sword'), 0);
});

test('effects: a write that does not land fails verification by name', async () => {
  const g = fakeGame(), fx = new Effects.Effects(g);
  g.writable = false;
  await assert.rejects(fx.enable('sword', 2), { message: 'grant verification failed for sword: read 0' });
  g.writable = true;
  g.mem[0xF34A] = 1;
  g.writable = false;
  await assert.rejects(fx.disable('lamp'), { message: 'revoke verification failed for lamp: read 1' });
});

/* ---- agent.js ---- */

/* An agent on a fake game with a fake server socket: `up` false makes send
   return false, as game.js does while the agent socket is down. */
function makeAgent(g) {
  const h = { up: true, sent: [], said: [], notes: [], logs: [] };
  h.agent = new Agent.Agent({
    game: g,
    send: m => { if (!h.up) return false; h.sent.push(JSON.parse(JSON.stringify(m))); return true; },
    say: t => h.said.push(t),
    onNotify: t => h.notes.push(t),
    onLog: t => h.logs.push(t)
  });
  h.poll = () => h.agent.tick(g.mem[Items.GAME_MODE_ADDR]);     // what snes.js does each loop
  h.pickups = () => h.sent.filter(m => m.type === 'pickup');
  h.last = () => h.sent[h.sent.length - 1];
  return h;
}
function inGame() { const g = fakeGame(); g.mem[Items.GAME_MODE_ADDR] = 0x07; return g; }
const LAMP = 0xF34A;

test('agent: a grant is applied, remembered as our own write and acked ok', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.agent.handle({ type: 'grant', item: 'sword', level: 2 });
  assert.deepStrictEqual(h.last(), { type: 'applied', item: 'sword', action: 'grant', ok: true });
  assert.strictEqual(g.mem[0xF359], 2);
  assert.strictEqual(h.agent.expected.get(0xF359), 2);
  await h.agent.handle({ type: 'revoke', item: 'sword' });
  assert.deepStrictEqual(h.last(), { type: 'applied', item: 'sword', action: 'revoke', ok: true });
  assert.strictEqual(g.mem[0xF359], 0);
});

test('agent: a failed write is acked with the error after 3 tries', async () => {
  const g = inGame(), h = makeAgent(g);
  g.writable = false;
  await h.agent.handle({ type: 'grant', item: 'sword', level: 2 });
  const ack = h.last();
  assert.strictEqual(ack.type, 'applied');
  assert.strictEqual(ack.ok, false);
  assert.strictEqual(ack.action, 'grant');
  assert.ok(/verification failed/.test(ack.error), ack.error);
  assert.ok(ack.error.length <= 200);
  assert.strictEqual(g.writesTo(0xF359).length, 3);
  assert.strictEqual(h.agent.expected.has(0xF359), false);
});

test('agent: a transient failure is retried instead of dropping the item', async () => {
  const g = inGame(), h = makeAgent(g);
  g.drop[0xF342] = 6;                     // the first verification read never comes back
  await h.agent.handle({ type: 'grant', item: 'hookshot', level: 1 });
  assert.strictEqual(h.last().ok, true);
  assert.strictEqual(g.mem[0xF342], 1);
  assert.strictEqual(g.writesTo(0xF342).length, 2);
});

test('agent: a silver bow pickup is read from the tracking byte', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.poll();                                            // seed: no bow
  g.mem[Items.BOW_FLAGS_ADDR] = 0xC0;
  await h.poll();
  assert.deepStrictEqual(h.pickups(), [{ type: 'pickup', item: 'bow', level: 2 }]);
});

test('agent: a tier up is reported at the new level; a shared byte reports only what changed', async () => {
  const g = inGame(), h = makeAgent(g);
  g.mem[0xF359] = 1;
  g.mem[Items.INV_TRACK_ADDR] = Effects.SHOVEL_BIT;
  await h.poll();
  assert.deepStrictEqual(h.pickups(), []);                   // first sight is silent
  g.mem[0xF359] = 2;
  g.mem[Items.INV_TRACK_ADDR] |= Effects.FLUTE_INACTIVE_BIT;
  await h.poll();
  assert.deepStrictEqual(h.pickups(), [
    { type: 'pickup', item: 'sword', level: 2 },
    { type: 'pickup', item: 'flute', level: 1 }]);
  g.mem[0xF359] = 1;                                         // a downgrade is not a find
  await h.poll();
  assert.strictEqual(h.pickups().length, 2);
});

test('agent: the inventory is one contiguous read of TRACKED_START/TRACKED_SIZE', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.poll();
  const inRange = g.ops.filter(o => o[0] === 'r' && o[1] >= Items.TRACKED_START
    && o[1] < Items.TRACKED_START + Items.TRACKED_SIZE);
  assert.deepStrictEqual(inRange, [['r', Items.TRACKED_START, Items.TRACKED_SIZE]]);
  assert.strictEqual(g.ops.filter(o => o[0] === 'r').length, 1);   // the ability byte comes from it too
});

test('agent: a grant at file select waits, then lands when play resumes', async () => {
  const g = fakeGame(), h = makeAgent(g);
  g.mem[Items.GAME_MODE_ADDR] = 0x01;
  await h.agent.handle({ type: 'grant', item: 'lamp', level: 1 });
  assert.strictEqual(g.mem[LAMP], 0);
  assert.deepStrictEqual(h.agent.pending.get('lamp'), [1, true]);
  assert.strictEqual(h.sent.length, 0);                      // no ack until it is applied
  g.mem[Items.GAME_MODE_ADDR] = 0x07;
  await h.poll();
  assert.strictEqual(g.mem[LAMP], 1);
  assert.strictEqual(h.agent.pending.size, 0);
  assert.deepStrictEqual(h.last(), { type: 'applied', item: 'lamp', action: 'grant', ok: true });
  assert.deepStrictEqual(h.pickups(), []);
});

test('agent: a newer command supersedes a deferred one', async () => {
  const g = fakeGame(), h = makeAgent(g);
  g.mem[Items.GAME_MODE_ADDR] = 0x01;
  await h.agent.handle({ type: 'grant', item: 'lamp', level: 1 });
  await h.agent.handle({ type: 'revoke', item: 'lamp' });
  g.mem[Items.GAME_MODE_ADDR] = 0x07;
  await h.poll();
  assert.strictEqual(g.mem[LAMP], 0);
  assert.deepStrictEqual(g.writesTo(LAMP), [0]);             // only the revoke ran
});

test('agent: an unreadable module defers too (not in the Python agent)', async () => {
  const g = inGame(), h = makeAgent(g);
  g.dropReads = 1;
  await h.agent.handle({ type: 'grant', item: 'hookshot', level: 1 });
  assert.deepStrictEqual(h.agent.pending.get('hookshot'), [1, true]);
  assert.strictEqual(g.writesTo(0xF342).length, 0);
  await h.poll();
  assert.strictEqual(g.mem[0xF342], 1);
  assert.strictEqual(h.last().ok, true);
});

test('agent: a save reload sends resync and reports no pickup', async () => {
  const g = inGame(), h = makeAgent(g);
  g.mem[LAMP] = 1;                                           // the player holds the lamp
  await h.poll();                                            // seed
  await h.agent.handle({ type: 'revoke', item: 'lamp' });    // the server took it
  await h.poll();                                            // our write echoes back
  g.mem[Items.GAME_MODE_ADDR] = 0x17;                        // save and quit
  await h.poll();
  assert.strictEqual(h.agent.outOfGame, true);
  g.mem[LAMP] = 1;                                           // the old save has the lamp
  g.mem[Items.GAME_MODE_ADDR] = 0x07;
  h.sent.length = 0;
  await h.poll();
  assert.deepStrictEqual(h.pickups(), []);
  assert.deepStrictEqual(h.sent, [{ type: 'resync' }]);
  assert.strictEqual(h.agent.outOfGame, false);
  assert.strictEqual(h.agent.baseline.get(LAMP), 1);         // re-seeded from the game
});

test('agent: a link blip keeps the baseline and reports the pickup made during it', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.poll();                                            // seed: no lamp
  h.agent.onGameLost();
  g.mem[LAMP] = 1;                                           // found while the link was down
  h.agent.onGameAttached(false);
  assert.deepStrictEqual(h.sent.slice(-3), [
    { type: 'status', emu: false }, { type: 'resync' }, { type: 'status', emu: true }]);
  h.sent.length = 0;
  await h.poll();
  assert.deepStrictEqual(h.pickups(), [{ type: 'pickup', item: 'lamp', level: 1 }]);
});

test('agent: death and other non-playable modules do not resync', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.poll();
  for (const mod of [0x12, 0x13, 0x15, 0x16, 0x19]) {         // death, spotlights, mirror, victory
    g.mem[Items.GAME_MODE_ADDR] = mod;
    await h.poll();
  }
  g.mem[Items.GAME_MODE_ADDR] = 0x07;
  h.sent.length = 0;
  await h.poll();
  assert.strictEqual(h.agent.outOfGame, false);
  assert.deepStrictEqual(h.sent, []);
});

test('agent: our own grant is not reported as a pickup', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.poll();                                            // seed
  await h.agent.handle({ type: 'grant', item: 'hookshot', level: 1 });
  await h.agent.handle({ type: 'grant', item: 'bow', level: 2 });
  await h.poll();
  assert.deepStrictEqual(h.pickups(), []);
  assert.strictEqual(h.agent.expected.size, 0);              // both echoes accepted
  assert.strictEqual(h.agent.baseline.get(0xF342), 1);
  g.mem[0xF359] = 1;                                         // a real find still counts
  await h.poll();
  assert.deepStrictEqual(h.pickups(), [{ type: 'pickup', item: 'sword', level: 1 }]);
});

test('agent: the boots run flag is re-asserted when the game clears it and cleared after a revoke', async () => {
  const g = inGame(), h = makeAgent(g), A = Effects.ABILITY_ADDR;
  g.mem[A] = 0x68;
  await h.agent.handle({ type: 'grant', item: 'boots', level: 1 });
  assert.strictEqual(h.agent.bootsOwned, true);
  assert.strictEqual(g.mem[A], 0x6C);
  await h.poll();
  assert.deepStrictEqual(g.writesTo(A), [0x6C]);             // right already: no write
  g.mem[A] = 0x68;                                           // a transition cleared it
  await h.poll();
  assert.strictEqual(g.mem[A], 0x6C);
  await h.agent.handle({ type: 'revoke', item: 'boots' });
  assert.strictEqual(h.agent.bootsOwned, false);
  assert.strictEqual(g.mem[A], 0x68);
  g.mem[A] = 0x6C;                                           // a lost revoke left it set
  await h.poll();
  assert.strictEqual(g.mem[A], 0x68);
  assert.strictEqual(g.mem[Effects.BOOTS_ADDR], 0);
});

test('agent: pickups made while the server is away wait in the outbox and go out on open', async () => {
  const g = inGame(), h = makeAgent(g);
  h.agent.setEmu(true);
  h.up = false;
  await h.poll();                                            // seed
  g.mem[LAMP] = 1;
  g.mem[0xF359] = 2;
  await h.poll();
  assert.strictEqual(h.sent.length, 0);
  assert.deepStrictEqual(h.agent.outbox.map(m => m.item), ['lamp', 'sword']);   // address order
  h.up = true;
  h.agent.onServerOpen();
  assert.deepStrictEqual(h.sent, [
    { type: 'status', emu: true },
    { type: 'pickup', item: 'lamp', level: 1 },
    { type: 'pickup', item: 'sword', level: 2 }]);
  assert.strictEqual(h.agent.outbox.length, 0);
});

test('agent: an outbox flush that fails halfway keeps the rest in order', async () => {
  const g = inGame(), h = makeAgent(g);
  h.agent.outbox = [{ type: 'pickup', item: 'lamp', level: 1 }, { type: 'pickup', item: 'hookshot', level: 1 }];
  let left = 2;                                              // status + one pickup, then down
  h.agent.o.send = m => { if (left-- <= 0) return false; h.sent.push(m); return true; };
  h.agent.onServerOpen();
  assert.deepStrictEqual(h.agent.outbox, [{ type: 'pickup', item: 'hookshot', level: 1 }]);
});

test('agent: apply and tick never interleave', async () => {
  const g = inGame(), h = makeAgent(g);
  g.slow = true;
  await h.poll();                                            // seed
  g.ops.length = 0;
  await Promise.all([h.poll(), h.agent.handle({ type: 'grant', item: 'boots', level: 1 }), h.poll()]);
  const snap = 'r ' + hex(Items.TRACKED_START) + '/' + Items.TRACKED_SIZE;
  assert.deepStrictEqual(trace(g), [
    snap,                                                    // tick 1
    'r 10/1', 'w F355=1', 'r F379/1', 'w F379=4', 'r F355/1',  // the grant, whole
    snap]);                                                  // tick 2 (the echo, no write)
  assert.deepStrictEqual(h.pickups(), []);
});

test('agent: a hello ownership push (one command per catalog item) applies in order, each acked', async () => {
  const g = inGame(), h = makeAgent(g);
  g.slow = true;
  g.mem[LAMP] = 1;                                           // a stale save holds the lamp
  await h.poll();                                            // seed
  const own = { sword: 2, bow: 2, boots: 1, flute: 1, shovel: 1, mirror: 1 };
  const cmds = Items.ITEMS.map(it => own[it.key]
    ? { type: 'grant', item: it.key, level: own[it.key] } : { type: 'revoke', item: it.key });
  const done = cmds.map(m => h.agent.handle(m));             // as game.js forwards them: no waiting
  await h.poll();                                            // a poll queued behind the push
  await Promise.all(done);
  const acks = h.sent.filter(m => m.type === 'applied');
  assert.strictEqual(acks.length, Items.ITEMS.length);
  assert.deepStrictEqual(acks.map(a => a.item), Items.ITEMS.map(it => it.key));
  assert.ok(acks.every(a => a.ok), JSON.stringify(acks.filter(a => !a.ok)));
  assert.strictEqual(g.mem[LAMP], 0);
  assert.strictEqual(g.mem[0xF359], 2);
  assert.strictEqual(g.mem[0xF38E] & 0xC0, 0xC0);
  assert.strictEqual(g.mem[Items.INV_TRACK_ADDR], Effects.SHOVEL_BIT | Effects.FLUTE_ACTIVE_BIT);
  assert.strictEqual(g.mem[Effects.FLUTE_EQUIP_ADDR], 0x03);
  assert.strictEqual(g.mem[0xF353], 2);
  assert.deepStrictEqual(h.pickups(), []);                   // the push is not a find
});

test('agent: notify goes to the page and the HUD, whitespace folded, capped at 120', async () => {
  const g = inGame(), h = makeAgent(g);
  await h.agent.handle({ type: 'notify', text: ' Sword  stolen\nfrom Zelda ' });
  assert.deepStrictEqual(h.notes, ['Sword stolen from Zelda']);
  assert.deepStrictEqual(h.said, ['Sword stolen from Zelda']);
  await h.agent.handle({ type: 'notify', text: '   ' });
  assert.strictEqual(h.notes.length, 1);
  await h.agent.handle({ type: 'notify', text: 'x'.repeat(300) });
  assert.strictEqual(h.said[1].length, 120);
  assert.strictEqual(g.ops.length, 0);                       // notify never touches the game itself
});

test('agent: status on attach and loss; an unknown item is ignored; reject is logged', async () => {
  const g = inGame(), h = makeAgent(g);
  h.agent.onGameAttached(true);
  assert.deepStrictEqual(h.sent, [{ type: 'status', emu: true }]);   // first attach: no resync
  h.agent.onGameLost();
  assert.deepStrictEqual(h.last(), { type: 'status', emu: false });
  h.sent.length = 0;
  await h.agent.handle({ type: 'grant', item: 'bottle', level: 1 });
  await h.agent.handle({ type: 'grant', item: 'constructor', level: 1 });
  assert.deepStrictEqual(h.sent, []);
  assert.strictEqual(g.ops.length, 0);
  await h.agent.handle({ type: 'reject', reason: 'bad room/player token' });
  assert.ok(h.logs.some(l => /bad room\/player token/.test(l)));
});

test('game: a line is on the HUD only with a game linked, lines on and a save loaded', async () => {
  const fate = HLGame.lineFate;
  assert.strictEqual(fate(null, true), 'off');
  assert.strictEqual(fate({ state: 'searching', module: null }, true), 'link');
  assert.strictEqual(fate({ state: 'attached', module: 0x07, _outOfGame: false }, false), 'hud');
  assert.strictEqual(fate({ state: 'attached', module: 0x07, _outOfGame: false }, true), '');
  // a door spotlight or a death keeps the line queued until play resumes
  assert.strictEqual(fate({ state: 'attached', module: 0x12, _outOfGame: false }, true), '');
  // title and file select: the line is dropped when the save loads
  assert.strictEqual(fate({ state: 'attached', module: 0x01, _outOfGame: true }, true), 'save');
  assert.strictEqual(fate({ state: 'attached', module: 0x05, _outOfGame: true }, true), 'save');
  assert.strictEqual(fate({ state: 'attached', module: null, _outOfGame: true }, true), 'save');
  // and the real thing: a line said on the file select never reaches the strip
  await withBridges({ 23074: 'ok' }, async (snes, clock, made, states, bridge) => {
    const poll = async () => { const p = snes._loop(); await clock.advance(1000); await p; };
    await clock.advance(1000);
    assert.strictEqual(snes.state, 'attached');
    assert.strictEqual(fate(snes, true), 'save');     // attached, not polled yet
    bridge.wram[0x10] = 0x01;                         // file select
    await poll();
    assert.strictEqual(fate(snes, true), 'save');
    snes.say('Lamp sent to Bo');
    bridge.wram[0x10] = 0x07;                         // the save loads
    await poll();
    await poll();
    assert.strictEqual(stripText(bridge.wram), ' '.repeat(20));
    assert.strictEqual(fate(snes, true), '');
    snes.say('Sword sent to Bo');
    await poll();
    assert.strictEqual(stripText(bridge.wram), '  SWORD SENT TO BO  ');
  });
});

/* ---- game.js link(): one game link per room per browser ---- */

/* navigator.locks as Chrome runs it, for exclusive locks: a grant runs the
   callback in a later task and holds until the callback's promise settles;
   ifAvailable answers null when it is taken or anyone is queued; steal
   rejects the holder's request with AbortError and preempts the queue; a
   signal takes a queued request out of line. */
function fakeLocks() {
  const held = new Map(), queues = new Map();
  const line = name => { if (!queues.has(name)) queues.set(name, []); return queues.get(name); };
  const abortError = () => { const e = new Error('The request was aborted.'); e.name = 'AbortError'; return e; };
  const L = {
    log: [],
    holder: name => (held.has(name) ? held.get(name).req.who : null),
    waiting: name => line(name).length
  };
  function grant(req) {
    const lease = { req };
    held.set(req.name, lease);
    setImmediate(() => {
      Promise.resolve().then(() => req.cb({ name: req.name, mode: 'exclusive' })).then(
        v => settle(lease, () => req.resolve(v)), e => settle(lease, () => req.reject(e)));
    });
  }
  function settle(lease, done) {
    if (held.get(lease.req.name) !== lease) return;     // stolen: already rejected
    held.delete(lease.req.name);
    done();
    const next = line(lease.req.name).shift();
    if (next) grant(next);
  }
  L.request = (name, opts, cb) => {
    if (typeof opts === 'function') { cb = opts; opts = {}; }
    L.log.push({ name, steal: !!opts.steal, ifAvailable: !!opts.ifAvailable, signal: !!opts.signal });
    if (opts.steal && (opts.ifAvailable || opts.signal)) return Promise.reject(new Error('NotSupportedError'));
    return new Promise((resolve, reject) => {
      const req = { name, cb, resolve, reject, who: cb };
      if (opts.steal) {
        const cur = held.get(name);
        if (cur) { held.delete(name); cur.req.reject(abortError()); }
        grant(req);
      } else if (held.has(name) || line(name).length) {
        if (opts.ifAvailable) {
          setImmediate(() => { Promise.resolve().then(() => cb(null)).then(resolve, reject); });
          return;
        }
        if (opts.signal) opts.signal.addEventListener('abort', () => {
          const i = line(name).indexOf(req);
          if (i >= 0) { line(name).splice(i, 1); reject(abortError()); }
        });
        line(name).push(req);
      } else {
        grant(req);
      }
    });
  };
  return L;
}

/* One browser: a fake bridge (Snes), agent and room socket for game.js to
   start, counted, so a test can see which tab opened what. */
async function withTabs(fn) {
  const saved = { Snes: global.Snes, HLAgent: global.HLAgent, WebSocket: global.WebSocket };
  const w = { snes: [], sockets: [] };
  function FakeSnes(o) {
    this.o = o; this.state = 'searching'; this.started = false; this.stopped = false;
    this.hud = { active: () => false, abort: async () => true };
    w.snes.push(this);
  }
  FakeSnes.prototype.start = function () { this.started = true; };
  FakeSnes.prototype.stop = function () { this.stopped = true; };
  FakeSnes.prototype.say = () => true;
  FakeSnes.prototype.attach = function () { this.state = 'attached'; this.o.onStatus(this); };
  function FakeAgent() { this.handled = []; }
  FakeAgent.prototype.onServerOpen = function () {};
  FakeAgent.prototype.onGameAttached = function () {};
  FakeAgent.prototype.onGameLost = function () {};
  FakeAgent.prototype.tick = async function () {};
  FakeAgent.prototype.handle = function (m) { this.handled.push(m); };
  function FakeSocket(url) {
    this.url = url; this.readyState = 0; this.sent = [];
    w.sockets.push(this);
    setImmediate(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen({}); } });
  }
  FakeSocket.prototype.send = function (data) { this.sent.push(JSON.parse(data)); };
  FakeSocket.prototype.close = function () {
    if (this.readyState === 3) return;
    this.readyState = 3;
    setImmediate(() => this.onclose && this.onclose({}));
  };
  global.Snes = FakeSnes;
  global.HLAgent = { Agent: FakeAgent };
  global.WebSocket = FakeSocket;
  const locks = fakeLocks();
  w.locks = locks;
  w.settle = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
  w.tab = (code, extra) => {
    const t = { states: [], agentSockets: [] };
    t.h = HLGame.link(Object.assign({
      room: { code: code || 'ABCDEFGH23', player_id: 1, player_token: 'tok' },
      wsUrl: 'ws://room.test/ws', hud: true, locks,
      onLock: s => t.states.push(s),
      onAgentSocket: open => t.agentSockets.push(open)
    }, extra || {}));
    return t;
  };
  w.hellos = () => w.sockets.filter(s => s.sent.some(m => m.type === 'hello'));
  w.open = () => w.sockets.filter(s => s.readyState === 1);
  try { await fn(w); } finally { Object.assign(global, saved); }
}

test('link: a second tab of the room waits without a bridge or a socket, and links when the first lets go', () =>
  withTabs(async (w) => {
    const a = w.tab();
    assert.strictEqual(a.h.state, 'asking');
    await w.settle();
    assert.strictEqual(a.h.state, 'linked');
    assert.strictEqual(w.snes.length, 1);
    assert.ok(a.h.snes && a.h.snes.started);
    a.h.snes.attach();
    await w.settle();
    assert.strictEqual(w.hellos().length, 1);
    assert.ok(a.h.agentOpen());

    const b = w.tab();
    await w.settle();
    assert.strictEqual(b.h.state, 'waiting');
    assert.deepStrictEqual(b.states, ['waiting']);
    assert.strictEqual(b.h.snes, null);
    assert.strictEqual(b.h.agentOpen(), false);
    assert.strictEqual(w.snes.length, 1, 'the waiting tab made no bridge');
    assert.strictEqual(w.sockets.length, 1, 'the waiting tab opened no socket');
    assert.strictEqual(w.locks.log.filter(r => r.steal).length, 0);

    const first = a.h.snes, sock = w.hellos()[0];
    await a.h.stop();                                  // link switched off, or Leave
    await w.settle();
    assert.strictEqual(a.h.state, 'stopped');
    assert.ok(first.stopped);
    assert.deepStrictEqual(sock.sent.map(m => m.type), ['hello', 'bye']);
    assert.strictEqual(b.h.state, 'linked');
    assert.deepStrictEqual(b.states, ['waiting', 'linked']);
    assert.strictEqual(w.snes.length, 2);
    b.h.snes.attach();
    await w.settle();
    assert.strictEqual(w.open().length, 1);
    assert.strictEqual(w.open()[0], w.hellos()[1]);
    assert.strictEqual(w.hellos()[1].sent[0].player_id, 1);
    await b.h.stop(true);
  }));

test('link: taking over stops the holder cleanly, and it waits instead of taking it back', () =>
  withTabs(async (w) => {
    const a = w.tab();
    await w.settle();
    a.h.snes.attach();
    const b = w.tab();
    await w.settle();
    assert.strictEqual(b.h.state, 'waiting');
    const aSnes = a.h.snes, aSock = w.hellos()[0];

    b.h.takeOver();
    b.h.takeOver();                                    // a double click steals once
    await w.settle();
    assert.strictEqual(w.locks.log.filter(r => r.steal).length, 1);
    assert.strictEqual(b.h.state, 'linked');
    assert.strictEqual(a.h.state, 'waiting');
    assert.deepStrictEqual(a.states, ['linked', 'waiting']);
    assert.ok(aSnes.stopped, 'the robbed tab let go of the bridge');
    assert.deepStrictEqual(aSock.sent.map(m => m.type), ['hello', 'bye']);
    assert.strictEqual(aSock.readyState, 3);
    assert.strictEqual(a.h.snes, null);
    assert.strictEqual(a.agentSockets[a.agentSockets.length - 1], false);
    assert.strictEqual(w.locks.waiting(HLGame.LOCK_PREFIX + 'ABCDEFGH23'), 1, 'the robbed tab is back in line');

    b.h.snes.attach();
    await w.settle();
    const made = w.snes.length, sockets = w.sockets.length;
    for (let i = 0; i < 5; i++) await w.settle();
    assert.strictEqual(a.h.state, 'waiting', 'the robbed tab did not take it back');
    assert.strictEqual(b.h.state, 'linked');
    assert.strictEqual(w.snes.length, made);
    assert.strictEqual(w.sockets.length, sockets);
    assert.strictEqual(w.locks.log.filter(r => r.steal).length, 1, 'no steal but the click');
    assert.strictEqual(w.open().length, 1);

    a.h.takeOver();                                    // a click in the first tab moves it back
    await w.settle();
    assert.strictEqual(a.h.state, 'linked');
    assert.strictEqual(b.h.state, 'waiting');
    assert.strictEqual(w.locks.log.filter(r => r.steal).length, 2);

    await a.h.stop();                                  // and the tab in line takes over
    await w.settle();
    assert.strictEqual(b.h.state, 'linked');
    await b.h.stop(true);
  }));

test('link: a tab stopped while it waits leaves the line and never links', () =>
  withTabs(async (w) => {
    const a = w.tab();
    await w.settle();
    const b = w.tab();
    await w.settle();
    assert.strictEqual(b.h.state, 'waiting');
    await b.h.stop();
    assert.strictEqual(w.locks.waiting(HLGame.LOCK_PREFIX + 'ABCDEFGH23'), 0);
    b.h.takeOver();
    await a.h.stop();
    await w.settle();
    assert.strictEqual(b.h.state, 'stopped');
    assert.strictEqual(w.snes.length, 1);
    assert.strictEqual(w.locks.holder(HLGame.LOCK_PREFIX + 'ABCDEFGH23'), null);
  }));

test('link: with no AbortController a stale place in line is let go when it comes up', () =>
  withTabs(async (w) => {
    const AC = global.AbortController;
    global.AbortController = undefined;
    try {
      const name = HLGame.LOCK_PREFIX + 'ABCDEFGH23';
      const a = w.tab();
      await w.settle();
      const b = w.tab(), c = w.tab();
      await w.settle();
      assert.strictEqual(w.locks.waiting(name), 2);
      await b.h.stop();                                // cannot leave the line: it lets go on its turn
      c.h.takeOver();                                  // nor can c's old place; the steal wins
      await w.settle();
      assert.strictEqual(c.h.state, 'linked');
      assert.strictEqual(a.h.state, 'waiting');
      await c.h.stop();
      await w.settle();
      assert.strictEqual(a.h.state, 'linked', 'b and c let their stale places go');
      assert.strictEqual(b.h.state, 'stopped');
      assert.strictEqual(w.snes.length, 3, 'a, c, then a again; never b');
      await a.h.stop(true);

      /* a stale place must not link a tab that is waiting again: a steal
         from that link would go unheard, and two tabs would link */
      const d = w.tab('ZZZZZZZZZZ');
      await w.settle();
      const e = w.tab('ZZZZZZZZZZ');
      await w.settle();
      e.h.takeOver();                                  // e's first place goes stale
      await w.settle();
      d.h.takeOver();                                  // e is robbed and queues again
      await w.settle();
      assert.strictEqual(e.h.state, 'waiting');
      await d.h.stop();
      await w.settle();
      assert.strictEqual(e.h.state, 'linked');
      const f = w.tab('ZZZZZZZZZZ');
      await w.settle();
      f.h.takeOver();
      await w.settle();
      assert.strictEqual(f.h.state, 'linked');
      assert.strictEqual(e.h.state, 'waiting', 'the robbed tab heard the steal');
      assert.strictEqual(e.h.snes, null);
      await e.h.stop(true); await f.h.stop(true);
    } finally { global.AbortController = AC; }
  }));

test('link: tabs of different rooms each link; the lock is named for the room', () =>
  withTabs(async (w) => {
    const a = w.tab('AAAAAAAAAA'), b = w.tab('BBBBBBBBBB');
    await w.settle();
    assert.strictEqual(a.h.state, 'linked');
    assert.strictEqual(b.h.state, 'linked');
    assert.deepStrictEqual(w.locks.log.map(r => r.name),
      ['hyrulelink.link.AAAAAAAAAA', 'hyrulelink.link.BBBBBBBBBB']);
    await a.h.stop(true); await b.h.stop(true);
  }));

test('link: no locks API links at once, as before', () =>
  withTabs(async (w) => {
    const a = w.tab(null, { locks: null }), b = w.tab(null, { locks: null });
    assert.strictEqual(a.h.state, 'linked');
    assert.strictEqual(b.h.state, 'linked');
    assert.deepStrictEqual(a.states, [], 'no onLock for the state link() returns with');
    assert.strictEqual(w.snes.length, 2);
    b.h.takeOver();
    assert.strictEqual(w.locks.log.length, 0);
    await a.h.stop(true); await b.h.stop(true);
    /* and a browser that refuses the request links too */
    const c = w.tab(null, { locks: { request: () => Promise.reject(new Error('SecurityError')) } });
    await w.settle();
    assert.strictEqual(c.h.state, 'linked');
    await c.h.stop(true);
  }));

test('game: nothing from the room reaches the game once a page stops linking', () =>
  withTabs(async (w) => {
    /* a robbed tab takes its HUD line down before it closes its socket:
       a grant arriving then must not be written behind the new tab's back */
    const g = HLGame.start({ room: { code: 'X', player_id: 1, player_token: 't' }, wsUrl: 'ws://room.test/ws' });
    g.snes.attach();
    await w.settle();
    const sock = w.hellos()[0];
    sock.onmessage({ data: JSON.stringify({ type: 'grant', item: 'lamp', level: 1 }) });
    assert.strictEqual(g.agent.handled.length, 1);
    const done = g.stop();
    sock.onmessage({ data: JSON.stringify({ type: 'grant', item: 'hookshot', level: 1 }) });
    assert.strictEqual(g.agent.handled.length, 1);
    await done;
    assert.deepStrictEqual(sock.sent.map(m => m.type), ['hello', 'bye']);
  }));

(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); passed++; console.log('ok   ' + name); }
    catch (e) { console.log('FAIL ' + name + '\n     ' + (e && e.message)); process.exitCode = 1; }
  }
  console.log(passed + ' / ' + tests.length + ' passed');
})();
