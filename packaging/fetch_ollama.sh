#!/usr/bin/env bash
# Fetches the latest official Ollama darwin (arm64/universal) standalone
# runtime from GitHub Releases into packaging/vendor/ollama/, for bundling
# into the .app (v6 packaging Option B). Also drops the upstream MIT LICENSE
# alongside it -- that file is the one thing in packaging/vendor/ollama/ that
# stays committed; everything else (the binary + its dylibs) is gitignored
# and must be re-fetched by whoever builds a release (see `make dist-app`).
#
# Usage: bash packaging/fetch_ollama.sh
#
# Verification: this script downloads the release's own sha256sum.txt and
# checks the ollama-darwin.tgz asset against it before extracting -- do not
# remove that check.
set -euo pipefail

REPO="ollama/ollama"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="$SCRIPT_DIR/vendor/ollama"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "[fetch_ollama] looking up latest ${REPO} release..."
API_JSON="$TMP_DIR/release.json"
curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" -o "$API_JSON"

TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['tag_name'])" "$API_JSON")
ASSET_URL=$(python3 - "$API_JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for a in data["assets"]:
    if a["name"] == "ollama-darwin.tgz":
        print(a["browser_download_url"])
        break
PY
)
CHECKSUMS_URL=$(python3 - "$API_JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for a in data["assets"]:
    if a["name"] == "sha256sum.txt":
        print(a["browser_download_url"])
        break
PY
)

if [ -z "$ASSET_URL" ]; then
    echo "[fetch_ollama] could not find ollama-darwin.tgz in release $TAG" >&2
    exit 1
fi

echo "[fetch_ollama] release: $TAG"
echo "[fetch_ollama] downloading $ASSET_URL"
curl -fL --progress-bar -o "$TMP_DIR/ollama-darwin.tgz" "$ASSET_URL"

if [ -n "$CHECKSUMS_URL" ]; then
    echo "[fetch_ollama] verifying checksum..."
    curl -fsSL -o "$TMP_DIR/sha256sum.txt" "$CHECKSUMS_URL"
    EXPECTED=$(grep 'ollama-darwin.tgz' "$TMP_DIR/sha256sum.txt" | awk '{print $1}')
    ACTUAL=$(shasum -a 256 "$TMP_DIR/ollama-darwin.tgz" | awk '{print $1}')
    if [ -z "$EXPECTED" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
        echo "[fetch_ollama] CHECKSUM MISMATCH: expected=${EXPECTED:-<none>} actual=$ACTUAL" >&2
        exit 1
    fi
    echo "[fetch_ollama] checksum OK ($ACTUAL)"
else
    echo "[fetch_ollama] WARNING: no sha256sum.txt asset found for $TAG -- skipping checksum verification" >&2
fi

echo "[fetch_ollama] extracting into $VENDOR_DIR"
rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"
tar -xzf "$TMP_DIR/ollama-darwin.tgz" -C "$VENDOR_DIR"
chmod +x "$VENDOR_DIR/ollama"

echo "[fetch_ollama] fetching upstream MIT LICENSE ($TAG)"
curl -fsSL -o "$VENDOR_DIR/LICENSE" \
    "https://raw.githubusercontent.com/${REPO}/${TAG}/LICENSE"

cat > "$VENDOR_DIR/VERSION" <<EOF
${TAG}
source: ${ASSET_URL}
sha256: ${ACTUAL:-unknown}
EOF

echo "[fetch_ollama] done: $VENDOR_DIR/ollama ($TAG)"
"$VENDOR_DIR/ollama" --version || true
