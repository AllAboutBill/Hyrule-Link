/* HyruleLink pixel-fill hover — mirrors billogna.lol's gimmick.
   Drops a <pixel-canvas> behind interactive elements so they fill with a
   subtle grayscale shimmer on hover/focus. Requires pixel-canvas.js first. */
(function () {
  // Subtle steel shimmer: grayscale + a little dark blue (no candy color).
  const COLORS = ["#9aa3b3", "#66718a", "#41506e", "#2c3a55"].join(",");
  const MAX_SIZE = "3";

  function attach(el, soft) {
    if (!el || el.querySelector(":scope > pixel-canvas")) return;
    const pc = document.createElement("pixel-canvas");
    pc.dataset.gap = "4";
    pc.dataset.speed = "38";
    pc.dataset.maxSize = MAX_SIZE;
    pc.dataset.colors = COLORS;
    // Inline guard so the canvas (display:grid, 100%×100%) can never balloon
    // its host even before the stylesheet applies.
    pc.style.cssText = "position:absolute;inset:0;z-index:-1;pointer-events:none";
    el.classList.add("hl-pixel-host");
    if (soft) el.classList.add("hl-pixel-soft");
    el.prepend(pc);
  }

  function run() {
    // Buttons (skip the dynamic item-grid buttons — too many, recreated often).
    document.querySelectorAll("button").forEach((b) => {
      if (!b.closest("#grid")) attach(b, false);
    });
    // Borderless text hosts get a soft edge-fade instead of a hard rectangle.
    document.querySelectorAll(".site-links a").forEach((a) => attach(a, true));
    const logo = document.querySelector(".site-logo");
    if (logo) attach(logo, true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
