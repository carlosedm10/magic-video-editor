/* Reel social safe zones + face safety UI (spec v7.7).

   Owns exactly one global: window.SafeZonesUI. It is a small, self-contained
   companion to ui/editor/reeleditor.js (same author, but kept in its own
   file per this task's ownership brief) -- reeleditor.js owns the Reel
   Editor's DOM/lifecycle and hands this module two element refs to render
   into:
     - a "bar" host (#re-safezone-bar, above the 9:16 preview): platform
       toggle chips (Ninguno / TikTok / Reels / Shorts) + the inline safety
       message/intervals/fix-button.
     - the crop-window host (#re-crop-window): a non-interactive mockup
       overlay (platform chrome + optional hatched safe-zone rects) sized to
       match that element's rect 1:1 (it's always the true 9:16 output rect,
       whatever the on-screen frame size).

   Server contract (magic_video_editor/api/safety.py -- NOT mounted in
   server.py as of this writing; every call below can legitimately 404 until
   the integrator adds `app.include_router(safety_api.router)`; this module
   degrades to an inline "not available yet" note rather than throwing):
     GET /api/safezones -> {platform_key: {key, label, zones: [{name,x,y,w,h}
       as fractions of a 1080x1920 canvas]}, ...} -- fetched once per session
       (module-level cache, same pattern as reeleditor.js's _fontsCache).
     GET /api/projects/{pid}/reels/{rid}/safety?platform=<key> ->
       {safe, coverage_pct, intervals: [{t0,t1,zone}], suggested_fit_scale}
       -- t0/t1 are on the reel's own concatenated segment timeline (0 =
       reel start), exactly what reeleditor.js's _seekToGlobalTime expects.
     PATCH /api/projects/{pid}/reels/{rid} {fit_mode:"fit_blur", fit_scale}
       -- the one-click fix (magic_video_editor/api/reels.py, not this file's
       endpoint to define, just to call).

   Usage (see ui/editor/reeleditor.js for the call sites):
     SafeZonesUI.mount(barEl, cropWindowEl)   // once, first open
     SafeZonesUI.setContext({ pid, rid, getReel, getTotalDuration, onSeek,
                              onReelPatched }) // every open / reel switch
     SafeZonesUI.notifyChange()               // after in/out/crop/fit edits
     SafeZonesUI.reset()                      // on close

   Every method that reeleditor.js calls into is safe to call even if this
   module failed to load or throws internally -- reeleditor.js always guards
   `window.SafeZonesUI` calls in try/catch, and every DOM-touching method
   here null-checks its hosts, so a bug in this file can degrade the safety
   feature but must never blank the Reel Editor around it. */

