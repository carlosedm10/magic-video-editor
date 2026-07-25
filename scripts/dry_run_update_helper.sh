#!/usr/bin/env bash
# DRY-RUN harness for packaging/update_helper.sh. Builds fake .app trees
# under /tmp, a fake dmg, and a fake "old app" process (sleep), and asserts
# the whole swap protocol behaves: waits for the parent, swaps atomically,
# strips quarantine, rolls back on a forced failure, and logs everything.
#
# Never touches the real installed app or launches any real binary -- this
# only ever operates on throwaway trees under $WORK.
#
# Usage: bash scripts/dry_run_update_helper.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$REPO_ROOT/packaging/update_helper.sh"

WORK="$(mktemp -d /tmp/mve-helper-dryrun.XXXXXX)"
PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "== working dir: $WORK =="

make_fake_dmg() {
  # $1 = version marker text written inside the app, $2 = dmg dest path
  local ver="$1" dmg_dest="$2"
  local src_dir
  src_dir="$(mktemp -d "$WORK/dmgsrc.XXXXXX")"
  mkdir -p "$src_dir/Magic Video Editor.app/Contents/MacOS"
  echo "$ver" >"$src_dir/Magic Video Editor.app/Contents/version.txt"
  touch "$src_dir/Magic Video Editor.app/Contents/MacOS/Magic Video Editor"
  chmod +x "$src_dir/Magic Video Editor.app/Contents/MacOS/Magic Video Editor"
  # Simulate the quarantine attribute a browser/curl download would carry.
  xattr -w com.apple.quarantine "0081;00000000;Safari;" \
    "$src_dir/Magic Video Editor.app" 2>/dev/null || true
  hdiutil create -volname "Magic Video Editor" -srcfolder "$src_dir" \
    -ov -format UDZO -quiet "$dmg_dest" >/dev/null
  rm -rf "$src_dir"
}

run_helper_and_wait() {
  # $1=dmg $2=bundle $3=wait_pid $4=marker $5=log $6=extra_env(optional "K=V")
  local dmg="$1" bundle="$2" pid="$3" marker="$4" log="$5" extra_env="${6:-}"
  env $extra_env /usr/bin/nohup /bin/bash "$HELPER" "$dmg" "$bundle" "$pid" "$marker" "$log" \
    >/dev/null 2>&1 &
  local helper_pid=$!
  # Give the harness control of when the "parent" appears to die.
  echo "$helper_pid"
}

wait_for_log() {
  # $1=log $2=needle $3=timeout_s
  local log="$1" needle="$2" timeout="${3:-15}"
  local waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if [ -f "$log" ] && grep -qF "$needle" "$log" 2>/dev/null; then
      return 0
    fi
    sleep 0.3
    waited=$((waited + 1))
  done
  return 1
}

# ---------------------------------------------------------------------
# Scenario 1: happy path -- swap + quarantine strip + relaunch (stubbed).
# ---------------------------------------------------------------------
echo "--- scenario 1: happy path ---"
S1="$WORK/s1"
mkdir -p "$S1/AppRoot"
mkdir -p "$S1/AppRoot/Magic Video Editor.app/Contents/MacOS"
echo "old-version" >"$S1/AppRoot/Magic Video Editor.app/Contents/version.txt"
DMG1="$S1/update.dmg"
make_fake_dmg "new-version" "$DMG1"

# Fake "open" so relaunch doesn't actually try to launch a real app -- put a
# stub `open` earlier on PATH.
mkdir -p "$S1/bin"
cat >"$S1/bin/open" <<'EOF'
#!/usr/bin/env bash
echo "[fake open] $*"
exit 0
EOF
chmod +x "$S1/bin/open"

MARKER1="$WORK/marker1"
LOG1="$S1/update.log"
touch "$MARKER1"

# Fake "parent" process to wait on.
sleep 30 &
PARENT_PID=$!

PATH="$S1/bin:$PATH" run_helper_and_wait "$DMG1" "$S1/AppRoot/Magic Video Editor.app" "$PARENT_PID" "$MARKER1" "$LOG1" >/dev/null
HELPER_PID_1=$!

sleep 1
if wait_for_log "$LOG1" "waiting for parent marker file to disappear" 10; then
  pass "s1: helper is waiting on the marker before touching anything"
else
  fail "s1: helper did not log the marker-wait step"
fi

if [ -f "$S1/AppRoot/Magic Video Editor.app/Contents/version.txt" ] && \
   grep -q "old-version" "$S1/AppRoot/Magic Video Editor.app/Contents/version.txt"; then
  pass "s1: old app untouched while waiting"
else
  fail "s1: old app was modified before the parent was confirmed gone"
fi

# Simulate the parent's real shutdown sequence: remove the marker, then die.
rm -f "$MARKER1"
kill "$PARENT_PID" 2>/dev/null
wait "$PARENT_PID" 2>/dev/null

if wait_for_log "$LOG1" "==== update_helper done ====" 20; then
  pass "s1: helper completed"
else
  fail "s1: helper did not complete in time"
  cat "$LOG1" 2>/dev/null
fi

