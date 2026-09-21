/* node tools/coop_check.js - a whole co-op round with no browser: two players,
 * two fake games, one real server, and the page's own game modules
 * (web/js/items.js, effects.js, agent.js, hud.js, snes.js) doing the linking.
 *
 *   node tools/coop_check.js --server http://127.0.0.1:5919 \
 *        --a ws://localhost:23174 --b ws://localhost:23274 [--verbose]
 *
 * --a and --b are two tools/fake_snes.py bridges; each one's control API is on
 * the next port up. --server is server/app.py. Ana opens a room with no
 * cooldown and Bo joins. Each player gets what the room page builds for a
 * linked game (Snes + Agent + an agent socket) and a ui socket for the board.
 * After both games are linked and the 29 ownership commands of each hello are
 * applied, it checks, each within 3 s:
 *
 *   1 find            Ana finds the Master Sword: the room says hers at 2,
 *                     her game keeps it, she sent exactly one pickup
 *   2 steal by find   Bo finds a Fighter Sword: his at 1, gone from Ana's
 *                     game, a line on both HUD strips
 *   3 claim back      Ana claims it on the board: hers at her own tier again
 *   4 boots           the run flag ($7EF379 bit 0x04) goes with the boots, and
 *                     the page puts it back within 2 polls when the game drops it
 *   5 bow             silver at Ana's tier, wood at Bo's; Ana's claim brings
 *                     back silver, its equip byte and 30 arrows
 *   6 save gate       a revoke that lands on the file select waits for the
 *                     save to load, then applies within 2 polls with a resync;
 *                     a byte the loaded save brings back (a Hookshot Ana
 *                     never found) is re-seeded, not reported, and the
 *                     resync takes it away
 *   7 echo cancel     every pickup a player sent is one their game made
 *
 * Prints each step and PASS (exit 0), or FAIL and why (exit 1). Exit 2 for bad
 * usage, a node without WebSocket (needs node 22+), or a bridge that is not
 * the fake. It resets both fakes and writes to their memory: it refuses to
 * link a device that is not the fake's.
 */
'use strict';
const path = require('path');

const WEB = path.join(__dirname, '..', 'web', 'js');
const HLItems = require(path.join(WEB, 'items.js'));
const HLAgent = require(path.join(WEB, 'agent.js'));
const Hud = require(path.join(WEB, 'hud.js'));
const Snes = require(path.join(WEB, 'snes.js'));   // after hud.js: it builds a Hud.HudStrip

const FAKE_DEVICE = 'emunwa://fake-alttp:48879';
const COMMANDS = HLItems.ITEMS.length;   // one grant or revoke per catalog item on every hello
const STEP_MS = 3000;                    // each assertion
const SETUP_MS = 15000;
const WATCHDOG_MS = 85000;
const CHECK_MS = 50;
const GRACE_MS = 150;                    // bridge delivery after a counted poll

const LAMP = 0xF34A, HOOKSHOT = 0xF342, BOOTS = 0xF355, ABILITY = 0xF379;
const BOW_FLAGS = 0xF38E, BOW_EQUIP = 0xF340, ARROWS = 0xF377;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hex2 = (n) => ('0' + (n >>> 0).toString(16).toUpperCase()).slice(-2);
const hex4 = (n) => ('000' + (n >>> 0).toString(16).toUpperCase()).slice(-4);

class Fail extends Error {}
class Usage extends Error {}

/* -- arguments --------------------------------------------------------------- */

function parseArgs(argv) {
  const out = { server: 'http://127.0.0.1:5919', a: 'ws://localhost:23174',
                b: 'ws://localhost:23274', verbose: false };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--verbose' || k === '-v') out.verbose = true;
    else if (k === '--server' || k === '--a' || k === '--b') {
      if (!argv[i + 1]) throw new Usage(k + ' needs a URL');
      out[k.slice(2)] = argv[++i];
    } else if (k === '--help' || k === '-h') throw new Usage('');
    else throw new Usage('unknown argument ' + k);
  }
  return out;
}