(function () {
  const PLATFORM_ORDER = ["none", "tiktok", "reels", "shorts"];
  const NONE_LABEL = "Ninguno";
  const CHECK_DEBOUNCE_MS = 700;
  const TOPBAR_LABELS = { tiktok: "Para ti", reels: "Reels", shorts: "Shorts" };

  let _specCache = null; // GET /api/safezones response, project-independent -- fetch once per session
  let _specLoading = null;

  async function ensureSpec() {
    if (_specCache) return _specCache;
    if (_specLoading) return _specLoading;
    _specLoading = api("/safezones")
      .then((spec) => { _specCache = spec; return spec; })
      .catch((e) => {
        // Expected during integration until server.py mounts the router --
        // log once, degrade gracefully, allow a later retry (don't cache
        // the failure) in case the page is reloaded after it's mounted.
        console.error("SafeZonesUI: GET /api/safezones failed (router may not be mounted yet)", e);
        return null;
      })
      .finally(() => { _specLoading = null; });
    return _specLoading;
  }

  const SafeZonesUI = {
    _mounted: false,
    _barEl: null,
    _chipsHost: null,
    _zonesBtn: null,
    _msgEl: null,
    _overlayHost: null,
    _spec: null,
    _ctx: null,
    _platform: "none",
    _showZones: false,
    _loading: false,
    _safety: null,
    _safetyError: null,
    _checkSeq: 0,
    _debounceTimer: null,

    /* ---------- one-time DOM + styles ---------- */

    mount(barEl, cropWindowEl) {
      if (this._mounted) return;
      if (!barEl || !cropWindowEl) return;
      this._mounted = true;
      this._injectStyles();

      barEl.innerHTML = `
        <div class="szu-chips" id="szu-chips"></div>
        <button type="button" class="btn small" id="szu-zones-btn" disabled>Ver zonas</button>
      `;
      const msg = document.createElement("div");
      msg.className = "szu-message";
      msg.id = "szu-message";
      msg.hidden = true;
      barEl.appendChild(msg);

      this._barEl = barEl;
      this._chipsHost = barEl.querySelector("#szu-chips");
      this._zonesBtn = barEl.querySelector("#szu-zones-btn");
      this._msgEl = msg;
      this._zonesBtn.onclick = () => {
        this._showZones = !this._showZones;
        this._zonesBtn.classList.toggle("active", this._showZones);
        this._renderOverlay();
      };

      const overlay = document.createElement("div");
      overlay.className = "szu-overlay";
      overlay.id = "szu-overlay";
      overlay.hidden = true;
      cropWindowEl.appendChild(overlay);
      this._overlayHost = overlay;

      this._renderChips();
    },

    _injectStyles() {
      if (document.getElementById("safezones-ui-styles")) return;
      const style = document.createElement("style");
      style.id = "safezones-ui-styles";
      style.textContent = `
        .szu-chips { display: flex; gap: 6px; flex-wrap: wrap; }
        .szu-chip { font-size: 11px; padding: 4px 10px; border-radius: 999px; background: var(--panel2);
          border: 1px solid var(--border); color: var(--dim); cursor: pointer; position: relative; }
        .szu-chip:hover { color: var(--text); border-color: var(--accent); }
        .szu-chip.active { color: var(--text); border-color: var(--accent-hover); background: var(--accent); }
        .szu-chip.warn::after { content: ""; position: absolute; top: -2px; right: -2px; width: 8px; height: 8px;
          border-radius: 999px; background: var(--danger); box-shadow: 0 0 0 2px var(--bg); }
        .szu-message { flex-basis: 100%; font-size: 12px; color: var(--text); margin-top: 4px; }
        .szu-message .dim { font-size: 12px; }
        .szu-interval { color: var(--accent-hover); cursor: pointer; text-decoration: underline;
          text-underline-offset: 2px; }
        .szu-interval:hover { color: var(--accent2); }
        .szu-fixbtn { display: block; margin-top: 6px; }

        .szu-overlay { position: absolute; inset: 0; z-index: 10; pointer-events: none; }
        .szu-badge { position: absolute; left: 4px; top: 4px; font-size: 9px; letter-spacing: .03em;
          text-transform: uppercase; color: rgba(255,255,255,.55); background: rgba(0,0,0,.35);
          border-radius: 4px; padding: 1px 5px; z-index: 3; }
        .szu-zone { position: absolute; box-sizing: border-box; overflow: hidden; }
        .szu-zone.szu-hatch { background-image: repeating-linear-gradient(45deg,
          rgba(194,32,48,.4) 0 6px, rgba(194,32,48,0) 6px 12px);
          outline: 1px dashed rgba(194,32,48,.8); outline-offset: -1px; }

        .szu-topbar { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
          background: linear-gradient(to bottom, rgba(0,0,0,.45), transparent); color: #fff; font-size: 10px;
          font-weight: 600; letter-spacing: .02em; }

        .szu-rail { position: absolute; inset: 0; display: flex; flex-direction: column;
          align-items: center; justify-content: space-evenly; color: #fff; filter: drop-shadow(0 1px 3px rgba(0,0,0,.6)); }
        .szu-rail i { width: 16px; height: 16px; opacity: .92; }

        .szu-bottom { position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: flex-end;
          gap: 4px; padding: 6px 44px 8px 8px; color: #fff; background: linear-gradient(to top, rgba(0,0,0,.55), transparent 70%);
          box-sizing: border-box; }
        .szu-username { font-size: 11px; font-weight: 700; text-shadow: 0 1px 2px rgba(0,0,0,.6); }
        .szu-caption-line { font-size: 10px; opacity: .9; white-space: nowrap; overflow: hidden;
          text-overflow: ellipsis; text-shadow: 0 1px 2px rgba(0,0,0,.6); }
        .szu-marquee { display: flex; align-items: center; gap: 4px; font-size: 9px; opacity: .85; overflow: hidden;
          white-space: nowrap; }
        .szu-marquee i { width: 11px; height: 11px; flex-shrink: 0; }
        .szu-marquee span { display: inline-block; animation: szu-scroll 6s linear infinite; }
        @media (prefers-reduced-motion: reduce) {
          .szu-marquee span { animation: none; text-overflow: ellipsis; overflow: hidden; }
        }
        @keyframes szu-scroll {
          0% { transform: translateX(0); } 100% { transform: translateX(-50%); }
        }
      `;
      document.head.appendChild(style);
    },

    /* ---------- context lifecycle (per reel open) ---------- */

    setContext(ctx) {
      this._ctx = ctx;
      this._platform = "none";
      this._showZones = false;
      this._safety = null;
      this._safetyError = null;
      this._loading = false;
      clearTimeout(this._debounceTimer);
      this._checkSeq++;
      if (this._zonesBtn) this._zonesBtn.disabled = true;
      if (this._zonesBtn) this._zonesBtn.classList.remove("active");
      this._renderChips();
      this._renderOverlay();
      this._renderMessage();
      ensureSpec().then((spec) => {
        if (this._ctx !== ctx) return; // a newer open superseded this one
        this._spec = spec;
        this._renderChips();
        this._renderOverlay();
      });
    },

    reset() {
      this._ctx = null;
      clearTimeout(this._debounceTimer);
      this._checkSeq++;
      this._platform = "none";
      this._showZones = false;
      this._safety = null;
      this._safetyError = null;
      this._loading = false;
    },

    notifyChange() {
      if (!this._ctx || this._platform === "none") return;
      clearTimeout(this._debounceTimer);
      this._debounceTimer = setTimeout(() => this._runCheck(), CHECK_DEBOUNCE_MS);
    },

    /* ---------- platform selection ---------- */

    _labelFor(key) {
      if (key === "none") return NONE_LABEL;
      return this._spec?.[key]?.label || key;
    },

    _zonesFor(key) {
      if (key === "none") return null;
      return this._spec?.[key]?.zones || null;
    },

    _selectPlatform(key) {
      if (this._platform === key) return;
      this._platform = key;
      this._safety = null;
      this._safetyError = null;
      this._loading = false;
      clearTimeout(this._debounceTimer);
      if (this._zonesBtn) this._zonesBtn.disabled = (key === "none");
      if (key === "none") {
        this._showZones = false;
        if (this._zonesBtn) this._zonesBtn.classList.remove("active");
      }
      this._renderChips();
      this._renderOverlay();
      this._renderMessage();
      if (key !== "none") this._runCheck();
    },

    /* ---------- safety check ---------- */

    async _runCheck() {
      if (!this._ctx || this._platform === "none") return;
      const seq = ++this._checkSeq;
      this._loading = true;
      this._safetyError = null;
      this._renderMessage();
      try {
        const reel = this._ctx.getReel?.();
        if (!reel || !reel.id) return;
        const data = await api(
          `/projects/${this._ctx.pid}/reels/${this._ctx.rid}/safety?platform=${encodeURIComponent(this._platform)}`,
        );
        if (seq !== this._checkSeq) return; // superseded by a newer platform/reel/close
        this._safety = data;
      } catch (e) {
        if (seq !== this._checkSeq) return;
        this._safety = null;
        this._safetyError = e;
      } finally {
        if (seq === this._checkSeq) this._loading = false;
        this._renderMessage();
        this._renderChips(); // warning badge reflects the fresh result
      }
    },

    async _applyFix() {
      if (!this._ctx || !this._safety || this._safety.suggested_fit_scale == null) return;
      const btn = this._msgEl?.querySelector("#szu-fix-btn");
      if (btn) { btn.disabled = true; btn.textContent = "Aplicando…"; }
      try {
        const updated = await api(`/projects/${this._ctx.pid}/reels/${this._ctx.rid}`, {
          method: "PATCH",
          body: { fit_mode: "fit_blur", fit_scale: this._safety.suggested_fit_scale },
        });
        try { this._ctx.onReelPatched?.(updated); } catch (_e) { /* ignore -- host's problem, not ours */ }
        this._runCheck();
      } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = "Arreglar: zoom out con fondo blur"; }
        alert(`No se pudo aplicar el ajuste: ${e.message}`);
      }
    },

    /* ---------- rendering ---------- */

    _renderChips() {
      if (!this._chipsHost) return;
      this._chipsHost.innerHTML = PLATFORM_ORDER.map((key) => {
        const label = this._labelFor(key);
        const active = this._platform === key ? " active" : "";
        const warn = (key === this._platform && this._safety && !this._safety.safe) ? " warn" : "";
        return `<button type="button" class="szu-chip${active}${warn}" data-platform="${key}"
          title="${esc(label)}">${esc(label)}</button>`;
      }).join("");
      this._chipsHost.querySelectorAll(".szu-chip").forEach((b) => {
        b.onclick = () => this._selectPlatform(b.dataset.platform);
      });
    },

    _renderMessage() {
      const el = this._msgEl;
      if (!el) return;
      if (this._platform === "none") { el.hidden = true; el.innerHTML = ""; return; }
      if (this._loading) {
        el.hidden = false;
        el.innerHTML = '<span class="dim">Comprobando el encuadre…</span>';
        return;
      }
      if (this._safetyError) {
        el.hidden = false;
        const notFound = this._safetyError.status === 404;
        el.innerHTML = `<span class="dim">${notFound
          ? "Comprobación de seguridad no disponible todavía."
          : `No se pudo comprobar: ${esc(this._safetyError.message)}`}</span>`;
        return;
      }
      const s = this._safety;
      if (!s) { el.hidden = true; el.innerHTML = ""; return; }
      el.hidden = false;
      if (s.safe) {
        el.innerHTML = `<span style="color:var(--accent2)">Encuadre seguro para ${esc(this._labelFor(this._platform))}.</span>`;
        return;
      }
      const label = this._labelFor(this._platform);
      const intervalSpans = (s.intervals || [])
        .map((iv) => `<span class="szu-interval" data-t0="${iv.t0}">${fmtT(iv.t0)}–${fmtT(iv.t1)}</span>`)
        .join(", ");
      let html = `<div>La cara queda tapada por la UI de ${esc(label)} en ${intervalSpans || "toda la duración"}` +
        (s.coverage_pct != null ? ` <span class="dim">(cobertura ${s.coverage_pct}%)</span>` : "") + "</div>";
      if (s.suggested_fit_scale != null) {
        html += '<button type="button" class="btn small szu-fixbtn" id="szu-fix-btn">Arreglar: zoom out con fondo blur</button>';
      }
      el.innerHTML = html;
      el.querySelectorAll(".szu-interval").forEach((sp) => {
        sp.onclick = () => { try { this._ctx?.onSeek?.(Number(sp.dataset.t0)); } catch (_e) { /* ignore */ } };
      });
      const fixBtn = el.querySelector("#szu-fix-btn");
      if (fixBtn) fixBtn.onclick = () => this._applyFix();
    },

    _renderOverlay() {
      const host = this._overlayHost;
      if (!host) return;
      const zones = this._zonesFor(this._platform);
      if (!zones) { host.hidden = true; host.innerHTML = ""; return; }
      host.hidden = false;
      const label = this._labelFor(this._platform);
      const parts = [`<div class="szu-badge">Mockup ${esc(label)}</div>`];
      zones.forEach((z) => {
        const style = `left:${(z.x * 100).toFixed(2)}%;top:${(z.y * 100).toFixed(2)}%;` +
          `width:${(z.w * 100).toFixed(2)}%;height:${(z.h * 100).toFixed(2)}%`;
        const hatch = this._showZones ? " szu-hatch" : "";
        let inner = "";
        if (z.name === "right_rail") {
          inner = '<div class="szu-rail"><i data-lucide="heart"></i><i data-lucide="message-circle"></i>'
            + '<i data-lucide="share-2"></i><i data-lucide="user"></i></div>';
        } else if (z.name === "bottom_caption") {
          inner = `<div class="szu-bottom">
            <div class="szu-username">@tu_usuario</div>
            <div class="szu-caption-line">Descripción de ejemplo del reel…</div>
            <div class="szu-marquee"><i data-lucide="music-2"></i>
              <span>Sonido original – Tu Marca&nbsp;&nbsp;•&nbsp;&nbsp;Sonido original – Tu Marca</span></div>
          </div>`;
        } else if (z.name === "top_bar") {
          inner = `<div class="szu-topbar">${esc(TOPBAR_LABELS[this._platform] || "")}</div>`;
        }
        parts.push(`<div class="szu-zone${hatch}" data-zone="${esc(z.name)}" style="${style}">${inner}</div>`);
      });
      host.innerHTML = parts.join("");
      refreshIcons();
    },
  };

  window.SafeZonesUI = SafeZonesUI;
})();