if [ -f "$S1/AppRoot/Magic Video Editor.app/Contents/version.txt" ] && \
   grep -q "new-version" "$S1/AppRoot/Magic Video Editor.app/Contents/version.txt"; then
  pass "s1: old app replaced by new app"
else
  fail "s1: app was not swapped"
fi

if xattr -p com.apple.quarantine "$S1/AppRoot/Magic Video Editor.app" >/dev/null 2>&1; then
  fail "s1: quarantine attribute still present after swap"
else
  pass "s1: quarantine attribute removed"
fi

if grep -q "relaunching" "$LOG1" && grep -q "fake open" "$LOG1" 2>/dev/null; then
  pass "s1: relaunch invoked"
else
  # relaunch stdout is redirected into LOG_FILE via >>"$LOG_FILE" 2>&1
  if grep -q "\[fake open\]" "$LOG1"; then
    pass "s1: relaunch invoked (fake open captured in log)"
  else
    fail "s1: relaunch was not invoked"
  fi
fi

if ! find "$S1/AppRoot" -maxdepth 1 -name "*.rollback-*" | grep -q .; then
  pass "s1: no leftover rollback/backup dirs"
else
  fail "s1: leftover rollback backup dir found"
fi

if ! find "$S1" -maxdepth 1 -name ".mve-update-staging.*" | grep -q .; then
  pass "s1: no leftover staging dir"
else
  fail "s1: leftover staging dir found"
fi

# ---------------------------------------------------------------------
# Scenario 2: copy/staging sabotaged (install dir not writable) -- old app
# must be left completely untouched, helper exits non-zero.
# ---------------------------------------------------------------------
echo "--- scenario 2: staging sabotaged (install dir read-only) ---"
S2="$WORK/s2"
mkdir -p "$S2/AppRoot"
mkdir -p "$S2/AppRoot/Magic Video Editor.app/Contents/MacOS"
echo "old-version" >"$S2/AppRoot/Magic Video Editor.app/Contents/version.txt"
DMG2="$S2/update.dmg"
make_fake_dmg "new-version" "$DMG2"

MARKER2="$WORK/marker2"
LOG2="$S2/update.log"
# No marker/pid wait needed for this one -- start already "dead".
PARENT_PID2=99999999  # never a real pid; kill -0 fails immediately

chmod 555 "$S2/AppRoot"
/bin/bash "$HELPER" "$DMG2" "$S2/AppRoot/Magic Video Editor.app" "$PARENT_PID2" "$MARKER2" "$LOG2"
RC2=$?
chmod 755 "$S2/AppRoot"

if [ "$RC2" -ne 0 ]; then
  pass "s2: helper exited non-zero on staging failure"
else
  fail "s2: helper exited 0 despite staging failure"
fi

if grep -q "old-version" "$S2/AppRoot/Magic Video Editor.app/Contents/version.txt" 2>/dev/null; then
  pass "s2: old app left completely untouched"
else
  fail "s2: old app was modified despite staging failure"
fi

if grep -qi "staging dir" "$LOG2" 2>/dev/null; then
  pass "s2: failure logged"
else
  fail "s2: no log entry for the staging failure"
fi

# ---------------------------------------------------------------------
# Scenario 3: forced swap-in failure (test hook) -- rollback must restore
# the old app.
# ---------------------------------------------------------------------
echo "--- scenario 3: forced swap-in failure -> rollback ---"
S3="$WORK/s3"
mkdir -p "$S3/AppRoot"
mkdir -p "$S3/AppRoot/Magic Video Editor.app/Contents/MacOS"
echo "old-version" >"$S3/AppRoot/Magic Video Editor.app/Contents/version.txt"
DMG3="$S3/update.dmg"
make_fake_dmg "new-version" "$DMG3"

MARKER3="$WORK/marker3"
LOG3="$S3/update.log"
PARENT_PID3=99999998

MVE_UPDATE_HELPER_FORCE_SWAP_FAIL=1 /bin/bash "$HELPER" "$DMG3" "$S3/AppRoot/Magic Video Editor.app" "$PARENT_PID3" "$MARKER3" "$LOG3"
RC3=$?

if [ "$RC3" -ne 0 ]; then
  pass "s3: helper exited non-zero on forced swap failure"
else
  fail "s3: helper exited 0 despite forced swap failure"
fi

if [ -f "$S3/AppRoot/Magic Video Editor.app/Contents/version.txt" ] && \
   grep -q "old-version" "$S3/AppRoot/Magic Video Editor.app/Contents/version.txt"; then
  pass "s3: rollback restored the old app at the live path"
else
  fail "s3: old app missing/wrong after rollback"
fi

if grep -q "rollback succeeded" "$LOG3" 2>/dev/null; then
  pass "s3: rollback logged as succeeded"
else
  fail "s3: no rollback-succeeded log line"
fi

if ! find "$S3/AppRoot" -maxdepth 1 -name "*.rollback-*" | grep -q .; then
  pass "s3: no leftover rollback backup dir after successful rollback"
else
  fail "s3: leftover rollback backup dir after rollback"
fi

echo
echo "== results: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
