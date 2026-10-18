#!/usr/bin/env bash
# Build a distributable single-file .flatpak bundle for the GitHub release page.
#
# Users install it with:
#   flatpak install aldur-appraiser-<ver>-<arch>.flatpak
# The embedded --runtime-repo lets flatpak pull the KDE runtime + PySide BaseApp
# from Flathub automatically if they're missing. x86_64 only (the pinned wheels).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
app_id="io.github.t0nd3.AldurAppraiser"
ver="$(grep -E '^version' "$repo/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
arch="$(uname -m)"
out="$here/aldur-appraiser-${ver}-${arch}.flatpak"

wheel="aldur_appraiser-${ver}-py3-none-any.whl"
echo ">> building wheel $wheel"
rm -f "$repo"/dist/aldur_appraiser-*.whl
python -m pip wheel "$repo" --no-deps -w "$repo/dist" >/dev/null
grep -q "$wheel" "$here/$app_id.yaml" || { echo "!! manifest wheel != $wheel" >&2; exit 1; }

cd "$here"
echo ">> flatpak-builder (export to local ostree repo)"
flatpak-builder --force-clean --repo=_repo _build "$app_id.yaml"

echo ">> build-bundle"
flatpak build-bundle \
  --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
  _repo "$out" "$app_id" master

echo ">> bundle ready:"
ls -lh "$out"