/* ws://host:N -> http://host:N+1 (the fake's control API) */
function controlUrl(bridge) {
  const u = new URL(bridge);
  if (u.protocol !== 'ws:') throw new Usage('bridge URL must be ws://host:port, not ' + bridge);
  if (!u.port) throw new Usage('bridge URL needs a port: ' + bridge);
  return 'http://' + u.hostname + ':' + (Number(u.port) + 1);
}

/* http://host:N[/prefix/] -> ws://host:N[/prefix/]ws, as the page builds it */
function socketUrl(server) {
  const u = new URL('ws', server.endsWith('/') ? server : server + '/');
  u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
  return u.toString();
}

function apiUrl(server, rel) {
  return new URL(rel, server.endsWith('/') ? server : server + '/').toString();
}

async function http(method, url, body) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  const text = await res.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) { data = null; }
  if (!res.ok) throw new Error(method + ' ' + url + ' -> ' + res.status + ' ' + text.slice(0, 200));
  return data;
}

/* -- waiting ------------------------------------------------------------------- */

/* Poll check() until it returns true; anything else is the current state, for
   the failure message. */
async function until(what, check, ms) {
  ms = ms || STEP_MS;
  const end = Date.now() + ms;
  let last = '';
  for (;;) {
    let r;
    try { r = await check(); } catch (e) { r = 'error: ' + e.message; }
    if (r === true) return;
    last = typeof r === 'string' ? r : JSON.stringify(r);
    if (Date.now() >= end) throw new Fail(what + ' - not within ' + ms + ' ms (' + last + ')');
    await sleep(CHECK_MS);
  }
}

function expect(ok, what) {
  if (!ok) throw new Fail(what);
}

/* -- a player: what the room page builds for a linked game ------------------- */

function makePlayer(label, name, bridge, opts) {
  const p = {
    label, name, bridge, ctl: controlUrl(bridge), verbose: opts.verbose,
    id: null, token: null,
    sent: [],            // every message the agent put on its socket
    recv: [],            // every message the server sent the agent
    notes: [],           // notify texts
    logs: [],
    uiRejects: [],
    events: [],
    state: null,         // latest state doc on the ui socket
    pokes: [],           // item finds made in this player's game
    ticks: 0,            // polls completed (snes loop -> agent.tick)
    linked: false, attachedOnce: false, detaches: 0,
    agentWs: null, agentOpen: false, agentClosed: false,
    ui: null, uiClosed: false,
    stopping: false
  };

  p.log = (text) => {
    p.logs.push(text);
    if (p.verbose) console.log('    [' + p.name + '] ' + text);
  };

  p.snes = new Snes({
    urls: [bridge],
    name: 'BombosSwap coop_check',
    onStatus: (snes) => onStatus(p, snes),
    onTick: async (mod) => {
      if (!p.fake) return;                  // never poll a device that is not the fake
      await p.agent.tick(mod);
      p.ticks++;
    }
  });

  p.agent = new HLAgent.Agent({
    game: p.snes,
    send: (obj) => {
      if (!p.agentOpen || !p.agentWs || p.agentWs.readyState !== 1) return false;
      p.agentWs.send(JSON.stringify(obj));
      p.sent.push(obj);
      if (p.verbose && obj.type !== 'applied') p.log('-> ' + JSON.stringify(obj));
      return true;
    },
    say: (text) => p.snes.say(text),
    onNotify: (text) => { p.notes.push(text); p.log('notify: ' + text); },
    onLog: (text) => p.log(text)
  });

  p.mark = () => p.sent.length;
  p.sentSince = (mark, type) => p.sent.slice(mark).filter((m) => m.type === type);
  p.count = (type) => p.sent.filter((m) => m.type === type).length;
  p.applied = (mark, item, action) => () => {
    const hit = p.sent.slice(mark).find((m) => m.type === 'applied' && m.item === item && m.action === action);
    if (!hit) return 'no applied ' + action + ' of ' + item + ' yet';
    return hit.ok === true || 'applied ' + action + ' of ' + item + ' failed: ' + hit.error;
  };

  /* the fake's control API */
  p.control = (method, rel) => http(method, p.ctl + rel);
  p.poke = async (key, level) => {
    await p.control('POST', '/item/' + key + '/' + level);
    if (level > 0) p.pokes.push(key + ' ' + level);
  };
  p.module = (n) => p.control('POST', '/module/' + n);
  p.mem = async (addr) => (await p.control('GET', '/mem/' + hex4(addr))).bytes[0];
  p.setMem = (addr, value) => p.control('POST', '/mem/' + hex4(addr) + '/' + hex2(value));
  p.items = async () => (await p.control('GET', '/')).items;
  p.strip = async () => (await p.control('GET', '/')).strip;

  p.claim = (item) => p.ui.send(JSON.stringify({ type: 'claim', item }));
  return p;
}

