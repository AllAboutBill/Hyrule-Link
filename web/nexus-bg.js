/**
 * Billogna.lol - Nexus Background Animation
 * Shared pixel texture animation for all subdomains
 */

(function() {
    'use strict';

    const PIXEL_SIZE = 28; // Bigger blocks for chunky look
    let nexusCanvas, nexusCtx, buffer, bctx;
    let pixelState = [];
    let gridW = 0, gridH = 0;

    function initPixelState(w, h) {
        pixelState = [];
        gridW = w;
        gridH = h;
        for (let i = 0; i < w * h; i++) {
            const base = Math.random();
            pixelState.push({
                value: base < 0.45 ? 20 + Math.random() * 40 :
                       base < 0.85 ? 80 + Math.random() * 70 :
                                     180 + Math.random() * 40,
                target: null,
                alpha: base < 0.45 ? 120 : base < 0.85 ? 140 : 160
            });
        }
    }

    function redrawPixels() {
        const img = bctx.createImageData(gridW, gridH);
        const d = img.data;
        for (let i = 0; i < pixelState.length; i++) {
            const idx = i * 4;
            const p = pixelState[i];
            if (p.value > 0) {
                d[idx] = d[idx+1] = d[idx+2] = p.value;
                d[idx+3] = p.alpha;
            } else {
                d[idx+3] = 0;
            }
        }
        bctx.putImageData(img, 0, 0);
        nexusCtx.clearRect(0, 0, nexusCanvas.width, nexusCanvas.height);
        nexusCtx.drawImage(
            buffer,
            0, 0, gridW, gridH,
            0, 0, nexusCanvas.width, nexusCanvas.height
        );
    }

    function driftPixels() {
        // Occasionally retarget a few pixels
        for (let i = 0; i < pixelState.length; i++) {
            if (Math.random() < 0.0009) {
                const type = Math.random();
                pixelState[i].target =
                    type < 0.4 ? 20 + Math.random() * 40 :
                    type < 0.8 ? 80 + Math.random() * 70 :
                                 180 + Math.random() * 40;
            }
        }
        // Lerp toward targets
        pixelState.forEach(p => {
            if (p.target !== null) {
                p.value += (p.target - p.value) * 0.01;
                if (Math.abs(p.target - p.value) < 1) {
                    p.value = p.target;
                    p.target = null;
                }
            }
        });
        redrawPixels();
        requestAnimationFrame(driftPixels);
    }

    function drawNexusTexture() {
        const w = Math.max(1, Math.floor(window.innerWidth / PIXEL_SIZE));
        const h = Math.max(1, Math.floor(window.innerHeight / PIXEL_SIZE));

        buffer.width = w;
        buffer.height = h;
        bctx.imageSmoothingEnabled = false;

        nexusCanvas.width = window.innerWidth;
        nexusCanvas.height = window.innerHeight;
        nexusCtx.imageSmoothingEnabled = false;

        initPixelState(w, h);
        redrawPixels();
    }

    function init() {
        nexusCanvas = document.getElementById('nexus-pixels');
        if (!nexusCanvas) {
            console.warn('Nexus background: #nexus-pixels canvas not found');
            return;
        }

        nexusCtx = nexusCanvas.getContext('2d');
        buffer = document.createElement('canvas');
        bctx = buffer.getContext('2d');

        drawNexusTexture();
        requestAnimationFrame(driftPixels);
        window.addEventListener('resize', drawNexusTexture);
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Parallax tilt on cards with [data-tilt] attribute
    document.addEventListener('DOMContentLoaded', function() {
        document.querySelectorAll('[data-tilt]').forEach(card => {
            card.addEventListener('pointermove', e => {
                const r = card.getBoundingClientRect();
                const x = e.clientX - r.left, y = e.clientY - r.top;
                const rx = ((y / r.height) - 0.5) * 6;
                const ry = ((x / r.width) - 0.5) * -6;
                card.style.transform = `translateY(-6px) rotateX(${rx}deg) rotateY(${ry}deg)`;
                card.style.setProperty('--px', `${x}px`);
                card.style.setProperty('--py', `${y}px`);
            });
            card.addEventListener('pointerleave', () => {
                card.style.transform = '';
                card.style.removeProperty('--px');
                card.style.removeProperty('--py');
            });
        });
    });
})();

