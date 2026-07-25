#!/usr/bin/env bash
# Auto-update swap helper (spec v6 "Auto-update via GitHub Releases").
#
# FIELD BUG (v0.6.0 -> v0.6.1 on a real M2): banner + download + progress all
# worked, but the app never relaunched and the replaced .app was left
# "corrupta" (user had to delete + re-download). Root causes, all fixed here:
#
#   1. This script used to live (and run) FROM INSIDE the .app bundle it was
#      about to overwrite -- `ditto`-ing the new app over the bundle while
#      this very file was one of the bundle's own members. That's a script
#      truncating itself mid-run. magic_video_editor/updater.py now copies
#      this file to a throwaway temp dir OUTSIDE the bundle first and runs
#      *that* copy, via `nohup` + `setsid` (start_new_session=True) with
#      stdin/out/err fully redirected, so the process tree survives the app
#      quitting. See the marker-file wait below for how it confirms the old
#      app process is actually gone before touching anything.
#   2. The swap itself used to be a `ditto` IN PLACE over the live bundle
#      path -- not atomic, and a mid-copy failure (disk full, permissions)
#      left a half-written, "damaged" app with no way back. Now: copy the
#      new .app to a temp dir on the SAME volume as the install location
#      (a sibling of $APP_BUNDLE_PATH, not /tmp, which can be a different
#      volume/tmpfs on macOS), then `mv` (rename, atomic on one volume) the
#      OLD app aside and `mv` the NEW one into place. If the copy fails,
#      nothing has touched the live bundle yet. If the final `mv` of the new
#      app fails, the old one is moved back (rollback) before giving up.
#   3. Quarantine: a .dmg/.app downloaded by the app itself carries
#      com.apple.quarantine, and this app is unsigned -- Gatekeeper then
#      reports the freshly-swapped app as "damaged". This *is* the app
#      updating itself at the explicit user request that triggered the
#      download in the first place, so stripping quarantine here is the
#      standard (if inelegant) unsigned-app self-updater move -- not a
#      Gatekeeper bypass of a foreign binary. `xattr -dr` on the new copy,
#      before it's swapped into place.
#   4. Relaunch via `open -n` (fresh instance, no stale window state), and
#      log every step to a FIXED path under the app's own data dir (passed
#      in as $5) instead of /tmp, so a field failure is actually diagnosable
#      after the fact (previously: /tmp/mve-update-helper.log, which nobody
#      goes looking for and which any /tmp cleaner can wipe).
#
# FIELD BUG #2 (owner report): ffprobe fails on the FIRST post-update
# relaunch only -- a manual quit + reopen fixes it. Root cause: quarantine
# was only ever stripped from the STAGED copy (step 3) before the `mv` into
# place; an unsigned app's first launch from a "new" bundle path can still
# trip Gatekeeper's first-launch assessment / App Translocation right at
# that swap-in moment. Fix (step 5b, right before relaunch): re-strip
# `com.apple.quarantine` from the FINAL live bundle path too, best-effort,
# plus a `sync` before `open -n`. Defense in depth -- the real fix is the
# app-side self-heal in ffmpeg_utils.py/app.py (see their headers), which
# means even if Gatekeeper still wins this race once, the app recovers on
# its own instead of requiring a manual quit+reopen.
#
# Usage (invoked by magic_video_editor/updater.py's install job, already
# copied outside the bundle and launched fully detached):
#
#   update_helper.sh <dmg_path> <app_bundle_path> <pid_to_wait_for> \
#                    <parent_alive_marker_file> <log_file>
#
# Best-effort throughout past the pid-wait: this runs unattended after the
# app has already quit, so there's no UI to report failures to -- only the
# log file.
set -uo pipefail

DMG_PATH="$1"
APP_BUNDLE_PATH="$2"
WAIT_PID="$3"
PARENT_MARKER="$4"
LOG_FILE="$5"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >>"$LOG_FILE"; }

log "==== update_helper starting (pid $$) ===="
log "dmg=$DMG_PATH bundle=$APP_BUNDLE_PATH wait_pid=$WAIT_PID marker=$PARENT_MARKER"

# --------------------------------------------------------------------------
# 1. Wait for the parent app to actually be gone before touching anything.
#
# Marker-file protocol: the parent process removes $PARENT_MARKER as
# (almost) its very last act before calling os._exit(0), so "marker gone" is
# the primary, race-free signal (unlike a bare `kill -0`, it can't be fooled
# by the pid having already been reused by an unrelated process). `kill -0`
# on the recorded pid is kept as a secondary confirmation/backstop in case
# the marker removal itself never landed (e.g. the parent was killed with
# SIGKILL before it got there).
# --------------------------------------------------------------------------
log "waiting for parent marker file to disappear: $PARENT_MARKER"
for _ in $(seq 1 300); do
  [ -e "$PARENT_MARKER" ] || break
  sleep 0.2