/* game.js's wiring: the agent socket opens on the first attach and stays;
   attach and detach are reported to the agent. */
function onStatus(p, snes) {
  p.log('game link: ' + snes.state + (snes.device ? ' ' + snes.device : ''));
  if (snes.state === 'attached' && !p.linked) {
    if (snes.device !== FAKE_DEVICE) {
      p.refused = snes.device;
      snes.stop();
      return;
    }
    p.fake = true;
    p.linked = true;
    const first = !p.attachedOnce;
    p.attachedOnce = true;
    p.agent.onGameAttached(first);
    if (first) openAgent(p);
  } else if (snes.state !== 'attached' && p.linked) {
    p.linked = false;
    p.detaches++;
    p.agent.onGameLost();
  }
}

function openAgent(p) {
  const ws = new WebSocket(p.wsUrl);
  p.agentWs = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'hello', role: 'agent', room: p.room, player_id: p.id, token: p.token }));
    p.agentOpen = true;
    p.agent.onServerOpen();
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    p.recv.push(msg);
    if (p.verbose && msg.type !== 'grant' && msg.type !== 'revoke') p.log('<- ' + ev.data);
    p.agent.handle(msg);
  };
  ws.onerror = () => {};
  ws.onclose = () => {
    p.agentOpen = false;
    if (!p.stopping) { p.agentClosed = true; p.log('agent socket closed'); }
  };
}

function openUi(p) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(p.wsUrl);
    p.ui = ws;
    let first = true;
    const to = setTimeout(() => reject(new Fail(p.name + "'s ui socket sent no state")), STEP_MS);
    ws.onopen = () => ws.send(JSON.stringify({ type: 'hello', role: 'ui', room: p.room, player_id: p.id, token: p.token }));
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'state') {
        p.state = msg;
        if (first) { first = false; clearTimeout(to); resolve(); }
      } else if (msg.type === 'event') {
        p.events.push(msg.text);
        if (p.verbose) console.log('    [room] ' + msg.text);
      } else if (msg.type === 'reject') {
        p.uiRejects.push(msg.reason);
        p.log('ui rejected: ' + msg.reason);
      }
    };
    ws.onerror = () => {};
    ws.onclose = () => {
      if (!p.stopping) { p.uiClosed = true; p.log('ui socket closed'); }
      if (first) { clearTimeout(to); reject(new Fail(p.name + "'s ui socket closed before a state")); }
    };
  });
}

/* -- checks ---------------------------------------------------------------------- */

