/* HyruleLink - grants and revokes as WRAM writes.
 *
 * A port of agent/effects.py and agent/sni/item_effects.py (ItemManager),
 * write for write and read for read: the same bytes go to the same
 * addresses in the same order, so what is proven against a live game there
 * holds here. tests/test_items_js.py replays every item through both and
 * compares the traces.
 *
 *   progressive   the byte = the exact level (never +1)
 *   simple        the byte = `give` (Magic Mirror is 2; 1 is a broken icon)
 *   boots         $7EF355 and bit 0x04 of $7EF379 (the run flag); revoke
 *                 clears both
 *   bow           $7EF38E bits 0x80 has / 0x40 silver are the truth; $7EF340
 *                 is 1 wood / 4 silver / 0 none for the HUD; silver puts 30
 *                 arrows in $7EF377 when it reads 0
 *   shared slots  ownership bits in $7EF38C (blue 0x80, red 0x40, mushroom
 *                 0x28 set / 0x20 own, powder 0x10, shovel 0x04, flute 0x01
 *                 active / 0x03 any); the slot bytes $7EF341, $7EF344,
 *                 $7EF34C are enums kept in step, falling back to the other
 *                 half on revoke. Never OR an enum.
 *
 * Every read inside a read-modify-write retries 6 times and never falls
 * back to 0: a 0 would clobber the other bits of $7EF379, $7EF38C, $7EF38E.
 * enable/disable read the item's byte back and throw when it is wrong, so
 * the agent can retry and report.
 *
 * transport = {read(addr, len) -> Promise<Uint8Array|null>,
 *              write(addr, Uint8Array) -> Promise<bool>}
 * No DOM, no socket: node runs it against a fake WRAM.
 */
