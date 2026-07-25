/* Suggestions inspector section — non-destructive review queue from the
   `reviewer` agent stage (docs/PLATFORM-SPEC.md "Suggest, don't delete").
   Contract (per this task's brief; the reviewer/backend endpoint is being
   built in parallel, so this degrades to a friendly "not available yet"
   card instead of breaking the inspector if it 404s):
     GET  /api/projects/{pid}/suggestions -> [{id, kind, message, refs,
       proposed_action}, ...]  (or {suggestions: [...]})
     POST /api/projects/{pid}/suggestions/{id}/accept
     POST /api/projects/{pid}/suggestions/{id}/dismiss
   Accept/dismiss both refresh the project (accept may mutate the EDL) and
   this panel. */

window.EditorUI = window.EditorUI || {};

const KIND_LABEL = {
  redundant: "Redundant", repeated_idea: "Repeated idea",
  off_topic: "Off topic", incoherent: "Incoherent",
  placement: "New clip", duplicate_clip: "Possible duplicate",
};

const Suggestions = {
  container: null,
  items: [],

  mount(container) {
    this.container = container;
    this.refresh();
  },

  async refresh() {
    if (!this.container || !state.pid) return;
    this.container.innerHTML = '<div class="card"><b>Suggestions</b><div class="hint">Loading…</div></div>';
    try {
      const res = await api(`/projects/${state.pid}/suggestions`);
      const all = Array.isArray(res) ? res : res.suggestions || [];
      // Only ever show cards still awaiting a decision -- accepted/dismissed
      // suggestions must not linger in the Ideas panel with live Accept/
      // Dismiss buttons (bug found live: an already-accepted "placement"
      // card kept rendering as actionable, since project["suggestions"] is
      // an append-only log, not a queue of pending items). Missing status
      // (old-format items) defaults to open for backward compat.
      this.items = all.filter((s) => (s.status || "open") === "open");
      this._render();
    } catch (e) {
      const notReady = e.status === 404;
      this.container.innerHTML = `
        <div class="card"><b>Suggestions</b>
          <div class="hint">${notReady
            ? "Not available yet — the reviewer stage hasn't produced any."
            : `Couldn't load suggestions: ${esc(e.message)}`}</div>
          <button class="btn small" id="sugg-retry">Retry</button>
        </div>`;
      const retry = document.getElementById("sugg-retry");
      if (retry) retry.onclick = () => this.refresh();
    }
  },

  _render() {
    this.container.innerHTML = `
      <div class="card">
        <div class="row"><b>Suggestions</b><span class="grow"></span>
          <button class="icon-btn" id="sugg-refresh" title="Refresh"><i data-lucide="refresh-cw"></i></button></div>
        <div class="hint">Nothing here is cut automatically — review and decide per card.</div>
        ${this.items.length ? this.items.map((s) => `
          <div class="card sugg-card">
            <div class="row"><span class="pill">${esc(KIND_LABEL[s.kind] || s.kind || "suggestion")}</span></div>
            <div style="margin:6px 0">${esc(s.message || "")}</div>
            <div class="row">
              <button class="btn small primary sugg-accept" data-sid="${esc(s.id)}">Accept</button>
              <button class="btn small sugg-dismiss" data-sid="${esc(s.id)}">Dismiss</button>
            </div>
          </div>`).join("") : '<div class="dim">No suggestions right now.</div>'}
      </div>`;

    const refresh = document.getElementById("sugg-refresh");
    if (refresh) refresh.onclick = () => this.refresh();
    this.container.querySelectorAll(".sugg-accept").forEach((btn) =>
      btn.onclick = () => this._act(btn.dataset.sid, "accept"));
    this.container.querySelectorAll(".sugg-dismiss").forEach((btn) =>
      btn.onclick = () => this._act(btn.dataset.sid, "dismiss"));
    refreshIcons();
  },

  async _act(sid, action) {
    try {
      await api(`/projects/${state.pid}/suggestions/${encodeURIComponent(sid)}/${action}`, { method: "POST" });
      await refreshProject();
      // Accepting a suggestion may have changed project["edl"] server-side —
      // reload it so the timeline/player reflect the applied cut.
      if (action === "accept" && Editor.pid === state.pid) {
        try { await Editor.load(state.pid); } catch (e) { console.error(e); }
      }
      await this.refresh();
    } catch (e) {
      alert(e.message);
    }
  },
};

window.EditorUI.suggestions = Suggestions;