function makeChecks(A, B) {
  const who = (id) => (id === A.id ? A.name : id === B.id ? B.name : id == null ? 'nobody' : '#' + id);

  const entry = (key) => (A.state && A.state.ledger ? A.state.ledger[key] : undefined);

  /* the room's ledger, as Ana's board sees it */
  const holds = (key, p, level) => () => {
    const e = entry(key);
    if (e && e.owner === p.id && (level == null || e.level === level)) return true;
    return 'ledger ' + key + ': ' + (e ? who(e.owner) + ' at ' + e.level : 'not in the ledger');
  };

  const item = (p, key, level) => async () => {
    const got = (await p.items())[key];
    return got === level || p.name + "'s game: " + key + ' ' + got;
  };

  const byte = (p, addr, mask, want) => async () => {
    const v = await p.mem(addr);
    return (v & mask) === want || p.name + "'s $7E" + hex4(addr) + ' = 0x' + hex2(v);
  };

  /* A HUD line as the strip draws it: the cleaned text centred when it fits
     in 20 cells, or a 20-cell window of it while a long line scrolls. */
  const shows = (p, text) => async () => {
    const want = Hud.clean(text, null, true);
    const strip = await p.strip();
    const ok = want.length <= Hud.WIDTH ? strip.includes(want) : want.includes(strip);
    return ok || p.name + "'s strip: '" + strip + "'";
  };

  const polls = async (p, n) => {
    const target = p.ticks + n;
    await until(p.name + "'s page polls " + n + ' times', () => p.ticks >= target || p.ticks + ' polls', STEP_MS);
  };

  /* check() must hold once n polls have completed after this call (read after
     the poke landed, so the n-th of them started after it). A short grace
     covers the bridge taking in a write the poll sent without a read-back. */
  const withinPolls = async (p, n, what, check) => {
    const mark = p.ticks;
    const end = Date.now() + STEP_MS;
    let last = '';
    for (;;) {
      const seen = p.ticks - mark;
      const r = await check();
      if (r === true) return seen;
      last = r;
      if (seen >= n) {
        await sleep(GRACE_MS);
        const again = await check();
        if (again === true) return seen;
        throw new Fail(what + ' - not after ' + n + ' polls (' + again + ')');
      }
      if (Date.now() >= end) throw new Fail(what + ' - not within ' + STEP_MS + ' ms, ' + seen + ' polls (' + last + ')');
      await sleep(CHECK_MS);
    }
  };

  return { who, entry, holds, item, byte, shows, polls, withinPolls };
}

/* -- the round ----------------------------------------------------------------------- */

