#!/usr/bin/env bash
# Build + install the Aldur Appraiser Flatpak locally.
#
# The app is installed from a pre-built universal wheel (pure Python), so the
# offline flatpak-builder sandbox never needs a Python build backend. This
# script (re)builds that wheel, then runs flatpak-builder.
#
# Prerequisites (installed from Flathub):
#   flatpak install flathub org.kde.Platform//6.9 org.kde.Sdk//6.9 \
#                           io.qt.PySide.BaseApp//6.9
#
# To refresh pinned deps after changing requirements.txt:
#   python flatpak-pip-generator --runtime org.kde.Sdk//6.9 -r requirements.txt \
#     -o python-deps --yaml --checker-data --wheel-arches x86_64 \
#     --prefer-wheels rapidfuzz,numpy,pillow,shapely,pyclipper,opencv-python,onnxruntime,protobuf,pyyaml
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
app_id="io.github.t0nd3.AldurAppraiser"

ver="$(grep -E '^version' "$repo/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
wheel="aldur_appraiser-${ver}-py3-none-any.whl"
echo ">> building wheel $wheel"
rm -f "$repo"/dist/aldur_appraiser-*.whl
python -m pip wheel "$repo" --no-deps -w "$repo/dist" >/dev/null

if ! grep -q "$wheel" "$here/$app_id.yaml"; then
  echo "!! manifest references a different wheel than $wheel" >&2
  echo "   update the version in $app_id.yaml to match pyproject ($ver)" >&2
  exit 1
fi

echo ">> flatpak-builder"
cd "$here"
flatpak-builder --user --force-clean --install _build "$app_id.yaml"
echo ">> done. Run:  flatpak run $app_id"
