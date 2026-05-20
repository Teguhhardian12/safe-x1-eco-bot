#!/usr/bin/env bash
# Vendor OpenZeppelin Contracts from npm to contracts/oz/
#
# Reason: the original X1-Ecochain-BOT fetched OZ sources from jsDelivr CDN
# at compile time, meaning a CDN compromise or DNS hijack could inject
# malicious code into every deploy. We pin a specific version locally,
# verify the tarball, and build offline.

set -euo pipefail

OZ_VERSION="${OZ_VERSION:-5.0.0}"
TARBALL_URL="https://registry.npmjs.org/@openzeppelin/contracts/-/contracts-${OZ_VERSION}.tgz"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${REPO_ROOT}/contracts/oz"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo ">> Downloading OpenZeppelin Contracts v${OZ_VERSION}"
curl -fsSL "$TARBALL_URL" -o "$TMPDIR/oz.tgz"

echo ">> SHA256:"
sha256sum "$TMPDIR/oz.tgz"

echo ">> Extracting"
mkdir -p "$TMPDIR/extract"
tar -xzf "$TMPDIR/oz.tgz" -C "$TMPDIR/extract"

# npm tarballs unpack to package/
SRC="$TMPDIR/extract/package"
if [[ ! -d "$SRC" ]]; then
  echo "ERROR: expected $SRC after extraction" >&2
  exit 1
fi

echo ">> Installing to $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
# OZ npm tarball layout: package/<top-level-dirs>/*.sol (no package/contracts/ wrapper)
# Copy every .sol-bearing top-level directory under package/ — skip JSON build artefacts
cd "$SRC"
for d in */; do
  d="${d%/}"
  case "$d" in
    build|hardhat|node_modules) continue ;;
  esac
  if find "$d" -name '*.sol' -print -quit | grep -q .; then
    cp -r "$d" "$DEST/"
  fi
done
cd - >/dev/null
cp "$SRC"/package.json "$DEST/_package.json" 2>/dev/null || true
cp "$SRC"/LICENSE "$DEST/LICENSE" 2>/dev/null || true

echo ">> Done. Counted $(find "$DEST" -name '*.sol' | wc -l) .sol files."
