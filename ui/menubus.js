/* MenuBus — routes native macOS menu bar clicks into the web app (spec v6
   addendum "native macOS menu bar & app identity"). magic_video_editor/app.py
   builds the actual pywebview menu and, on click, calls:

     window.evaluate_js("window.MenuBus && MenuBus.dispatch('<action>')")

   so the web app stays the single source of truth for what each item does
   -- app.py only knows action *names*, never behavior. Every action must
   no-op gracefully when there is no open project (checked via the shared
   `state` from core.js), per spec.

   Also owns two small app-first PRINCIPLE bits that don't belong to any
   other module: disabling the default webview right-click context menu
   (except on real text inputs, where Cut/Copy/Paste is still useful) and a
   best-effort Cmd+N/Cmd+I/Cmd+E keyboard fallback -- pywebview's MenuAction
   has no cross-platform accelerator support (see the comment in app.py), so
   without this the File menu's shortcuts would silently do nothing when
   typed instead of clicked. Undo/Redo/Split/Delete already have real
   bindings in ui/editor/timeline.js and are untouched here. */

window.MenuBus = {
  dispatch(action) {
    try {
      this._route(action);
    } catch (e) {
      // A menu click must never crash the app -- worst case, no-op.
      console.error("MenuBus: dispatch failed for", action, e);
    }
  },

  _route(action) {
    // `state`, `setTab`, `openSettings`, `openExportDialog`, `goHome` are
    // all declared at the top level of ui/core.js (a classic <script>, no
    // modules) and are therefore visible here as bare identifiers -- same
    // global lexical scope, same document. Everything is still guarded with
    // optional chaining / typeof so a load-order hiccup degrades silently
    // instead of throwing.
    const hasProject = typeof state !== "undefined" && !!state.pid;
    const timeline = window.EditorUI?.timeline;

    switch (action) {
      case "about":
        if (typeof openSettings === "function") openSettings();
        break;

      case "check_updates":
      case "github": {
        // App-first PRINCIPLE: never open external links inside the app
        // window -- route through the native "open externally" API exposed
        // by magic_video_editor/app.py's Api.open_external.
        const url =
          action === "github"
            ? "https://github.com/carlosedm10/magic-video-editor"
            : "https://github.com/carlosedm10/magic-video-editor/releases";
        window.pywebview?.api?.open_external?.(url);
        break;
      }

      case "new_project":
        document.getElementById("new-project")?.click();
        break;

      case "import_files":
        if (!hasProject) return; // no project open -- no-op gracefully
        document.getElementById("add-files")?.click();
        break;

      case "import_folder":
        if (!hasProject) return;
        document.getElementById("add-folder")?.click();
        break;

      case "export":
        if (!hasProject) return;
        if (typeof openExportDialog === "function") openExportDialog();
        break;

      case "undo":
        if (!hasProject) return;
        window.Editor?.undo?.();
        break;

      case "redo":
        if (!hasProject) return;
        window.Editor?.redo?.();
        break;

      case "split":
        if (!hasProject) return;
        timeline?.splitAtPlayhead?.();
        break;

      case "delete":
        if (!hasProject) return;
        // Overlay selection (spec v5.9b) takes priority, same rule the
        // Delete key uses in ui/editor/timeline.js.
        if (window.Editor?.overlaySelected) window.Editor.deleteOverlay(window.Editor.overlaySelected);
        else window.Editor?.deleteSelected?.();
        break;

      case "fit_timeline":
        if (!hasProject) return;
        timeline?.zoomToFit?.();
        break;

      case "zoom_in":
        if (!hasProject || !timeline) return;
        timeline._setZoom(timeline.pxPerSec * 1.2);
        break;

      case "zoom_out":
        if (!hasProject || !timeline) return;
        timeline._setZoom(timeline.pxPerSec / 1.2);
        break;

      case "takes":
        if (!hasProject) return;
        if (typeof setTab === "function") setTab("takes");
        break;

      case "reels":
        if (!hasProject) return;
        if (typeof setTab === "function") setTab("reels");
        break;

      case "projects_home":
        if (typeof goHome === "function") goHome();
        break;

      case "shortcuts":
        if (!hasProject) return; // the popover lives on the timeline toolbar
        timeline?._toggleShortcuts?.();
        break;

      default:
        console.warn("MenuBus: unknown action", action);
    }
  },
};

/* ---------- Cmd+N / Cmd+I / Cmd+E fallback (best-effort, see file header) --
   Only fires outside text inputs, mirroring the guard in
   ui/editor/timeline.js's own keydown handler. ---------- */
document.addEventListener("keydown", (e) => {
  const meta = e.metaKey || e.ctrlKey;
  if (!meta) return;
  const tag = (document.activeElement?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || document.activeElement?.isContentEditable) return;
  if (e.key === "n" || e.key === "N") { e.preventDefault(); window.MenuBus.dispatch("new_project"); }
  else if (e.key === "i" || e.key === "I") { e.preventDefault(); window.MenuBus.dispatch("import_files"); }
  else if (e.key === "e" || e.key === "E") { e.preventDefault(); window.MenuBus.dispatch("export"); }
});

/* ---------- native-feeling context menu (app-first PRINCIPLE) ----------
   Disable the default webview right-click menu (which leaks Chromium/WebKit
   "Reload"/"Inspect"-style items) everywhere EXCEPT real text inputs, where
   the native Cut/Copy/Paste menu is still useful. */
document.addEventListener("contextmenu", (e) => {
  const tag = (e.target?.tagName || "").toLowerCase();
  const editable = tag === "input" || tag === "textarea" || e.target?.isContentEditable;
  if (!editable) e.preventDefault();
});
