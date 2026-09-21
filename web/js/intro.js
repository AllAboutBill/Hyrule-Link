/* BombosSwap - the Bombos medallion, once per open.
 *
 * What Bombos does to a screen: Link raises the medallion, fire erupts in a
 * ring of pillars around him, the ring winds outward, and bursts of flame go
 * off across the whole screen until everything on it has been caught. Here:
 * the medallion beside the name is raised, a spiral of pixel fire columns
 * winds out from it, bursts go off across the page as the fire front passes,
 * and each panel the front reaches catches an ember glow. Then it dies down
 * to sparks and is gone.
 *
 *     0 ms   the medallion is held up: 1.5x, so 3 screen px a sprite pixel
 *   140 ms   fire columns erupt round it, one after another, winding out in
 *            a spiral squashed to the ground plane (2.4 turns in 560 ms);
 *            the first turn is low so the medallion stays in sight
 *   470 ms   bursts go off across the page, each when the front reaches it;
 *            the far corner is reached at about 1180 ms
 *            each panel, card and heading flares as the front passes
 *  1450 ms   the canvas fades in three steps; the last sparks rise
 *  1750 ms   gone: canvas removed, every class taken off again
 *
 * The fire is pixel art, drawn on one canvas at a third of the page's
 * resolution and scaled up pixelated, so a fire pixel is 3 screen px: the
 * raised medallion's own pixel size. The sprites are made here, once, from
 * outlines: each pixel is shaded by how deep inside the outline it is, so
 * the bands run red, ember, gold, white-hot like the game's own flames. The
 * colours are the --fire-* tokens in the Bombos block of hyrulelink.css.
 *
 * Rules it keeps:
 *   - ONCE per open. sessionStorage, so going from the front page into a room
 *     in the same tab does not replay it; closing the tab (or the browser)
 *     and coming back does. No storage, no show - never "every page load".
 *   - Not for anyone who asked for reduced motion.
 *   - index.html and room.html only; never operator.html.
 *   - Not on a front page that is only forwarding an invite or a watch link
 *     (home.js sends ?room= and ?watch= straight on to room.html).
 *   - 1.75 s, pointer-events none, nothing waits for it. The room connects
 *     and the game links underneath it as usual, and every element and class
 *     it added is removed when it ends.
 */
