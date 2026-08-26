#!/usr/bin/env bash
# Build Linux onedir PyInstaller bundle, then wrap in an AppImage if appimagetool is
# on PATH (else plain tarball).
#
# Usage: packaging/linux/build_linux.sh
# Requires: pip install -r app/requirements.txt pyinstaller

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG_DIR="$REPO_ROOT/packaging/linux"
DIST_DIR="$PKG_DIR/dist"
BUILD_DIR="$PKG_DIR/build"

rm -rf "$DIST_DIR" "$BUILD_DIR" "$PKG_DIR/OSpRad.spec"

pyinstaller --name OSpRad --onedir --windowed \
    --add-data "$REPO_ROOT/app/calibration_data.csv:." \
    --icon "$REPO_ROOT/packaging/assets/ospradicon.png" \
    --paths "$REPO_ROOT/app" \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR" \
    --specpath "$PKG_DIR" \
    "$REPO_ROOT/app/OSpRad.py"

if command -v appimagetool >/dev/null 2>&1; then
    APPDIR="$PKG_DIR/OSpRad.AppDir"
    rm -rf "$APPDIR"
    mkdir -p "$APPDIR/usr/bin"
    cp -r "$DIST_DIR/OSpRad/." "$APPDIR/usr/bin/"
    cp "$REPO_ROOT/packaging/assets/ospradicon.png" "$APPDIR/ospradicon.png"
    cp "$PKG_DIR/OSpRad.desktop" "$APPDIR/OSpRad.desktop"
    cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "$HERE/usr/bin/OSpRad" "$@"
EOF
    chmod +x "$APPDIR/AppRun"
    appimagetool "$APPDIR" "$DIST_DIR/OSpRad-x86_64.AppImage"
    echo "Built AppImage: $DIST_DIR/OSpRad-x86_64.AppImage"
else
    tar -C "$DIST_DIR" -czf "$DIST_DIR/OSpRad-linux-x86_64.tar.gz" OSpRad
    cat <<EOF
appimagetool not found on PATH - built a plain tarball instead:
  $DIST_DIR/OSpRad-linux-x86_64.tar.gz
Extract it and run the OSpRad binary inside the extracted OSpRad/ folder.
For an AppImage with desktop-menu integration, install appimagetool
(https://github.com/AppImage/appimagetool) and re-run this script.
EOF
fi
