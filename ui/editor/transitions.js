/* Transitions catalog + browser (spec v7.5 "Transitions catalog (FCP-style,
   ffmpeg xfade)"). This module is the single owner of:
     - fetching/caching GET /api/transitions (may 404 until the integrator
       mounts the endpoint — falls back to a local catalog covering the same
       5 categories so the browser is always usable; see FALLBACK_CATALOG),
     - the animated CSS thumbnails (two colored tiles + a keyframe
       approximation of the wipe/slide/circle/pixel family; generic dissolve
       for anything unmapped),
     - making thumbnails draggable onto timeline junction chips (HTML5 DnD,
       "application/x-mve-transition" carrying the xfade name),
     - "focused" mode: opening the FX tab pre-scrolled/highlighted for one
       specific junction (used by the timeline chip's click and by the Video
       tab's transition button — see the Inspector.renderVideo patch at the
       bottom of this file).

   window.FxPanel (ui/panels/fx.js) delegates its whole render() to
   TransitionsBrowser.render() — see fx.js for why the FX tab and this module
   are split (background-aurora-fx vs. transitions-fx naming collision). */

window.EditorUI = window.EditorUI || {};

const TR_CATEGORY_ORDER = ["Fundidos", "Barridos", "Deslizamientos", "Geométricas", "Píxel"];

// Used only until GET /api/transitions is mounted server-side (or if it ever
// errors) — same shape the backend contract promises (spec 7.5): [{name,
// label_es, category, xfade_name}]. Kept intentionally short; the real
// catalog (~50 names) lives server-side once wired.
const FALLBACK_CATALOG = [
  { name: "Cross Dissolve", label_es: "Fundido cruzado", category: "Fundidos", xfade_name: "fade" },
  { name: "Fade to Black", label_es: "Fundido a negro", category: "Fundidos", xfade_name: "fadeblack" },
  { name: "Fade to White", label_es: "Fundido a blanco", category: "Fundidos", xfade_name: "fadewhite" },
  { name: "Dissolve", label_es: "Disolvencia", category: "Fundidos", xfade_name: "dissolve" },
  { name: "Wipe Left", label_es: "Barrido a la izquierda", category: "Barridos", xfade_name: "wipeleft" },
  { name: "Wipe Right", label_es: "Barrido a la derecha", category: "Barridos", xfade_name: "wiperight" },
  { name: "Wipe Up", label_es: "Barrido hacia arriba", category: "Barridos", xfade_name: "wipeup" },
  { name: "Wipe Down", label_es: "Barrido hacia abajo", category: "Barridos", xfade_name: "wipedown" },
  { name: "Slide Left", label_es: "Deslizar a la izquierda", category: "Deslizamientos", xfade_name: "slideleft" },
  { name: "Slide Right", label_es: "Deslizar a la derecha", category: "Deslizamientos", xfade_name: "slideright" },
  { name: "Slide Up", label_es: "Deslizar hacia arriba", category: "Deslizamientos", xfade_name: "slideup" },
  { name: "Slide Down", label_es: "Deslizar hacia abajo", category: "Deslizamientos", xfade_name: "slidedown" },
  { name: "Circle Open", label_es: "Círculo (abrir)", category: "Geométricas", xfade_name: "circleopen" },
  { name: "Circle Close", label_es: "Círculo (cerrar)", category: "Geométricas", xfade_name: "circleclose" },
  { name: "Circle Crop", label_es: "Recorte circular", category: "Geométricas", xfade_name: "circlecrop" },
  { name: "Rect Crop", label_es: "Recorte rectangular", category: "Geométricas", xfade_name: "rectcrop" },
  { name: "Diagonal TL", label_es: "Diagonal (arriba-izq.)", category: "Geométricas", xfade_name: "diagtl" },
  { name: "Diagonal BR", label_es: "Diagonal (abajo-der.)", category: "Geométricas", xfade_name: "diagbr" },
  { name: "Pixelize", label_es: "Pixelado", category: "Píxel", xfade_name: "pixelize" },
  { name: "Radial", label_es: "Radial", category: "Píxel", xfade_name: "radial" },
];

// Legacy values that predate the catalog (spec v3 "Transitions (junction-
// level)") — render() always keeps these selectable at the top of Fundidos
// so existing projects' junctions still show a matching card/name.
const LEGACY_ITEMS = [
  { name: "None", label_es: "Ninguna", category: "Fundidos", xfade_name: "none" },
  { name: "Fade", label_es: "Fundido", category: "Fundidos", xfade_name: "fade" },
  { name: "Crossfade", label_es: "Fundido cruzado", category: "Fundidos", xfade_name: "crossfade" },
];