done
if [ -e "$PARENT_MARKER" ]; then
  log "marker still present after timeout -- proceeding to pid check anyway"
fi

log "confirming pid $WAIT_PID has exited"
for _ in $(seq 1 100); do
  kill -0 "$WAIT_PID" 2>/dev/null || break
  sleep 0.3
done
if kill -0 "$WAIT_PID" 2>/dev/null; then
  log "pid $WAIT_PID still alive after timeout -- proceeding anyway (best effort)"
else
  log "parent process confirmed gone"
fi

# --------------------------------------------------------------------------
# 2. Mount the (already sha256-verified in Python) dmg.
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# 3. Copy the new app to a temp dir that is a SIBLING of the install path,
# i.e. on the same volume, so the final swap-in is a cheap atomic rename
# rather than a cross-volume copy. Nothing at $APP_BUNDLE_PATH is touched
# yet -- if this copy fails, the old app is completely untouched.
# --------------------------------------------------------------------------
INSTALL_DIR="$(dirname "$APP_BUNDLE_PATH")"
APP_BASENAME="$(basename "$APP_BUNDLE_PATH")"
SWAP_TMP_DIR="$(mktemp -d "$INSTALL_DIR/.mve-update-staging.XXXXXX" 2>>"$LOG_FILE")"
if [ -z "$SWAP_TMP_DIR" ] || [ ! -d "$SWAP_TMP_DIR" ]; then
  log "could not create staging dir under $INSTALL_DIR (same-volume requirement) -- aborting"
  hdiutil detach "$MOUNT_DIR" -quiet >>"$LOG_FILE" 2>&1
  exit 1
fi
STAGED_NEW_APP="$SWAP_TMP_DIR/$APP_BASENAME"

log "copying $NEW_APP -> $STAGED_NEW_APP (staging, same volume as $INSTALL_DIR)"
if ! ditto "$NEW_APP" "$STAGED_NEW_APP" >>"$LOG_FILE" 2>&1; then
  log "ditto copy to staging failed -- aborting, old app bundle left completely untouched"
  rm -rf "$SWAP_TMP_DIR"
  hdiutil detach "$MOUNT_DIR" -quiet >>"$LOG_FILE" 2>&1
  exit 1
fi

log "detaching dmg (staged copy no longer needs it)"
hdiutil detach "$MOUNT_DIR" -quiet >>"$LOG_FILE" 2>&1

# --------------------------------------------------------------------------
# 4. Strip com.apple.quarantine from the staged copy (see root cause 3 in
# the header comment). Best-effort: if `xattr` itself is missing/fails we
# still proceed with the swap -- an update that lands but shows a Gatekeeper
# prompt is recoverable by the user; an update that never lands at all
# (previous bug) is not.
# --------------------------------------------------------------------------
log "removing com.apple.quarantine from staged app (this app updating itself, user-initiated)"
if xattr -dr com.apple.quarantine "$STAGED_NEW_APP" >>"$LOG_FILE" 2>&1; then
  log "quarantine attribute removed"
else
  log "xattr -dr com.apple.quarantine failed or had nothing to remove -- continuing anyway"
fi

# --------------------------------------------------------------------------
# 5. Atomic-ish swap: rename the OLD app aside first (kept until the new one
# is confirmed in place), then rename the STAGED new app into the live
# path. Both are same-volume renames. If the final rename fails, the old
# app is renamed back -- rollback.
# --------------------------------------------------------------------------
OLD_APP_BACKUP="${APP_BUNDLE_PATH}.rollback-$(date +%s)"
SWAP_OK=0

if [ -d "$APP_BUNDLE_PATH" ]; then
  log "moving current app aside: $APP_BUNDLE_PATH -> $OLD_APP_BACKUP"
  if ! mv "$APP_BUNDLE_PATH" "$OLD_APP_BACKUP" >>"$LOG_FILE" 2>&1; then
    log "could not move current app aside (permissions?) -- aborting, nothing changed"
    rm -rf "$SWAP_TMP_DIR"
    exit 1
  fi
else
  log "no existing app at $APP_BUNDLE_PATH (unexpected, but proceeding as a fresh install)"
  OLD_APP_BACKUP=""
fi