(function () {
  'use strict';

  var KEY = 'hyrulelink.intro';
  var body = document.body;
  var onRoom = /room\.html$/.test(location.pathname) || !!(body && body.classList.contains('room'));
  /* An invite that lands on the front page is forwarded to the room at once
     (home.js). Do not spend the one showing on a page nobody will see. */
  if (!onRoom && typeof URLSearchParams === 'function') {
    var q = new URLSearchParams(location.search);
    if ((q.get('room') || '').trim() || (q.get('watch') || '').trim()) return;
  }
  try {
    if (sessionStorage.getItem(KEY)) return;
    sessionStorage.setItem(KEY, '1');
  } catch (e) { return; }
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var PX = 3;            // CSS px per fire pixel: the raised medallion's
  var END = 1750;        // ms; the canvas fades out over the last 300 (the CSS)

  /* ---------------------------------------------------------- the sprites */

  /* Each inside pixel's distance from the outside, 4-neighbour: 1 is the
     outline. That depth, capped, is the colour band. */
  function depth(mask, w, h) {
    var d = [], q = [], i, k, x, y;
    for (i = 0; i < w * h; i++) {
      x = i % w; y = (i / w) | 0;
      if (!mask[i]) d[i] = 0;
      else if (!x || !y || x === w - 1 || y === h - 1 || !mask[i - 1] || !mask[i + 1] || !mask[i - w] || !mask[i + w]) {
        d[i] = 1; q.push(i);
      } else d[i] = 99;
    }
    for (k = 0; k < q.length; k++) {
      i = q[k]; x = i % w; y = (i / w) | 0;
      [x > 0 ? i - 1 : -1, x < w - 1 ? i + 1 : -1, y > 0 ? i - w : -1, y < h - 1 ? i + w : -1].forEach(function (j) {
        if (j >= 0 && d[j] > d[i] + 1) { d[j] = d[i] + 1; q.push(j); }
      });
    }
    return d;
  }

  /* bands[depth] is the band (1 red .. 4 white-hot), no hotter than cap */
  function paint(mask, w, h, bands, cap, pal) {
    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    var g = c.getContext('2d'), d = depth(mask, w, h);
    for (var i = 0; i < w * h; i++) {
      if (!d[i]) continue;
      g.fillStyle = pal[Math.min(bands[Math.min(d[i], bands.length - 1)], cap(i % w, (i / w) | 0))];
      g.fillRect(i % w, (i / w) | 0, 1, 1);
    }
    return c;
  }

  /* A fire column, w x h: a body that tapers to a swaying tip, and two
     tongues peeling off its sides. Three frames, each also mirrored, in
     three sizes: the first turn of the ring is lower, so the medallion in
     the middle of it stays in sight. */
  var SIZES = [[7, 15], [9, 19], [11, 24]];
  function pillar(f, flip, w, h) {
    var m = [], ph = f * 2.2, cx = (w - 1) / 2, top = .96 - .06 * (f % 2), sc = w / 11;
    var tongues = [[-1, .28, .56 + .08 * Math.sin(ph), 3.8], [1, .36, .7 + .08 * Math.cos(ph), 3.6]];
    for (var row = 0; row < h; row++) {
      var y = h - 1 - row, u = y / (h - 1);
      for (var x = 0; x < w; x++) {
        var inside = false;
        if (u <= top) {
          var hw = 5.4 * sc * Math.pow(1 - u / top, .8) - (y === 0 ? 1.4 : y === 1 ? .5 : 0);
          var c = cx + 1.4 * sc * Math.sin(u * 4.2 + ph) * Math.pow(u / top, 1.2);
          inside = Math.abs(x - c) <= hw - .15;
        }
        for (var t = 0; t < 2 && !inside; t++) {
          var s = tongues[t];
          if (u < s[1] || u > s[2]) continue;
          var k = (u - s[1]) / (s[2] - s[1]);
          var tc = cx + s[0] * sc * (1.5 + s[3] * Math.pow(k, .9)) + .6 * Math.sin(ph + s[0]);
          inside = Math.abs(x - tc) <= 1.5 * sc * Math.pow(1 - k, .7);
        }
        m[row * w + (flip ? w - 1 - x : x)] = inside;
      }
    }
    return m;
  }
  var COLUMN_BANDS = [0, 1, 2, 3, 4];        // one pixel a band
  function columnCap(h) {                    // cooler toward the tip
    return function (x, y) {
      var u = (h - 1 - y) / (h - 1);
      return u < .42 ? 4 : u < .66 ? 3 : u < .86 ? 2 : 1;
    };
  }

  /* A burst, 41 x 41, seven frames: the blast of a bomb. A white-hot ball
     swells into a cloud of seven lumps, the middle burns out, and the lumps
     drift apart into puffs. */
  var BN = 41, LUMPS = 7;
  var BURST = [      // ball radius, lump distance, lump radius, hollow radius, hottest band
    [4.5, 0, 0, 0, 4], [7, 5, 4.5, 0, 4], [8.5, 8.5, 6.2, 0, 4], [7, 11, 7, 4.5, 3],
    [0, 13, 6.4, 8.5, 3], [0, 14.5, 5, 11.5, 2], [0, 15.5, 3.4, 13.5, 1]];
  function burst(k) {
    var p = BURST[k], m = [], c = (BN - 1) / 2, j;
    var lumps = [];
    for (j = 0; j < LUMPS; j++) {
      var a = 2 * Math.PI * j / LUMPS + .35 + .22 * Math.sin(j * 2.3);
      lumps.push([c + Math.cos(a) * p[1], c + Math.sin(a) * p[1] * .92, p[2] * (1 + .18 * Math.sin(j * 1.7 + k))]);
    }
    for (var y = 0; y < BN; y++) {
      for (var x = 0; x < BN; x++) {
        var r = Math.sqrt((x - c) * (x - c) + (y - c) * (y - c)), inside = r <= p[0];
        for (j = 0; j < LUMPS && !inside; j++) {
          inside = Math.sqrt((x - lumps[j][0]) * (x - lumps[j][0]) + (y - lumps[j][1]) * (y - lumps[j][1])) <= lumps[j][2];
        }
        m[y * BN + x] = inside && r >= p[3];
      }
    }
    return m;
  }
  var BLAST_BANDS = [0, 1, 2, 3, 3, 3, 4];   // a thin rim, a wide gold body, white only deep inside

  /* ---------------------------------------------------------- the fire */

  function run() {
    var root = document.documentElement;              // the viewport less its scrollbar
    var W = root.clientWidth || window.innerWidth, H = root.clientHeight || window.innerHeight;
    var cw = Math.ceil(W / PX), ch = Math.ceil(H / PX);
    var cv = document.createElement('canvas');
    cv.className = 'bombos-fire';
    cv.setAttribute('aria-hidden', 'true');
    cv.width = cw; cv.height = ch;
    cv.style.width = cw * PX + 'px';
    cv.style.height = ch * PX + 'px';
    var g = cv.getContext && cv.getContext('2d');
    if (!g) return;
    body.appendChild(cv);
    g.imageSmoothingEnabled = false;

    var css = getComputedStyle(cv);
    function tok(name, dflt) { return (css.getPropertyValue(name) || '').trim() || dflt; }
    var pal = [null, tok('--fire-red', '#A8391F'), tok('--fire-ember', '#E07A30'),
               tok('--fire-gold', '#F1C158'), tok('--fire-core', '#FFF1C9')];

    var columns = [], bursts = [], f;
    SIZES.forEach(function (sz) {
      var set = [];
      for (f = 0; f < 3; f++) {
        set.push(paint(pillar(f, false, sz[0], sz[1]), sz[0], sz[1], COLUMN_BANDS, columnCap(sz[1]), pal));
        set.push(paint(pillar(f, true, sz[0], sz[1]), sz[0], sz[1], COLUMN_BANDS, columnCap(sz[1]), pal));
      }
      columns.push(set);
    });
    BURST.forEach(function (p, k) {
      bursts.push(paint(burst(k), BN, BN, BLAST_BANDS, function () { return p[4]; }, pal));
    });

    /* The fire starts at the medallion, if it is on screen */
    var mark = document.querySelector('.brand img');
    var ox = W / 2, oy = H * .3;
    if (mark) {
      var mr = mark.getBoundingClientRect();
      if (mr.bottom > 0 && mr.top < H && mr.right > 0 && mr.left < W) {
        ox = mr.left + mr.width / 2; oy = mr.top + mr.height / 2;
      }
      mark.classList.add('bombos-raise');
    }
    ox /= PX; oy /= PX;
    var far = Math.max(Math.hypot(ox, oy), Math.hypot(cw - ox, oy), Math.hypot(ox, ch - oy), Math.hypot(cw - ox, ch - oy));

    /* The ring of columns round the medallion, winding outward: a spiral
       squashed to the ground plane, one column every SPACE fire pixels. */
    var R0 = 26, GROW = 18, TURNS = 2.4, SPACE = 8, RING_AT = 140, RING_FOR = 560, LIFE = 460;
    var REND = R0 + GROW * TURNS;
    var cols = [], th = 0, n = 0, pts = [];
    while (th < TURNS * 2 * Math.PI) {
      var rr = R0 + GROW * th / (2 * Math.PI);
      pts.push([rr, th]);
      th += SPACE / rr;
    }
    n = pts.length;
    pts.forEach(function (p, j) {
      var a = p[1] + .5;
      var x = Math.round(ox + Math.cos(a) * p[0]), y = Math.round(oy + Math.sin(a) * p[0] * .72);
      var turn = p[1] / (2 * Math.PI), size = turn < .9 ? 0 : turn < 1.8 ? 1 : 2;
      if (x < -12 || x > cw + 12 || y < 0 || y > ch + 24) return;
      cols.push({ x: x, y: y, born: RING_AT + RING_FOR * j / n, set: columns[size], w: SIZES[size][0], h: SIZES[size][1], ph: j });
    });
    cols.sort(function (a, b) { return a.y - b.y; });      // nearer the bottom draws on top

    /* Then bursts across the page, each when the fire front reaches it */
    var WAVE_AT = 470, WAVE_TO = 1180;
    var speed = Math.max(far - REND * .8, 1) / (WAVE_TO - WAVE_AT);
    function reach(t) {                                 // how far out the fire is at t
      return Math.max(t < RING_AT ? 0 : Math.min(REND, R0 + (REND - R0) * (t - RING_AT) / RING_FOR),
                      REND * .8 + (t - WAVE_AT) * speed);
    }
    var booms = [], CELL = 92;
    for (var gy = CELL * .45; gy < ch + CELL * .3; gy += CELL * .8) {
      for (var gx = CELL * (.4 + (Math.round(gy / CELL) % 2) * .5); gx < cw + CELL * .3; gx += CELL) {
        var bx = Math.round(gx + (Math.random() - .5) * CELL * .6), by = Math.round(gy + (Math.random() - .5) * CELL * .5);
        var bd = Math.hypot(bx - ox, (by - oy) / .9);
        if (bd < REND * .9) continue;
        booms.push({ x: bx, y: by, born: WAVE_AT + (bd - REND * .8) / speed + Math.random() * 50, step: 52 + Math.random() * 14 });
      }
    }

    /* Sparks: two off each column as it dies, three off each burst */
    var sparks = [];
    function spark(x, y, born) {
      sparks.push({ x: x, y: y, born: born, life: 260 + Math.random() * 200,
                    vx: (Math.random() - .5) * .02, vy: -.03 - Math.random() * .035 });
    }
    cols.forEach(function (c) {
      spark(c.x - 2 + Math.random() * 4, c.y - c.h * .7, c.born + LIFE - 60);
      spark(c.x - 3 + Math.random() * 6, c.y - c.h * .5, c.born + LIFE - 30);
    });
    booms.forEach(function (b) {
      for (var k = 0; k < 3; k++) spark(b.x + (Math.random() - .5) * 28, b.y + (Math.random() - .5) * 24, b.born + b.step * 4);
    });

    /* Everything on the page the front reaches: panels flare, headings glow */
    var SEL = '.panel, .item, .tag, .code-chip, .home h1, .bar-title h1';
    var marks = [], lit = [];
    function survey() {
      Array.prototype.forEach.call(document.querySelectorAll(SEL), function (el) {
        if (marks.some(function (m) { return m.el === el; })) return;
        var b = el.getBoundingClientRect();
        if (b.width < 24 || b.height < 12 || b.bottom < 0 || b.top > H || b.right < 0 || b.left > W) return;
        var nx = Math.min(Math.max(ox * PX, b.left), b.right), ny = Math.min(Math.max(oy * PX, b.top), b.bottom);
        marks.push({ el: el, d: Math.hypot(nx - ox * PX, (ny - oy * PX) / .8) / PX, done: false });
      });
    }
    survey();

    var t0 = performance.now(), raf = 0, surveyed = 1;
    function draw(now) {
      var t = Math.max(0, now - t0), k, a;
      g.clearRect(0, 0, cw, ch);
      booms.forEach(function (b) {
        a = t - b.born;
        if (a < 0 || a >= b.step * bursts.length) return;
        g.drawImage(bursts[Math.floor(a / b.step)], b.x - (BN >> 1), b.y - (BN >> 1));
      });
      cols.forEach(function (c) {
        a = t - c.born;
        if (a < 0 || a >= LIFE) return;
        // up fast with a little overshoot, flicker, then sink back down
        var s = a < 70 ? .3 + a / 70 * .85 : a < 110 ? 1.15 - (a - 70) / 40 * .15 : a < LIFE - 100 ? 1 : (LIFE - a) / 100;
        var h = Math.max(2, Math.round(c.h * s));
        g.drawImage(c.set[((Math.floor(a / 70) + c.ph) % 3) * 2 + (c.ph & 1)], 0, 0, c.w, c.h, c.x - (c.w >> 1), c.y - h, c.w, h);
      });
      sparks.forEach(function (p) {
        a = t - p.born;
        if (a < 0 || a >= p.life) return;
        k = a / p.life;
        g.fillStyle = pal[k < .3 ? 4 : k < .55 ? 3 : k < .8 ? 2 : 1];
        g.fillRect(Math.round(p.x + p.vx * a), Math.round(p.y + p.vy * a), 1, 1);
      });
      // the layout may still be arriving (a room shows its cards once it has the state)
      if (surveyed < 3 && t > surveyed * 330) { surveyed++; survey(); }
      var front = reach(t);
      marks.forEach(function (m) {
        if (m.done || m.d + 12 > front) return;          // a beat after the fire is on it
        m.done = true;
        m.el.classList.add('bombos-caught');
        lit.push(m.el);
      });
      if (t < END) raf = requestAnimationFrame(draw);
    }
    raf = requestAnimationFrame(draw);

    setTimeout(function () {
      cancelAnimationFrame(raf);
      cv.remove();
      if (mark) mark.classList.remove('bombos-raise');
      lit.forEach(function (el) { el.classList.remove('bombos-caught'); });
    }, END + 40);
  }

  /* A tab opened in the background waits until it is looked at */
  function start() {
    if (!document.hidden) { run(); return; }
    document.addEventListener('visibilitychange', function shown() {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', shown);
      run();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