function _trApproxKind(xfadeName) {
  const n = (xfadeName || "").toLowerCase();
  if (n === "none") return "none";
  if (n.startsWith("fade") || n === "dissolve" || n === "crossfade") return "fade";
  if (n.startsWith("wipeleft") || n === "wipel") return "wipe-l";
  if (n.startsWith("wiperight")) return "wipe-r";
  if (n.startsWith("wipeup")) return "wipe-u";
  if (n.startsWith("wipedown")) return "wipe-d";
  if (n.startsWith("slideleft")) return "slide-l";
  if (n.startsWith("slideright")) return "slide-r";
  if (n.startsWith("slideup")) return "slide-u";
  if (n.startsWith("slidedown")) return "slide-d";
  if (n.startsWith("circle")) return "circle";
  if (n.startsWith("rect") || n.startsWith("diag") || n.startsWith("radial") || n.startsWith("squeeze")) return "geo";
  if (n.startsWith("pixel")) return "pixel";
  return "dissolve"; // generic fallback for anything unmapped
}

function _ensureTrStyles() {
  if (document.getElementById("tr-browser-styles")) return;
  const style = document.createElement("style");
  style.id = "tr-browser-styles";
  style.textContent = `
    .tr-cat { margin-bottom: 12px; }
    .tr-cat-title { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--dim);
      margin: 0 0 6px; }
    .tr-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 8px; }
    .tr-card { background: var(--panel2); border: 1px solid var(--border); border-radius: 10px; padding: 6px;
      cursor: grab; text-align: center; user-select: none; }
    .tr-card:hover { border-color: var(--accent); }
    .tr-card.active { border-color: var(--accent-hover); box-shadow: 0 0 0 1px var(--accent-hover); }
    .tr-thumb { position: relative; width: 100%; height: 44px; border-radius: 6px; overflow: hidden;
      background: #0a1020; margin-bottom: 4px; }
    .tr-tile { position: absolute; inset: 0; }
    .tr-tile.a { background: linear-gradient(135deg, #7a1220, #a01828); }
    .tr-tile.b { background: linear-gradient(135deg, #1c2333, #2a3350); animation-duration: 1.6s;
      animation-iteration-count: infinite; animation-timing-function: ease-in-out; }
    .tr-name { font-size: 10px; color: var(--text); line-height: 1.25; }

    @keyframes tr-anim-fade { 0%, 15% { opacity: 0; } 50%, 65% { opacity: 1; } 100% { opacity: 0; } }
    .tr-tile.b.tr-anim-fade { animation-name: tr-anim-fade; }
    .tr-tile.b.tr-anim-none { animation: none; opacity: 0; }

    @keyframes tr-anim-wipe-l { 0%, 10% { clip-path: inset(0 100% 0 0); } 50%, 60% { clip-path: inset(0 0 0 0); }
      100% { clip-path: inset(0 100% 0 0); } }
    @keyframes tr-anim-wipe-r { 0%, 10% { clip-path: inset(0 0 0 100%); } 50%, 60% { clip-path: inset(0 0 0 0); }
      100% { clip-path: inset(0 0 0 100%); } }
    @keyframes tr-anim-wipe-u { 0%, 10% { clip-path: inset(100% 0 0 0); } 50%, 60% { clip-path: inset(0 0 0 0); }
      100% { clip-path: inset(100% 0 0 0); } }
    @keyframes tr-anim-wipe-d { 0%, 10% { clip-path: inset(0 0 100% 0); } 50%, 60% { clip-path: inset(0 0 0 0); }
      100% { clip-path: inset(0 0 100% 0); } }
    .tr-tile.b.tr-anim-wipe-l { animation-name: tr-anim-wipe-l; }
    .tr-tile.b.tr-anim-wipe-r { animation-name: tr-anim-wipe-r; }
    .tr-tile.b.tr-anim-wipe-u { animation-name: tr-anim-wipe-u; }
    .tr-tile.b.tr-anim-wipe-d { animation-name: tr-anim-wipe-d; }

    @keyframes tr-anim-slide-l { 0%, 10% { transform: translateX(100%); } 50%, 60% { transform: translateX(0); }
      100% { transform: translateX(100%); } }
    @keyframes tr-anim-slide-r { 0%, 10% { transform: translateX(-100%); } 50%, 60% { transform: translateX(0); }
      100% { transform: translateX(-100%); } }
    @keyframes tr-anim-slide-u { 0%, 10% { transform: translateY(100%); } 50%, 60% { transform: translateY(0); }
      100% { transform: translateY(100%); } }
    @keyframes tr-anim-slide-d { 0%, 10% { transform: translateY(-100%); } 50%, 60% { transform: translateY(0); }
      100% { transform: translateY(-100%); } }
    .tr-tile.b.tr-anim-slide-l { animation-name: tr-anim-slide-l; }
    .tr-tile.b.tr-anim-slide-r { animation-name: tr-anim-slide-r; }
    .tr-tile.b.tr-anim-slide-u { animation-name: tr-anim-slide-u; }
    .tr-tile.b.tr-anim-slide-d { animation-name: tr-anim-slide-d; }

    @keyframes tr-anim-circle { 0%, 10% { clip-path: circle(0% at 50% 50%); }
      50%, 60% { clip-path: circle(75% at 50% 50%); } 100% { clip-path: circle(0% at 50% 50%); } }
    .tr-tile.b.tr-anim-circle { animation-name: tr-anim-circle; }

    @keyframes tr-anim-geo { 0%, 10% { clip-path: polygon(0 0, 0 0, 0 0); }
      50%, 60% { clip-path: polygon(0 0, 100% 0, 0 100%); } 100% { clip-path: polygon(0 0, 0 0, 0 0); } }
    .tr-tile.b.tr-anim-geo { animation-name: tr-anim-geo; }

    @keyframes tr-anim-pixel { 0%, 10% { opacity: 0; } 50%, 60% { opacity: 1; } 100% { opacity: 0; } }
    .tr-tile.b.tr-anim-pixel { animation-name: tr-anim-pixel; animation-timing-function: steps(6, end);
      background-image: repeating-conic-gradient(#1c2333 0% 25%, #2a3350 0% 50%);
      background-size: 10px 10px; }

    @keyframes tr-anim-dissolve { 0%, 10% { opacity: 0; filter: blur(2px); }
      50%, 60% { opacity: 1; filter: blur(0); } 100% { opacity: 0; filter: blur(2px); } }
    .tr-tile.b.tr-anim-dissolve { animation-name: tr-anim-dissolve; }

    /* junction chip: name label + drop-target highlight (spec v7.5) */
    .tl-chip.tr-drop-target { outline: 2px dashed var(--accent2); outline-offset: 1px;
      background: rgba(53,194,143,.3) !important; }
  `;
  document.head.appendChild(style);
}

