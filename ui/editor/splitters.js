/* Resizable panels (spec v5.9a): draggable splitters between every main
   region of the editor grid (#project-view.editor-grid, ui/style.css) —
   media bin | player | inspector columns, and the player-row | timeline-row
   boundary. ui/style.css/ui/index.html are owned by other agents this phase,
   so this module does the whole thing from JS: it neutralizes the static
   grid-template-areas layout with explicit line-based placement (so its own
   handle elements get real grid cells) and injects its own <style> block —
   same pattern ui/editor/timeline.js already uses for its injected chrome.

   Persistence: sizes are saved to localStorage (key below) and restored on
   every launch/reload, independent of which project is open — this is a
   layout preference, not project data.

   Reusable core: `Splitters.makeResizable(gridEl, opts)` drives one grid's
   handles generically (used here for the main editor grid); a sibling
   module with its own grid of panes (e.g. the Reel Editor) can call it too
   instead of re-implementing drag math — see the doc comment on that
   function. */

window.EditorUI = window.EditorUI || {};

const MVE_SPLIT_LS_KEY = "mve.splitters.v1";

const Splitters = {
  _wired: false,

  /* ---------- main editor grid (bin | player | inspector, over timeline) ---------- */
  mount() {
    if (this._wired) return;
    const grid = document.getElementById("project-view");
    if (!grid) return; // DOM not ready yet — mount() is safe to call again later
    this._wired = true;
    this._injectStyles();

    const defaults = { binW: 280, inspW: 340, timelineH: 260 };
    const min = { binW: 180, inspW: 240, playerW: 320, timelineH: 140, topH: 160 };

    this._mainHandle = this.makeResizable(grid, {
      key: "main",
      defaults,
      min,
      // Explicit grid-area placements (row-start/col-start/row-end/col-end)
      // reproducing the original "bin player inspector" / "bin timeline
      // timeline" areas, but over 5 column tracks / 3 row tracks so there's
      // room for 2 vertical + 1 horizontal handle track:
      //   col: 1=bin 2=splitV1 3=player 4=splitV2 5=inspector
      //   row: 1=top  2=splitH  3=timeline
      panes: {
        bin: { el: "#media-bin", area: "1 / 1 / 4 / 2" },
        player: { el: "#player-pane", area: "1 / 3 / 2 / 4" },
        inspector: { el: "#inspector-pane", area: "1 / 5 / 2 / 6" },
        timeline: { el: "#timeline-pane", area: "3 / 3 / 4 / 6" },
      },
      handles: [
        { id: "mve-split-bin", axis: "v", area: "1 / 2 / 4 / 3",
          get: (s) => s.binW, set: (s, v) => (s.binW = v),
          min: () => min.binW,
          max: (s, rect) => rect.width - min.playerW - min.inspW - 12,
          sign: 1 }, // dragging right grows the fixed-width LEFT pane (bin)
        { id: "mve-split-insp", axis: "v", area: "1 / 4 / 2 / 5",
          get: (s) => s.inspW, set: (s, v) => (s.inspW = v),
          min: () => min.inspW,
          max: (s, rect) => rect.width - s.binW - min.playerW - 12,
          sign: -1 }, // dragging right SHRINKS the fixed-width RIGHT pane (inspector)
        { id: "mve-split-row", axis: "h", area: "2 / 3 / 3 / 6",
          get: (s) => s.timelineH, set: (s, v) => (s.timelineH = v),
          min: () => min.timelineH,
          max: (s, rect) => rect.height - min.topH - 6,
          sign: -1 }, // dragging down SHRINKS the fixed-height bottom pane (timeline)
      ],
      onApply: () => {
        // Panes resized — everything that measures its own viewport on
        // render (timeline zoom-to-fit, thumbnail widths) needs a nudge.
        // timeline.js already ResizeObserves #timeline-scroll for exactly
        // this, so this is mostly a no-op safety net for the very first
        // apply (before that observer exists).
        try { window.EditorUI.timeline?.render(); } catch (e) { console.error(e); }
      },
    });
  },

  /* ---------- generic reusable core ----------
     makeResizable(gridEl, opts) wires N draggable handles into an existing
     CSS grid element:
       opts.key       — localStorage namespace (so multiple grids don't clash)
       opts.defaults  — {sizeName: px, ...} initial/reset values
       opts.min       — {sizeName: px, ...} floor values (also referenced by
                        handles' min/max functions)
       opts.panes     — {name: {el: selector, area: "r1/c1/r2/c2"}} — panes
                        get their grid-area OVERRIDDEN via inline style
                        (beats the stylesheet's `grid-area: <named-area>`)
       opts.handles   — [{id, axis: "v"|"h", area, get(state), set(state,v),
                          min(state), max(state, gridRect), sign}] — sign=1
                        means dragging right/down INCREASES the size this
                        handle controls; sign=-1 means it decreases it
                        (depends on whether the controlled pane sits to the
                        left/top or right/bottom of the handle).
       opts.onApply() — optional, called after every layout recompute.
     Returns a small controller {state, apply, reset} the caller can ignore.

     A grid using named `grid-template-areas` needs that neutralized first
     (set to "none") so explicit numeric grid-area placements on the panes
     aren't fighting an area-count mismatch against the stylesheet's fixed
     template — done once here via `gridEl.style.gridTemplateAreas`. */
  makeResizable(gridEl, opts) {
    const { key, defaults, min, panes, handles, onApply } = opts;
    const lsKey = `${MVE_SPLIT_LS_KEY}.${key}`;
    const state = { ...defaults, ...this._loadState(lsKey, defaults) };

    gridEl.style.gridTemplateAreas = "none";
    Object.values(panes).forEach((p) => {
      const el = typeof p.el === "string" ? document.querySelector(p.el) : p.el;
      if (el) el.style.gridArea = p.area;
    });

    const handleEls = handles.map((h) => {
      let el = document.getElementById(h.id);
      if (!el) {
        el = document.createElement("div");
        el.id = h.id;
        el.className = `mve-splitter mve-splitter-${h.axis}`;
        gridEl.appendChild(el);
      }
      el.style.gridArea = h.area;
      return el;
    });

    const apply = () => {
      gridEl.style.gridTemplateColumns =
        `${Math.round(state.binW ?? 0)}px 6px 1fr 6px ${Math.round(state.inspW ?? 0)}px`;
      // Generic grids (no bin/inspector) fall back to whatever sizeNames the
      // caller actually declared handles for — the main editor grid always
      // has all three, so this simple two-template approach covers it.
      if (state.timelineH != null) {
        gridEl.style.gridTemplateRows = `1fr 6px ${Math.round(state.timelineH)}px`;
      }
      handles.forEach((h) => {
        const el = document.getElementById(h.id);
        if (el) el.dataset.value = String(h.get(state));
      });
      this._saveState(lsKey, state);
      try { onApply?.(state); } catch (e) { console.error("splitters onApply failed", e); }
    };

    handles.forEach((h, i) => {
      const el = handleEls[i];
      let startClient = 0;
      let startVal = 0;
      el.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        startClient = h.axis === "v" ? e.clientX : e.clientY;
        startVal = h.get(state);
        el.classList.add("dragging");
        document.body.style.userSelect = "none";
        document.body.style.cursor = h.axis === "v" ? "col-resize" : "row-resize";
        const onMove = (ev) => {
          const cur = h.axis === "v" ? ev.clientX : ev.clientY;
          const delta = (cur - startClient) * h.sign;
          const rect = gridEl.getBoundingClientRect();
          const lo = h.min(state);
          const hi = Math.max(lo, h.max(state, rect));
          h.set(state, Math.max(lo, Math.min(hi, startVal + delta)));
          apply();
        };
        const onUp = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          el.classList.remove("dragging");
          document.body.style.userSelect = "";
          document.body.style.cursor = "";
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      });
      el.addEventListener("dblclick", () => {
        // Reset THIS handle's controlled dimension specifically (not the
        // whole grid) — resolve which `defaults` key it reads via a probe
        // value, since handles only expose get/set closures over `state`.
        this._resetHandleDimension(state, defaults, h);
        apply();
      });
    });

    // Initial layout + keep the whole thing sane whenever the grid's actual
    // box changes size (window resize, sidebar toggles, and — critically —
    // its very first reveal: core.js's selectProject() calls
    // EditorUI.onProjectSelected(), which mounts this, WHILE #project-view
    // is still [hidden] (it only unhides right after awaiting that), so
    // getBoundingClientRect() here is 0x0 on the very first mount. Clamping
    // against a 0-width rect would force every pane down to its floor and
    // (destructively) persist that shrunk size to localStorage. So: only
    // clamp state against real numbers once the rect is actually non-zero;
    // while hidden, just apply() the current/persisted values as-is (inert
    // — nothing is visible yet) and let the ResizeObserver below run the
    // real clamp the moment the grid gets laid out for real. */
    const clamp = () => {
      const rect = gridEl.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        handles.forEach((h) => {
          const lo = h.min(state);
          const hi = Math.max(lo, h.max(state, rect));
          h.set(state, Math.max(lo, Math.min(hi, h.get(state))));
        });
      }
      apply();
    };
    clamp();
    new ResizeObserver(clamp).observe(gridEl);

    return { state, apply, reset: () => { Object.assign(state, defaults); apply(); } };
  },

  // Resolves which `defaults` key a handle's get() reads, then restores it —
  // handles reference plain closures over `state`, so this small helper
  // avoids requiring callers to pass an explicit sizeName string per handle.
  _resetHandleDimension(state, defaults, h) {
    for (const k of Object.keys(defaults)) {
      const probe = { ...state };
      probe[k] = `__probe__${k}`;
      if (h.get(probe) === `__probe__${k}`) { h.set(state, defaults[k]); return; }
    }
  },

  _loadState(lsKey, defaults) {
    try {
      const raw = JSON.parse(localStorage.getItem(lsKey) || "{}");
      const out = {};
      for (const k of Object.keys(defaults)) if (typeof raw[k] === "number") out[k] = raw[k];
      return out;
    } catch (_e) {
      return {};
    }
  },
  _saveState(lsKey, state) {
    try { localStorage.setItem(lsKey, JSON.stringify(state)); }
    catch (_e) { /* storage full/unavailable — layout still works this session */ }
  },

  _injectStyles() {
    if (document.getElementById("mve-splitter-styles")) return;
    const style = document.createElement("style");
    style.id = "mve-splitter-styles";
    style.textContent = `
      .mve-splitter { position: relative; background: transparent; z-index: 15; }
      .mve-splitter::after { content: ""; position: absolute; background: var(--border);
        transition: background .12s ease; }
      .mve-splitter-v { cursor: col-resize; }
      .mve-splitter-v::after { top: 0; bottom: 0; left: 50%; width: 1px; transform: translateX(-50%); }
      .mve-splitter-h { cursor: row-resize; }
      .mve-splitter-h::after { left: 0; right: 0; top: 50%; height: 1px; transform: translateY(-50%); }
      .mve-splitter:hover::after, .mve-splitter.dragging::after { background: var(--accent); }
      .mve-splitter-v:hover, .mve-splitter-v.dragging { background: rgba(160,24,40,.08); }
      .mve-splitter-h:hover, .mve-splitter-h.dragging { background: rgba(160,24,40,.08); }
    `;
    document.head.appendChild(style);
  },
};

window.EditorUI.splitters = Splitters;
