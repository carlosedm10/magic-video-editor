#!/usr/bin/env bash
# Auto-update swap helper (spec v6 "Auto-update via GitHub Releases").
#
# Invoked by magic_video_editor/updater.py's install job, already detached
# (start_new_session=True) from the running app so it survives the app's
# own exit:
#
#   update_helper.sh <dmg_path> <app_bundle_path> <pid_to_wait_for>
#
# 1. Waits for the app process (pid) to actually exit -- it can't be
#    overwritten while running.
# 2. Mounts the already sha256-verified .dmg (verification happened in
#    Python before this script was ever launched).
# 3. ditto-copies the new "Magic Video Editor.app" over the current bundle
#    path in place (ditto preserves the app bundle structure/permissions
#    better than cp -R for .app trees).
# 4. Detaches the dmg, cleans up the temp download dir.
# 5. Relaunches the (now-updated) app with `open -n` (app-first PRINCIPLE:
#    a fresh instance, not reusing any stale window state).
#
# Best-effort throughout past the pid-wait: this runs unattended after the
# app has already quit, so there's no UI to report failures to -- log to
# a file next to the dmg instead in case something needs debugging.
set -uo pipefail

DMG_PATH="$1"
APP_BUNDLE_PATH="$2"
WAIT_PID="$3"

# Fixed location (NOT inside the per-download temp dir we rm -rf below --
# that would yank the log file out from under later `log` calls).
LOG_FILE="/tmp/mve-update-helper.log"
log() { echo "[$(date '+%H:%M:%S')] $*" >>"$LOG_FILE"; }

log "waiting for pid $WAIT_PID to exit"
for _ in $(seq 1 100); do
  kill -0 "$WAIT_PID" 2>/dev/null || break
  sleep 0.3
done
if kill -0 "$WAIT_PID" 2>/dev/null; then
  log "pid $WAIT_PID still alive after timeout -- proceeding anyway"
fi

MOUNT_DIR="$(mktemp -d /tmp/mve-update-mount.XXXXXX)"
log "mounting $DMG_PATH at $MOUNT_DIR"
if ! hdiutil attach "$DMG_PATH" -nobrowse -noautoopen -mountpoint "$MOUNT_DIR" >>"$LOG_FILE" 2>&1; then
  log "hdiutil attach failed -- aborting, app will need a manual update"
  rmdir "$MOUNT_DIR" 2>/dev/null
  exit 1
fi

NEW_APP="$MOUNT_DIR/Magic Video Editor.app"
if [ ! -d "$NEW_APP" ]; then
  log "no .app found inside the mounted image at $NEW_APP -- aborting"
  hdiutil detach "$MOUNT_DIR" -quiet >>"$LOG_FILE" 2>&1
  exit 1
fi

log "copying $NEW_APP over $APP_BUNDLE_PATH"
if ! ditto "$NEW_APP" "$APP_BUNDLE_PATH" >>"$LOG_FILE" 2>&1; then
  log "ditto copy failed -- aborting, old app bundle left untouched"
  hdiutil detach "$MOUNT_DIR" -quiet >>"$LOG_FILE" 2>&1
  exit 1
fi

log "detaching dmg"
hdiutil detach "$MOUNT_DIR" -quiet >>"$LOG_FILE" 2>&1

log "cleaning up download dir $(dirname "$DMG_PATH")"
rm -rf "$(dirname "$DMG_PATH")"

log "relaunching $APP_BUNDLE_PATH"
open -n "$APP_BUNDLE_PATH" >>"$LOG_FILE" 2>&1

log "done"