const TransitionsBrowser = {
  _catalog: null,
  _loading: null,
  _focusIndex: null, // segment index the browser should apply clicks to when opened "focused"

  async _loadCatalog() {
    if (this._catalog) return this._catalog;
    if (this._loading) return this._loading;
    this._loading = (async () => {
      try {
        const list = await api("/transitions");
        if (Array.isArray(list) && list.length) return (this._catalog = list);
        throw new Error("empty catalog");
      } catch (e) {
        console.warn("GET /api/transitions unavailable (fallback catalog in use):", e.message);
        return (this._catalog = FALLBACK_CATALOG);
      }
    })();
    return this._loading;
  },

  /* Opens the FX tab with the browser scoped to one junction (segment index
     `i`, i.e. "transition into segment i") — used by the timeline chip click
     and the Video tab's transition button. */
  openFocused(i) {
    Editor.select(i);
    this._focusIndex = i;
    try { window.EditorUI.inspector?.switchTab("fx"); } catch (e) { console.error(e); }
  },

  /* Display name for a stored transition type — used by the timeline's
     junction chips and by the Video-tab button patch below. Falls back to
     the raw xfade name (still readable, e.g. "wipeleft") if the catalog
     hasn't loaded yet or doesn't recognize it. */
  labelFor(xfadeName) {
    if (!xfadeName || xfadeName === "none") return null;
    const legacy = LEGACY_ITEMS.find((x) => x.xfade_name === xfadeName);
    if (legacy) return legacy.label_es;
    const found = (this._catalog || FALLBACK_CATALOG).find((x) => x.xfade_name === xfadeName);
    return found?.label_es || found?.name || xfadeName;
  },

  render(container, _project) {
    if (!container) return;
    _ensureTrStyles();
    container.innerHTML = `<div class="card" id="tr-browser-root">
      <div class="row"><b>Transitions</b><span class="grow"></span>
        <span class="dim" id="tr-focus-label"></span></div>
      <div class="hint">Click a transition to apply it to the selected junction, or drag it onto a
        junction chip in the timeline.</div>
      <div id="tr-sections"></div>
    </div>`;

    const focusLabel = container.querySelector("#tr-focus-label");
    if (focusLabel) {
      const i = this._focusIndex ?? Editor.selected;
      focusLabel.textContent = Editor.segments?.length ? `Junction ${Math.max(0, i)}` : "";
    }

    const sections = container.querySelector("#tr-sections");
    sections.innerHTML = `<div class="dim">Loading catalog…</div>`;
    this._loadCatalog().then((catalog) => {
      if (!container.isConnected) return; // tab switched away while fetching
      this._renderSections(sections, catalog);
    });
  },

  _renderSections(sections, catalog) {
    const current = Editor.segments?.[this._focusIndex ?? Editor.selected]?.transition?.type || "none";
    const byCategory = new Map();
    TR_CATEGORY_ORDER.forEach((c) => byCategory.set(c, []));
    LEGACY_ITEMS.forEach((it) => byCategory.get("Fundidos").push(it));
    catalog.forEach((it) => {
      const cat = TR_CATEGORY_ORDER.includes(it.category) ? it.category : "Fundidos";
      if (!byCategory.has(cat)) byCategory.set(cat, []);
      byCategory.get(cat).push(it);
    });

    sections.innerHTML = [...byCategory.entries()].filter(([, items]) => items.length).map(([cat, items]) => `
      <div class="tr-cat">
        <div class="tr-cat-title">${esc(cat)}</div>
        <div class="tr-grid">
          ${items.map((it) => {
    const kind = _trApproxKind(it.xfade_name);
    const active = it.xfade_name === current || (it.xfade_name === "crossfade" && current === "crossfade");
    return `<div class="tr-card${active ? " active" : ""}" draggable="true"
              data-xfade="${esc(it.xfade_name)}" title="${esc(it.label_es || it.name)}">
              <div class="tr-thumb"><div class="tr-tile a"></div><div class="tr-tile b tr-anim-${kind}"></div></div>
              <div class="tr-name">${esc(it.label_es || it.name)}</div>
            </div>`;
  }).join("")}
        </div>
      </div>`).join("");

    sections.querySelectorAll(".tr-card").forEach((card) => {
      card.onclick = () => this._applyToFocused(card.dataset.xfade, sections);
      card.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("application/x-mve-transition", card.dataset.xfade);
        e.dataTransfer.effectAllowed = "copy";
      });
    });
  },

  _applyToFocused(xfadeName, sectionsEl) {
    const i = this._focusIndex ?? Editor.selected;
    if (i == null || !Editor.segments?.[i]) return;
    Editor.setTransition(i, xfadeName);
    // Editor.setTransition() re-renders the timeline/inspector via its own
    // notify hooks, but this browser's own "active" outline is drawn from a
    // one-time snapshot at render() time — flip it locally instead of
    // re-fetching/re-rendering the whole grid for one click.
    sectionsEl?.querySelectorAll(".tr-card").forEach((c) =>
      c.classList.toggle("active", c.dataset.xfade === xfadeName));
  },
};