async function run(args, A, B) {
  const C = makeChecks(A, B);
  const wsUrl = socketUrl(args.server);
  const t0 = Date.now();

  async function step(label, title, fn) {
    const start = Date.now();
    process.stdout.write(label + ' ' + title + ' ... ');
    const note = await fn();
    console.log('ok (' + (Date.now() - start) + ' ms)' + (note ? ' - ' + note : ''));
  }

  const health = await http('GET', apiUrl(args.server, 'api/health'));
  console.log('coop_check: server ' + args.server + ' (version ' + health.version + '), games '
    + A.bridge + ' and ' + B.bridge);

  await step('setup', 'both fakes reset into a loaded save, a room, two linked games', async () => {
    for (const p of [A, B]) {
      let doc;
      try { doc = await p.control('GET', '/'); } catch (e) { doc = null; }
      if (!doc || typeof doc.strip !== 'string' || !doc.items) {
        throw new Usage(p.bridge + ' has no fake control API at ' + p.ctl
          + ' - this harness only runs against tools/fake_snes.py');
      }
      await p.control('POST', '/reset');
      await p.module(7);
    }

    const room = await http('POST', apiUrl(args.server, 'api/rooms'), { display_name: A.name, cooldown_s: 0 });
    const seat = await http('POST', apiUrl(args.server, 'api/rooms/' + room.code + '/join'), { display_name: B.name });
    A.id = room.player_id; A.token = room.player_token;
    B.id = seat.player_id; B.token = seat.player_token;
    for (const p of [A, B]) { p.room = room.code; p.wsUrl = wsUrl; }

    await Promise.all([openUi(A), openUi(B)]);
    /* The rules that make a steal-back immediate: no item cooldown, no
       steal-back lock, no budget, no tenure; claiming needs a find of your own. */
    const s = A.state, R = s.rules || {};
    expect(s.mode === 'normal' && s.claiming === true && s.cooldown_s === 0
      && !R.steal_back_lock_s && !R.steal_budget_per_min && !R.tenure_lock_s
      && R.require_found_to_claim === true && R.cooldown_scope === 'item',
      'room rules are not the ones this round assumes: mode ' + s.mode + ', cooldown ' + s.cooldown_s
      + ', rules ' + JSON.stringify(R));

    A.snes.start();
    B.snes.start();
    for (const p of [A, B]) {
      await until(p.name + "'s game linked", () => {
        if (p.refused) throw new Usage(p.bridge + ' offers ' + p.refused + ', not the fake (' + FAKE_DEVICE + ')');
        return p.linked || p.snes.state;
      }, SETUP_MS);
    }
    for (const p of [A, B]) {
      await until(p.name + "'s agent socket open and the hello's " + COMMANDS + ' commands applied', () => {
        const got = p.recv.filter((m) => m.type === 'grant' || m.type === 'revoke').length;
        const done = p.sent.filter((m) => m.type === 'applied' && m.ok === true).length;
        return (p.agentOpen && got >= COMMANDS && done >= COMMANDS)
          || 'socket ' + (p.agentOpen ? 'open' : 'closed') + ', ' + got + ' received, ' + done + ' applied';
      }, SETUP_MS);
      const bad = p.sent.filter((m) => m.type === 'applied' && m.ok !== true);
      expect(!bad.length, p.name + ': ' + bad.length + ' commands failed: ' + JSON.stringify(bad.slice(0, 3)));
    }
    await until('both players show a linked game on the board', () => {
      const ps = (A.state && A.state.players) || [];
      const up = (p) => ps.some((x) => x.id === p.id && x.agent && x.emu);
      return (up(A) && up(B)) || JSON.stringify(ps.map((x) => [x.name, x.agent, x.emu]));
    });
    /* snes.js resets the HUD on the first playable poll: be past it. */
    await C.polls(A, 2);
    await C.polls(B, 2);
    return 'room ' + A.room + ', ' + A.name + ' #' + A.id + ' and ' + B.name + ' #' + B.id
      + ', ' + COMMANDS + ' commands applied on each';
  });

  const sword = HLItems.BY_KEY.sword.name;

  await step('step 1', 'find: Ana finds the Master Sword', async () => {
    const m = A.mark();
    await A.poke('sword', 2);
    await until('ledger: Sword held by Ana at 2', C.holds('sword', A, 2));
    await until('Ana applied the grant for her find', A.applied(m, 'sword', 'grant'));
    await C.polls(A, 2);
    await until("Ana's game keeps the Master Sword", C.item(A, 'sword', 2), 1);
    const ups = A.sentSince(m, 'pickup');
    expect(ups.length === 1 && ups[0].item === 'sword' && ups[0].level === 2,
      'Ana sent ' + JSON.stringify(ups) + '; want exactly one pickup of sword at 2');
    return 'Ana sent 1 pickup';
  });

  await step('step 2', 'steal by find: Bo finds a Fighter Sword', async () => {
    await B.poke('sword', 1);
    await until('ledger: Sword held by Bo at 1', C.holds('sword', B, 1));
    await until("Ana's sword taken out of her game", C.item(A, 'sword', 0));
    await until("Bo's game keeps the Fighter Sword", C.item(B, 'sword', 1));
    /* resolve_pickup's lines: "{item} sent to {finder}" to the loser,
       "{item} taken from {loser}" to the finder */
    const toA = sword + ' sent to ' + B.name, toB = sword + ' taken from ' + A.name;
    await Promise.all([
      until("Ana's HUD strip shows '" + Hud.clean(toA, null, true) + "'", C.shows(A, toA)),
      until("Bo's HUD strip shows '" + Hud.clean(toB, null, true) + "'", C.shows(B, toB))
    ]);
    return "strips '" + Hud.clean(toA, null, true) + "' / '" + Hud.clean(toB, null, true) + "'";
  });

  await step('step 3', 'claim back: Ana claims the sword on the board', async () => {
    const rejects = A.uiRejects.length;
    A.claim('sword');
    await until('ledger: Sword held by Ana at 2 (her own tier)', () => {
      if (A.uiRejects.length > rejects) throw new Fail('claim rejected: ' + A.uiRejects[A.uiRejects.length - 1]);
      return C.holds('sword', A, 2)();
    });
    await until("Ana's game has the Master Sword again", C.item(A, 'sword', 2));
    await until("Bo's sword taken out of his game", C.item(B, 'sword', 0));
  });

  await step('step 4', 'boots: the run flag goes with the boots', async () => {
    const mA = A.mark(), mB = B.mark();
    await A.poke('boots', 1);
    await until('ledger: boots held by Ana', C.holds('boots', A));
    await until('Ana applied her boots', A.applied(mA, 'boots', 'grant'));
    await B.poke('boots', 1);
    await until('ledger: boots held by Bo', C.holds('boots', B));
    await until('Ana applied the revoke of her boots', A.applied(mA, 'boots', 'revoke'));
    await until("Ana's boots gone ($7EF355 = 0)", C.byte(A, BOOTS, 0xFF, 0));
    await until("Ana's run flag clear ($7EF379 & 0x04 = 0)", C.byte(A, ABILITY, 0x04, 0));
    await until('Bo applied his boots', B.applied(mB, 'boots', 'grant'));
    await until("Bo's run flag set ($7EF379 & 0x04 = 0x04)", C.byte(B, ABILITY, 0x04, 0x04));
    /* the game drops the flag (a transition does); the page puts it back */
    await B.setMem(ABILITY, 0x00);
    const n = await C.withinPolls(B, 2, "Bo's run flag put back by his page", C.byte(B, ABILITY, 0x04, 0x04));
    return 'run flag back after ' + n + ' poll' + (n === 1 ? '' : 's');
  });

  await step('step 5', "bow: silver at Ana's tier, wood at Bo's", async () => {
    const mA = A.mark();
    await A.poke('bow', 2);
    await until('ledger: Bow held by Ana at 2', C.holds('bow', A, 2));
    await until('Ana applied her silver bow', A.applied(mA, 'bow', 'grant'));
    await B.poke('bow', 1);
    await until('ledger: Bow held by Bo at 1', C.holds('bow', B, 1));
    await until("Ana's bow gone ($7EF38E & 0xC0 = 0)", C.byte(A, BOW_FLAGS, 0xC0, 0x00));
    await until("Bo's wood bow ($7EF38E & 0xC0 = 0x80)", C.byte(B, BOW_FLAGS, 0xC0, 0x80));
    await until("Bo's bow equip is wood ($7EF340 = 1)", C.byte(B, BOW_EQUIP, 0xFF, 1));
    /* Ana has shot every arrow: the silver grant must put 30 back */
    await A.setMem(ARROWS, 0);
    const rejects = A.uiRejects.length;
    A.claim('bow');
    await until('ledger: Bow held by Ana at 2', () => {
      if (A.uiRejects.length > rejects) throw new Fail('claim rejected: ' + A.uiRejects[A.uiRejects.length - 1]);
      return C.holds('bow', A, 2)();
    });
    await until("Ana's silver bow ($7EF38E & 0xC0 = 0xC0)", C.byte(A, BOW_FLAGS, 0xC0, 0xC0));
    await until("Ana's bow equip is silver ($7EF340 = 4)", C.byte(A, BOW_EQUIP, 0xFF, 4));
    await until("Ana's arrows ($7EF377 = 30)", C.byte(A, ARROWS, 0xFF, 30));
    await until("Bo's bow gone ($7EF38E & 0xC0 = 0)", C.byte(B, BOW_FLAGS, 0xC0, 0x00));
  });

  await step('step 6', 'save gate: a revoke waits out the file select', async () => {
    const m = A.mark();
    await A.poke('lamp', 1);
    await until('ledger: Lamp held by Ana', C.holds('lamp', A));
    await until('Ana applied her lamp', A.applied(m, 'lamp', 'grant'));
    await A.module(1);                                         // save and quit: file select
    await until("Ana's page saw the file select", () => A.agent.outOfGame || 'module ' + A.snes.module);
    await B.poke('lamp', 1);
    await until('ledger: Lamp held by Bo', C.holds('lamp', B));
    await until("Ana's revoke of the lamp deferred", () =>
      A.agent.pending.has('lamp') || 'pending: ' + JSON.stringify(Array.from(A.agent.pending.keys())));
    /* Loading the file puts back what the save holds. The fake keeps WRAM
       over a load, so do it by hand: this save has a Hookshot Ana never
       found. The page must re-seed it, not report it. */
    await A.setMem(HOOKSHOT, 0x01);
    await C.polls(A, 2);
    expect((await A.items()).lamp === 1, "Ana's lamp was written while no save was loaded");
    expect(A.agent.pending.has('lamp'), "Ana's deferred revoke of the lamp went missing");

    const mark = A.mark();
    await A.module(7);                                         // the save is loaded again
    const n = await C.withinPolls(A, 2, "Ana's lamp revoked once her save is loaded", C.item(A, 'lamp', 0));
    /* The first playable poll asks for a resync and then applies the deferred
       revoke, before the server's re-push (which would revoke the lamp too). */
    const after = A.sent.slice(mark).filter((x) => x.type === 'resync' || x.type === 'applied');
    expect(after.length && after[0].type === 'resync', 'Ana sent no resync after the file select');
    expect(after.length > 1 && after[1].item === 'lamp' && after[1].action === 'revoke' && after[1].ok === true,
      "Ana's deferred revoke was not applied on the first poll of the loaded save (next: "
      + JSON.stringify(after[1] || null) + ')');
    expect(!A.agent.pending.size, 'Ana still has deferred commands: ' + JSON.stringify(Array.from(A.agent.pending.keys())));
    await until("the resync's " + COMMANDS + ' commands applied on Ana', () => {
      const done = A.sentSince(mark, 'applied').length;
      return done >= COMMANDS + 1 || done + ' applied';            // + the deferred revoke
    });
    await C.polls(A, 2);
    const ups = A.sentSince(mark, 'pickup');
    expect(!ups.length, 'Ana reported ' + JSON.stringify(ups) + ' after her save loaded; want none');
    await until("the save's Hookshot taken back by the resync", C.item(A, 'hookshot', 0));
    const hk = C.entry('hookshot');
    expect(!hk || hk.owner == null, 'ledger: Hookshot went to ' + (hk && C.who(hk.owner)));
    expect(C.holds('lamp', B)() === true, 'ledger: Lamp left Bo');
    return 'lamp 0 after ' + n + ' poll' + (n === 1 ? '' : 's') + ', resync sent, no pickup';
  });

  await step('step 7', 'echo cancel: every pickup sent is a find the game made', async () => {
    await Promise.all([C.polls(A, 2), C.polls(B, 2)]);
    for (const p of [A, B]) {
      const ups = p.sent.filter((m) => m.type === 'pickup').map((m) => m.item + ' ' + m.level);
      expect(ups.length === p.pokes.length,
        p.name + ' sent ' + ups.length + ' pickups (' + ups.join(', ') + ') for ' + p.pokes.length
        + ' finds (' + p.pokes.join(', ') + ')');
      expect(!p.agent.outbox.length, p.name + ' has pickups the server never got: ' + JSON.stringify(p.agent.outbox));
      const bad = p.sent.filter((m) => m.type === 'applied' && m.ok !== true);
      expect(!bad.length, p.name + ': failed writes ' + JSON.stringify(bad.slice(0, 3)));
      const rej = p.recv.filter((m) => m.type === 'reject');
      expect(!rej.length && !p.uiRejects.length,
        p.name + ': server rejected ' + JSON.stringify(rej.map((m) => m.reason).concat(p.uiRejects)));
      expect(!p.agentClosed && !p.uiClosed, p.name + "'s sockets dropped during the round");
    }
    return A.name + ' ' + A.pokes.length + '/' + A.count('pickup') + ', ' + B.name + ' '
      + B.pokes.length + '/' + B.count('pickup') + ' finds/pickups';
  });

  for (const p of [A, B]) {
    if (p.detaches) console.log('note: ' + p.name + "'s game link dropped " + p.detaches + ' time(s) and came back');
  }
  console.log('PASS (' + ((Date.now() - t0) / 1000).toFixed(1) + ' s)');
}

