#!/usr/bin/env bash
# Build macOS OSpRad.app via PyInstaller. Must run on macOS - PyInstaller doesn't cross-compile.
#
# Usage: packaging/macos/build_macos.sh
# Requires: pip install -r app/requirements.txt pyinstaller

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG_DIR="$REPO_ROOT/packaging/macos"
DIST_DIR="$PKG_DIR/dist"
BUILD_DIR="$PKG_DIR/build"

rm -rf "$DIST_DIR" "$BUILD_DIR" "$PKG_DIR/OSpRad.spec"

pyinstaller --name OSpRad --windowed \
    --add-data "$REPO_ROOT/app/calibration_data.csv:." \
    --icon "$REPO_ROOT/packaging/assets/ospradicon.png" \
    --paths "$REPO_ROOT/app" \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR" \
    --specpath "$PKG_DIR" \
    "$REPO_ROOT/app/OSpRad.py"

echo "Built: $DIST_DIR/OSpRad.app"