(function (root) {
  'use strict';

  var I = root.HLItems || (typeof require === 'function' ? require('./items.js') : null);

  var BOOM_BLUE_BIT = 0x80;
  var BOOM_RED_BIT = 0x40;
  var MUSHROOM_BITS = 0x28;       // receive ORs 0x20|0x08; ownership/menu gate is 0x20
  var MUSHROOM_OWN = 0x20;
  var POWDER_BIT = 0x10;
  var SHOVEL_BIT = 0x04;
  var FLUTE_ACTIVE_BIT = 0x01;
  var FLUTE_INACTIVE_BIT = 0x02;
  var FLUTE_ANY_BITS = 0x03;

  var BOOM_EQUIP_ADDR = 0xF341;   // enum: 1 blue, 2 red
  var POWDER_EQUIP_ADDR = 0xF344; // enum: 1 mushroom, 2 powder
  var FLUTE_EQUIP_ADDR = 0xF34C;  // enum: 1 shovel, 2 inactive flute, 3 active flute

  var BOOTS_ADDR = 0xF355;
  var ABILITY_ADDR = 0xF379;
  var RUN_ABILITY_MASK = 0x04;

  var ARROWS_ADDR = 0xF377;
  var DEFAULT_ARROWS = 30;

  var BOW_FLAGS_ADDR = I.BOW_FLAGS_ADDR;
  var BOW_HAS_MASK = I.BOW_HAS_MASK;
  var BOW_SILVER_MASK = I.BOW_SILVER_MASK;
  var BOW_EQUIP_ADDR = I.BOW_EQUIP_ADDR;
  var INV_TRACK_ADDR = I.INV_TRACK_ADDR;

  var has = Object.prototype.hasOwnProperty;

  function hex4(n) { return ('0000' + n.toString(16).toUpperCase()).slice(-4); }
  function one(v) { return new Uint8Array([v & 0xFF]); }

  function itemFor(key) {
    if (!has.call(I.BY_KEY, key)) throw new Error('unknown item ' + key);
    return I.BY_KEY[key];
  }

  /* item_effects.ItemManager.SPECIAL / SPECIAL_REMOVE */
  var SPECIAL = {
    boomerang: ['_addBoomerang', { blue: true }],
    blue_boomerang: ['_addBoomerang', { blue: true }],
    red_boomerang: ['_addBoomerang', { red: true }],
    mushroom: ['_addMushroomPowder', { mushroom: true }],
    powder: ['_addMushroomPowder', { powder: true }],
    shovel: ['_addShovelFlute', { shovel: true }],
    flute: ['_addShovelFlute', { flute: true }]
  };
  var SPECIAL_REMOVE = {
    boomerang: ['_removeBoomerang', { blue: true }],
    blue_boomerang: ['_removeBoomerang', { blue: true }],
    red_boomerang: ['_removeBoomerang', { red: true }],
    mushroom: ['_removeMushroomPowder', { mushroom: true }],
    powder: ['_removeMushroomPowder', { powder: true }],
    shovel: ['_removeShovelFlute', { shovel: true }],
    flute: ['_removeShovelFlute', { flute: true }]
  };

  function Effects(transport) {
    this.t = transport;
  }

  /* One byte, retrying transient drops (NWA drops the reply to a read right
     after a write). Throws when it stays unreadable: NEVER falls back to 0. */
  Effects.prototype._read = async function (addr, tries) {
    if (tries == null) tries = 6;
    for (var i = 0; i < tries; i++) {
      var d = await this.t.read(addr, 1);
      if (d && d.length >= 1) return d[0];
    }
    throw new Error('could not read WRAM $' + hex4(addr));
  };

  Effects.prototype._write = function (addr, value) {
    return this.t.write(addr, one(value));
  };

  function matches(item, raw, level, enable) {
    if (item.kind === 'bitfield') return !!(raw & item.mask) === enable;
    if (item.kind === 'bow') {
      var hasBow = !!(raw & BOW_HAS_MASK);
      if (!enable) return !hasBow;
      return hasBow && (!!(raw & BOW_SILVER_MASK) === (level >= 2));
    }
    var expected = enable ? (item.kind === 'progressive' ? level : item.give) : 0;
    return raw === expected;
  }
  Effects._matches = matches;
  Effects.prototype._matches = matches;

  /* Grant `key` at `level`. Resolves to the raw byte at item.addr afterwards. */
  Effects.prototype.enable = async function (key, level) {
    var item = itemFor(key);
    if (item.kind === 'bow') await this.setBow(level);
    else if (item.kind === 'progressive') await this._write(item.addr, level);
    else if (item.kind === 'boots') await this._addBoots();
    else if (item.kind === 'bitfield') await this.add(item.effect_key);
    else await this.add(key);
    var raw = await this._read(item.addr);
    if (!matches(item, raw, level, true)) {
      throw new Error('grant verification failed for ' + key + ': read ' + raw);
    }
    return raw;
  };

  /* Take `key` away. Resolves to the raw byte at item.addr afterwards. */
  Effects.prototype.disable = async function (key) {
    var item = itemFor(key);
    if (item.kind === 'bow') await this.setBow(0);
    else if (item.kind === 'progressive') await this._write(item.addr, 0);
    else if (item.kind === 'boots') await this._removeBoots();
    else if (item.kind === 'bitfield') await this.remove(item.effect_key);
    else await this.remove(key);
    var raw = await this._read(item.addr);
    if (!matches(item, raw, 0, false)) {
      throw new Error('revoke verification failed for ' + key + ': read ' + raw);
    }
    return raw;
  };

  /* ItemManager.add: boots, bow, shared slots, else a flat write of `give`. */
  Effects.prototype.add = async function (key) {
    if (key === 'boots') return this._addBoots();
    if (key === 'bow' || key === 'silver_arrows') { await this.setBow(2); return true; }
    if (has.call(SPECIAL, key)) return this[SPECIAL[key][0]](SPECIAL[key][1]);
    if (has.call(I.BY_KEY, key) && I.BY_KEY[key].kind === 'simple') {
      return this._write(I.BY_KEY[key].addr, I.BY_KEY[key].give);
    }
    return false;
  };

  /* ItemManager.remove: the other half of a shared slot is kept. */
  Effects.prototype.remove = async function (key) {
    if (key === 'boots') return this._removeBoots();
    if (key === 'bow' || key === 'silver_arrows') { await this.setBow(0); return true; }
    if (has.call(SPECIAL_REMOVE, key)) return this[SPECIAL_REMOVE[key][0]](SPECIAL_REMOVE[key][1]);
    if (has.call(I.BY_KEY, key) && I.BY_KEY[key].kind === 'simple') {
      return this._write(I.BY_KEY[key].addr, 0);
    }
    return false;
  };

  Effects.prototype._trackSet = async function (bits) {
    await this._write(INV_TRACK_ADDR, (await this._read(INV_TRACK_ADDR)) | bits);
  };

  Effects.prototype._trackClear = async function (bits) {
    await this._write(INV_TRACK_ADDR, (await this._read(INV_TRACK_ADDR)) & ~bits & 0xFF);
  };

  /* Shared slots: granting sets the ownership bit and equips the item;
     revoking clears the bit and, if that item was equipped, falls back to
     the other one still owned (or empties the slot). */
  Effects.prototype._addBoomerang = async function (o) {
    if (o.blue) {
      await this._trackSet(BOOM_BLUE_BIT);
      await this._write(BOOM_EQUIP_ADDR, 0x01);
    }
    if (o.red) {
      await this._trackSet(BOOM_RED_BIT);
      await this._write(BOOM_EQUIP_ADDR, 0x02);
    }
    return true;
  };

  Effects.prototype._removeBoomerang = async function (o) {
    await this._trackClear((o.blue ? BOOM_BLUE_BIT : 0) | (o.red ? BOOM_RED_BIT : 0));
    var track = await this._read(INV_TRACK_ADDR);
    var equip = await this._read(BOOM_EQUIP_ADDR);
    if (o.blue && equip === 0x01) await this._write(BOOM_EQUIP_ADDR, track & BOOM_RED_BIT ? 0x02 : 0x00);
    if (o.red && equip === 0x02) await this._write(BOOM_EQUIP_ADDR, track & BOOM_BLUE_BIT ? 0x01 : 0x00);
    return true;
  };

  Effects.prototype._addMushroomPowder = async function (o) {
    if (o.mushroom) {
      await this._trackSet(MUSHROOM_BITS);
      await this._write(POWDER_EQUIP_ADDR, 0x01);
    }
    if (o.powder) {
      await this._trackSet(POWDER_BIT);
      await this._write(POWDER_EQUIP_ADDR, 0x02);
    }
    return true;
  };

  Effects.prototype._removeMushroomPowder = async function (o) {
    await this._trackClear((o.mushroom ? MUSHROOM_BITS : 0) | (o.powder ? POWDER_BIT : 0));
    var track = await this._read(INV_TRACK_ADDR);
    var equip = await this._read(POWDER_EQUIP_ADDR);
    if (o.mushroom && equip === 0x01) await this._write(POWDER_EQUIP_ADDR, track & POWDER_BIT ? 0x02 : 0x00);
    if (o.powder && equip === 0x02) await this._write(POWDER_EQUIP_ADDR, track & MUSHROOM_OWN ? 0x01 : 0x00);
    return true;
  };

  Effects.prototype._addShovelFlute = async function (o) {
    if (o.shovel) {
      await this._trackSet(SHOVEL_BIT);
      await this._write(FLUTE_EQUIP_ADDR, 0x01);
    }
    if (o.flute) {
      await this._trackSet(FLUTE_ACTIVE_BIT);          // the working (active) flute
      await this._write(FLUTE_EQUIP_ADDR, 0x03);
    }
    return true;
  };

  Effects.prototype._removeShovelFlute = async function (o) {
    await this._trackClear((o.shovel ? SHOVEL_BIT : 0) | (o.flute ? FLUTE_ANY_BITS : 0));
    var track = await this._read(INV_TRACK_ADDR);
    var equip = await this._read(FLUTE_EQUIP_ADDR);
    if (o.shovel && equip === 0x01) {
      if (track & FLUTE_ACTIVE_BIT) await this._write(FLUTE_EQUIP_ADDR, 0x03);
      else if (track & FLUTE_INACTIVE_BIT) await this._write(FLUTE_EQUIP_ADDR, 0x02);
      else await this._write(FLUTE_EQUIP_ADDR, 0x00);
    }
    if (o.flute && (equip === 0x02 || equip === 0x03)) {
      await this._write(FLUTE_EQUIP_ADDR, track & SHOVEL_BIT ? 0x01 : 0x00);
    }
    return true;
  };

  /* Boots: the item byte, then the run flag (read-modify-write). */
  Effects.prototype._addBoots = async function () {
    await this._write(BOOTS_ADDR, 0x01);
    var ability = await this._read(ABILITY_ADDR);
    return this._write(ABILITY_ADDR, ability | RUN_ABILITY_MASK);
  };

  Effects.prototype._removeBoots = async function () {
    await this._write(BOOTS_ADDR, 0x00);
    var ability = await this._read(ABILITY_ADDR);
    return this._write(ABILITY_ADDR, ability & ~RUN_ABILITY_MASK & 0xFF);
  };

  /* None (0), wood (1) or silver (2). BowTracking is the truth; silver
     arrows fire only with 0x80 and 0x40 both set. Resolves to BowTracking. */
  Effects.prototype.setBow = async function (level) {
    var flags = await this._read(BOW_FLAGS_ADDR);
    var equip;
    if (level <= 0) {
      flags &= ~(BOW_HAS_MASK | BOW_SILVER_MASK);
      equip = 0x00;
    } else {
      flags |= BOW_HAS_MASK;
      if (level >= 2) {
        flags |= BOW_SILVER_MASK;
        equip = 0x04;             // silver bow + arrows
      } else {
        flags &= ~BOW_SILVER_MASK;
        equip = 0x01;             // wood bow
      }
    }
    flags &= 0xFF;
    await this._write(BOW_FLAGS_ADDR, flags);
    await this._write(BOW_EQUIP_ADDR, equip);
    if (level >= 2 && (await this._read(ARROWS_ADDR)) === 0) {
      await this._write(ARROWS_ADDR, DEFAULT_ARROWS);
    }
    return this._read(BOW_FLAGS_ADDR);
  };

  var HLEffects = {
    Effects: Effects,
    BOOTS_ADDR: BOOTS_ADDR, ABILITY_ADDR: ABILITY_ADDR, RUN_ABILITY_MASK: RUN_ABILITY_MASK,
    ARROWS_ADDR: ARROWS_ADDR, DEFAULT_ARROWS: DEFAULT_ARROWS,
    BOOM_EQUIP_ADDR: BOOM_EQUIP_ADDR, POWDER_EQUIP_ADDR: POWDER_EQUIP_ADDR, FLUTE_EQUIP_ADDR: FLUTE_EQUIP_ADDR,
    BOOM_BLUE_BIT: BOOM_BLUE_BIT, BOOM_RED_BIT: BOOM_RED_BIT, MUSHROOM_BITS: MUSHROOM_BITS,
    MUSHROOM_OWN: MUSHROOM_OWN, POWDER_BIT: POWDER_BIT, SHOVEL_BIT: SHOVEL_BIT,
    FLUTE_ACTIVE_BIT: FLUTE_ACTIVE_BIT, FLUTE_INACTIVE_BIT: FLUTE_INACTIVE_BIT, FLUTE_ANY_BITS: FLUTE_ANY_BITS
  };
  root.HLEffects = HLEffects;
  if (typeof module === 'object' && module.exports) module.exports = HLEffects;
})(typeof window !== 'undefined' ? window : globalThis);
