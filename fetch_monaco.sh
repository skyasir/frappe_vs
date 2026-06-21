#!/usr/bin/env bash
# Vendor a local copy of Monaco for the self-hosted (offline) fallback.
# Downloads the pinned version's `min/vs` tree into frappe_vs/public/monaco/vs.
set -euo pipefail

VERSION="0.52.2"   # keep in sync with FVS_MONACO_VERSION in frappe_vs.js
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/frappe_vs/public/monaco"
TARBALL="https://registry.npmjs.org/monaco-editor/-/monaco-editor-${VERSION}.tgz"

echo "Downloading monaco-editor@${VERSION}…"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fSL "$TARBALL" -o "$TMP/monaco.tgz"
tar -xzf "$TMP/monaco.tgz" -C "$TMP"

mkdir -p "$DEST"
rm -rf "$DEST/vs"
cp -R "$TMP/package/min/vs" "$DEST/vs"

echo "Done. Local Monaco available at /assets/frappe_vs/monaco/vs/loader.js"
echo "Run 'bench build --app frappe_vs' (or restart the bench) to serve it."