log "moving staged new app into place: $STAGED_NEW_APP -> $APP_BUNDLE_PATH"
# Test-only hook (unset/0 in real installs): lets the dry-run harness force
# this specific step to fail without needing to fabricate a real
# filesystem-level failure, so the rollback path can be exercised
# deterministically. See scripts/dry_run_update_helper.sh.
if [ "${MVE_UPDATE_HELPER_FORCE_SWAP_FAIL:-0}" = "1" ]; then
  log "MVE_UPDATE_HELPER_FORCE_SWAP_FAIL=1 (test hook) -- forcing swap-in failure"
  MOVE_RC=1
else
  mv "$STAGED_NEW_APP" "$APP_BUNDLE_PATH" >>"$LOG_FILE" 2>&1
  MOVE_RC=$?
fi
if [ "$MOVE_RC" -eq 0 ]; then
  SWAP_OK=1
  log "swap succeeded"
else
  log "moving staged app into place FAILED -- rolling back"
  if [ -n "$OLD_APP_BACKUP" ]; then
    if mv "$OLD_APP_BACKUP" "$APP_BUNDLE_PATH" >>"$LOG_FILE" 2>&1; then
      log "rollback succeeded -- old app restored at $APP_BUNDLE_PATH"
      OLD_APP_BACKUP=""
    else
      log "ROLLBACK FAILED -- old app left at $OLD_APP_BACKUP, nothing at $APP_BUNDLE_PATH. Manual recovery needed."
    fi
  fi
fi

log "cleaning up staging dir $SWAP_TMP_DIR"
rm -rf "$SWAP_TMP_DIR"

if [ -n "$OLD_APP_BACKUP" ] && [ -d "$OLD_APP_BACKUP" ]; then
  log "removing old app backup $OLD_APP_BACKUP"
  rm -rf "$OLD_APP_BACKUP"
fi

log "cleaning up download dir $(dirname "$DMG_PATH")"
rm -rf "$(dirname "$DMG_PATH")"

if [ "$SWAP_OK" -ne 1 ]; then
  log "update FAILED -- not relaunching (app should still be usable at its previous version, or see rollback-failure note above)"
  exit 1
fi

# --------------------------------------------------------------------------
# 5b. FIELD BUG (first-relaunch ffprobe failure): quarantine was stripped
# from the STAGED copy (step 4) BEFORE it was moved into place -- but `mv`
# is a rename, not a copy, so that alone should be enough. In the field, an
# unsigned app's *first* launch from a path it hasn't run from before can
# still trip Gatekeeper's first-launch assessment / App Translocation
# (macOS re-evaluates a bundle the moment it lands somewhere new, and the
# staged copy's move-in can race that). Symptom: the nested unsigned
# ffprobe fails to exec on the update-relaunched process, but a manual quit
# + reopen from the canonical path works fine (translocation/assessment has
# settled by then). Re-stripping quarantine on the FINAL, live bundle path
# -- right before relaunch -- closes that window. Best-effort: never abort
# the update over this, an update that landed with a lingering Gatekeeper
# prompt is recoverable; one that never relaunches is the bug we're fixing.
# --------------------------------------------------------------------------
log "re-stripping com.apple.quarantine from the FINAL live bundle at $APP_BUNDLE_PATH (belt-and-suspenders for first-launch Gatekeeper/translocation)"
if xattr -dr com.apple.quarantine "$APP_BUNDLE_PATH" >>"$LOG_FILE" 2>&1; then
  log "quarantine attribute removed from live bundle"
else
  log "xattr -dr com.apple.quarantine on live bundle failed or had nothing to remove -- continuing anyway"
fi

# Let the rename/xattr changes settle on disk before we hand off to `open`
# -- cheap insurance against relaunching before the filesystem/Gatekeeper's
# view of the just-swapped-in bundle is fully consistent.
sync 2>/dev/null || true

# --------------------------------------------------------------------------
# 6. Relaunch (app-first: a fresh instance, not reusing stale window state).
# --------------------------------------------------------------------------
log "relaunching $APP_BUNDLE_PATH"
open -n "$APP_BUNDLE_PATH" >>"$LOG_FILE" 2>&1
log "==== update_helper done ===="

# Self-cleanup: this script (and its containing temp dir, copied outside the
# bundle by updater.py before launch -- see root cause 1) is no longer
# needed. Removing our own script file after we've reached the end is safe
# on macOS/Unix (the inode stays valid for this already-running process).
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
case "$(basename "$SELF_DIR")" in
  mve-update-helper-*)
    rm -rf "$SELF_DIR" 2>/dev/null
    ;;
esac

exit 0