function dump(A, B) {
  for (const p of [A, B]) {
    if (!p) continue;
    console.log('--- ' + p.name + ': game ' + p.snes.state + ', module '
      + (p.snes.module == null ? '?' : '0x' + hex2(p.snes.module)) + ', ' + p.ticks + ' polls, sent '
      + p.sent.length + ', received ' + p.recv.length);
    for (const line of p.logs.slice(-25)) console.log('    ' + line);
  }
  const ledger = A && A.state ? A.state.ledger : null;
  if (ledger) {
    console.log('--- ledger: ' + Object.keys(ledger).map((k) =>
      k + '=' + (ledger[k].owner == null ? '-' : ledger[k].owner_name + '@' + ledger[k].level)).join(' '));
  }
}

function shutdown(players) {
  for (const p of players) {
    if (!p) continue;
    p.stopping = true;
    try { p.snes.stop(); } catch (e) { /* gone */ }
    try {
      if (p.agentWs && p.agentWs.readyState === 1) p.agentWs.send(JSON.stringify({ type: 'bye' }));
      if (p.agentWs) p.agentWs.close();
    } catch (e) { /* gone */ }
    try { if (p.ui) p.ui.close(); } catch (e) { /* gone */ }
  }
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (e) {
    console.log((e.message ? e.message + '\n' : '')
      + 'usage: node tools/coop_check.js --server http://127.0.0.1:5919 --a ws://localhost:23174 --b ws://localhost:23274 [--verbose]');
    return 2;
  }
  if (typeof WebSocket !== 'function' || typeof fetch !== 'function') {
    console.log('coop_check needs node 22 or newer (global WebSocket and fetch); this is node ' + process.version);
    return 2;
  }
  let A = null, B = null;
  try {
    A = makePlayer('A', 'Ana', args.a, args);
    B = makePlayer('B', 'Bo', args.b, args);
    await run(args, A, B);
    return 0;
  } catch (e) {
    console.log('');
    if (e instanceof Usage) {
      console.log('cannot run: ' + e.message);
      return 2;
    }
    console.log('FAIL: ' + (e instanceof Fail ? e.message : (e && e.stack) || e));
    dump(A, B);
    return 1;
  } finally {
    shutdown([A, B]);
  }
}

/* Not unref'd: if the round ever stalls with nothing else pending, this is
   what ends it, with a FAIL rather than a silent exit 0. */
setTimeout(() => {
  console.log('\nFAIL: the round did not finish within ' + WATCHDOG_MS / 1000 + ' s');
  process.exit(1);
}, WATCHDOG_MS);

main().then((code) => {
  /* snes.js keeps retry timers; the round is over, so leave now */
  setTimeout(() => process.exit(code), 100);
});