window.EditorUI.transitions = TransitionsBrowser;

/* ---------- Inspector Video tab integration (spec v7.5 §1) ----------
   inspector.js (owned by another agent this phase) renders a full
   None/Fade/Crossfade button row under "Transition into this segment"
   every time Editor.renderVideo() runs. Rather than editing that file, this
   wraps its exported render function the same "fair game" way
   ui/editor/timeline.js already patches DOM it doesn't own (see that file's
   toolbar relabeling) — except here we wrap the *function*, since the
   target markup is rebuilt from scratch on every call and a plain
   post-render DOM patch would be clobbered on the very next selection
   change. Runs after inspector.js's script tag (see index.html) so
   window.EditorUI.inspector already exists. */
(function patchVideoTabTransitionButton() {
  function tryPatch() {
    const inspector = window.EditorUI?.inspector;
    if (!inspector || inspector.__trPatched) return false;
    const original = inspector.renderVideo.bind(inspector);
    inspector.renderVideo = function () {
      original();
      const el = document.getElementById("insp-video");
      const btnsEl = el?.querySelector(".transition-btns");
      if (!btnsEl) return;
      const segs = Editor.segments;
      if (!segs?.length) return;
      const i = Math.min(Editor.selected, segs.length - 1);
      const tr = segs[i].transition || { type: "none", duration: 0.5 };
      const label = TransitionsBrowser.labelFor(tr.type) || "None";
      // Duration input (if the original render built one for a non-"none"
      // transition) stays as-is — only the type-selection button row is
      // replaced with the "open the FX browser" entry point.
      const wrap = document.createElement("div");
      wrap.className = "row";
      wrap.innerHTML = `<button class="btn small" id="insp-tr-open">
        <i data-lucide="wand-2"></i> ${esc(label)}</button>`;
      btnsEl.replaceWith(wrap);
      wrap.querySelector("#insp-tr-open").onclick = () => TransitionsBrowser.openFocused(i);
      try { window.refreshIcons?.(); } catch (e) { console.error(e); }
    };
    inspector.__trPatched = true;
    return true;
  }
  if (!tryPatch()) document.addEventListener("DOMContentLoaded", tryPatch);
})();
