#!/usr/bin/env bash
# Wraps "dist/Magic Video Editor.app" into a distributable .dmg:
#   dist/Magic Video Editor.dmg
#   dist/Magic Video Editor.dmg.sha256
#
# Plain hdiutil (no create-dmg dependency) -- a temp read-write dmg staged
# with the .app + an /Applications symlink, then converted to a compressed
# read-only image. Run via `make dist-dmg` (usually after `make dist-app`).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
APP_NAME="Magic Video Editor"
APP_PATH="$DIST_DIR/$APP_NAME.app"
DMG_PATH="$DIST_DIR/$APP_NAME.dmg"
VOL_NAME="$APP_NAME"

if [ ! -d "$APP_PATH" ]; then
  echo "error: $APP_PATH not found -- run 'make dist-app' first" >&2
  exit 1
fi

STAGE_DIR="$(mktemp -d /tmp/mve-dmg-stage.XXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT

echo "Staging dmg contents..."
cp -R "$APP_PATH" "$STAGE_DIR/$APP_NAME.app"
ln -s /Applications "$STAGE_DIR/Applications"

rm -f "$DMG_PATH"

echo "Building $DMG_PATH ..."
hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$STAGE_DIR" \
  -fs HFS+ \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$DMG_PATH"

echo "Computing sha256 sidecar..."
shasum -a 256 "$DMG_PATH" | awk '{print $1}' > "$DMG_PATH.sha256"
echo "  $(cat "$DMG_PATH.sha256")  $(basename "$DMG_PATH")"

echo "Done: $DMG_PATH"
