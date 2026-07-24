/* Background aurora canvas: slow-moving, low-opacity garnet/maroon blobs
   drifting over a subtle dark-navy glow, per docs/PLATFORM-SPEC.md
   "Brand / visual identity". Defensive: any failure here must never break
   the app, so everything is wrapped in try/catch and degrades to a plain
   background (already painted via CSS on <body>) if canvas isn't usable. */

(function () {
  try {
    const canvas = document.getElementById("fx");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reducedMotion = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
    let w = 0;
    let h = 0;

    // Pre-rendered blob sprites (offscreen canvases) so per-frame work is
    // just a translate + drawImage, never a gradient rebuild.
    const BLOB_COLORS = [
      ["#a01828", "#7a1220"],
      ["#c22030", "#7a1220"],
      ["#a01828", "#5a0d18"],
      ["#7a1220", "#a01828"],
      ["#c22030", "#a01828"],
      ["#7a1220", "#5a0d18"],
    ];

    function makeBlobSprite(radius, colorPair) {
      const size = Math.round(radius * 2);
      const off = document.createElement("canvas");
      off.width = size;
      off.height = size;
      const octx = off.getContext("2d");
      const grad = octx.createRadialGradient(
        radius, radius, 0,
        radius, radius, radius
      );
      grad.addColorStop(0, colorPair[0]);
      grad.addColorStop(0.45, colorPair[1]);
      grad.addColorStop(1, "rgba(0,0,0,0)");
      octx.fillStyle = grad;
      octx.fillRect(0, 0, size, size);
      return off;
    }

    // Base subtle navy glow, also pre-rendered and just re-scaled/positioned
    // relative to viewport center on resize (rare event, cheap enough).
    let baseGlowCanvas = null;
    function makeBaseGlow(vw, vh) {
      const off = document.createElement("canvas");
      off.width = vw;
      off.height = vh;
      const octx = off.getContext("2d");
      const cx = vw / 2;
      const cy = vh / 2;
      const r = Math.max(vw, vh) * 0.75;
      const grad = octx.createRadialGradient(cx, cy, 0, cx, cy, r);
      grad.addColorStop(0, "#0a1020");
      grad.addColorStop(1, "#05070d");
      octx.fillStyle = grad;
      octx.fillRect(0, 0, vw, vh);
      return off;
    }

    const NUM_BLOBS = 5;
    const blobs = [];

    function initBlobs(vw, vh) {
      blobs.length = 0;
      const minDim = Math.min(vw, vh);
      for (let i = 0; i < NUM_BLOBS; i++) {
        const radius = minDim * (0.45 + Math.random() * 0.35);
        const colorPair = BLOB_COLORS[i % BLOB_COLORS.length];
        blobs.push({
          sprite: makeBlobSprite(radius, colorPair),
          radius,
          // Anchor point (fraction of viewport) + drift amplitude, each
          // blob orbits slowly around its anchor using time-based sine/cos
          // so motion is smooth and frame-rate independent.
          ax: Math.random(),
          ay: Math.random(),
          ampX: vw * (0.12 + Math.random() * 0.1),
          ampY: vh * (0.12 + Math.random() * 0.1),
          periodMs: (60 + Math.random() * 60) * 1000, // 60-120s
          phase: Math.random() * Math.PI * 2,
          phaseY: Math.random() * Math.PI * 2,
        });
      }
    }

    function resize() {
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
      baseGlowCanvas = makeBaseGlow(canvas.width, canvas.height);
      initBlobs(canvas.width, canvas.height);
    }

    window.addEventListener("resize", resize);
    resize();

    // Gentle scroll-driven drift offset — only applied to #main scroll
    // (the app's scroll container), best-effort, never required.
    let scrollDrift = 0;
    function onScroll(e) {
      try {
        const el = e.target;
        const top = typeof el.scrollTop === "number" ? el.scrollTop : 0;
        scrollDrift = Math.max(-40, Math.min(40, top * 0.02));
      } catch (_e) {
        // ignore
      }
    }
    document.addEventListener("scroll", onScroll, true);

    function drawStatic() {
      if (!baseGlowCanvas) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(baseGlowCanvas, 0, 0);
      ctx.globalAlpha = 0.14;
      for (const b of blobs) {
        const x = b.ax * canvas.width - b.radius;
        const y = b.ay * canvas.height - b.radius;
        ctx.drawImage(b.sprite, x, y);
      }
      ctx.globalAlpha = 1;
    }

    if (reducedMotion) {
      drawStatic();
      return;
    }

    function frame(t) {
      try {
        if (!baseGlowCanvas) {
          requestAnimationFrame(frame);
          return;
        }
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(baseGlowCanvas, 0, 0);
        ctx.globalAlpha = 0.14;
        for (const b of blobs) {
          const angle = (t / b.periodMs) * Math.PI * 2;
          const cx = b.ax * canvas.width +
            Math.cos(angle + b.phase) * b.ampX * dpr;
          const cy = b.ay * canvas.height +
            Math.sin(angle + b.phaseY) * b.ampY * dpr + scrollDrift * dpr;
          ctx.drawImage(b.sprite, cx - b.radius, cy - b.radius);
        }
        ctx.globalAlpha = 1;
      } catch (_e) {
        // Never let a runtime error here kill the app; just stop animating.
        return;
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  } catch (_e) {
    // Swallow any setup error entirely — #fx just stays blank/CSS-background.
  }
})();
