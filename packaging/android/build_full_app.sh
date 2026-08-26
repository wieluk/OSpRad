#!/usr/bin/env bash
# Build the OSpRad app as an Android .apk via pyside6-android-deploy
# (buildozer/python-for-android). Bundles PySide6, numpy, matplotlib, usb4a,
# usbserial4a, pyjnius and every app/*.py module. scipy is excluded (its p4a recipe
# doesn't build); see calibration_wizard.py and monitor_calibration.py for the
# numpy only fallbacks.
#
# pyside6-android-deploy regenerates buildozer.spec / pysidedeploy.spec every run, so
# hand editing them doesn't stick. Everything below is sed patched into the installed
# tool, scoped to the throwaway --work-dir venv:
#   requirements   numpy/matplotlib/usb4a/usbserial4a/pyjnius + the tool's hardcoded
#                  list (pyjnius because usb4a/usbserial4a import jnius at runtime
#                  and p4a cannot infer that).
#   include_exts   +csv (default source.include_exts excludes .csv, so the bundled
#                  calibration_data.csv would otherwise be missing from the APK).
#   icon.filename  use OSpRad's launcher icon instead of PySide's generic one.
#   version        stamp OSPRAD_VERSION into the APK.
#   p4a.commit     pin p4a to the last commit before its default interpreter became
#                  3.14 (the PySide6/shiboken6 Android wheels are cp311 only; the
#                  mismatch only fails at launch with UnsatisfiedLinkError).
#   local_recipes  copy packaging/android/local_recipes/ in at a point after the
#                  tool's own rmtree cleanup, which would otherwise wipe them.
#   minapi/ndk_api raised to 24, which numpy's recipe requires.
#
# Also prebuilds a usbserial4a wheel (PyPI ships sdist only; p4a's pip runs with
# --only-binary=:all:) and installs the patch/autotools/cmake/bzip2 toolchain via
# Homebrew if present or apt-get otherwise.
#
# Usage: packaging/android/build_full_app.sh [work_dir]
#   Output: <work_dir>/osprad_full/*.apk
#   Env:    OSPRAD_VERSION - version stamped into the APK (default: app/_version.py)
#   IMPORTANT: keep work_dir short. Recipes invoke a hostpython3 pip whose shebang
#   embeds the full work_dir path; past the kernel's 127 char shebang limit that
#   fails as "Exec format error".

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK_DIR="${1:-$(mktemp -d)}"
# Everything this script downloads is pinned. A floating version here means a build
# that worked yesterday can fail today with nothing changed in the repo, so bump these
# deliberately. PYSIDE_VERSION must stay in step with app/requirements.txt.
PYSIDE_VERSION="6.11.2"
NDK_VERSION="r27c"
JDK_VERSION="jdk-17.0.20.1+1"
BUILDOZER_VERSION="1.5.0"
USBSERIAL4A_VERSION="0.4.0"
ARCH="aarch64"
OSPRAD_VERSION="${OSPRAD_VERSION:-$(sed -n "s/^__version__ = '\(.*\)'$/\1/p" "$REPO_ROOT/app/_version.py")}"

mkdir -p "$WORK_DIR"
echo "Work dir: $WORK_DIR"
echo "Version:  $OSPRAD_VERSION"
if [ "${#WORK_DIR}" -gt 40 ]; then
    echo "WARNING: work_dir is long (${#WORK_DIR} chars). A hostpython3 pip shebang" >&2
    echo "may exceed the kernel's 127 char limit. Prefer a short path." >&2
fi

echo "=== JDK $JDK_VERSION (Temurin, portable; no system install) ==="
mkdir -p "$WORK_DIR/jdk"
if [ -z "$(find "$WORK_DIR/jdk" -maxdepth 1 -mindepth 1 -type d -iname 'jdk*')" ]; then
    # /binary/version/<release> rather than /binary/latest/17/ga, so a new Temurin
    # point release can't silently change the toolchain under a reproducible build.
    curl -fsL -o "$WORK_DIR/jdk/jdk17.tar.gz" \
        "https://api.adoptium.net/v3/binary/version/${JDK_VERSION//+/%2B}/linux/x64/jdk/hotspot/normal/eclipse"
    tar xzf "$WORK_DIR/jdk/jdk17.tar.gz" -C "$WORK_DIR/jdk"
    rm -f "$WORK_DIR/jdk/jdk17.tar.gz"
fi
JAVA_HOME=$(find "$WORK_DIR/jdk" -maxdepth 1 -mindepth 1 -type d -iname "jdk*" | head -1)

echo "=== Android NDK $NDK_VERSION ==="
mkdir -p "$WORK_DIR/android_sdk"
if [ ! -d "$WORK_DIR/android_sdk/android-ndk-$NDK_VERSION" ]; then
    curl -L --progress-bar -o "$WORK_DIR/android_sdk/ndk.zip" \
        "https://dl.google.com/android/repository/android-ndk-$NDK_VERSION-linux.zip"
    unzip -q "$WORK_DIR/android_sdk/ndk.zip" -d "$WORK_DIR/android_sdk"
    rm -f "$WORK_DIR/android_sdk/ndk.zip"
fi

echo "=== PySide6/shiboken6 Android wheels ==="
# pyside6-android-deploy reads the target arch out of the wheel *filename*, so these
# have to keep their upstream names. Renaming them to something tidier makes it fail
# with "PySide wheel corrupted. Wheel name should end with platform name".
mkdir -p "$WORK_DIR/wheels"
PYSIDE_WHEEL="pyside6-$PYSIDE_VERSION-$PYSIDE_VERSION-cp311-cp311-android_$ARCH.whl"
SHIBOKEN_WHEEL="shiboken6-$PYSIDE_VERSION-$PYSIDE_VERSION-cp311-cp311-android_$ARCH.whl"
[ -f "$WORK_DIR/wheels/$PYSIDE_WHEEL" ] || curl -fsL -o "$WORK_DIR/wheels/$PYSIDE_WHEEL" \
    "https://download.qt.io/official_releases/QtForPython/pyside6/$PYSIDE_WHEEL"
[ -f "$WORK_DIR/wheels/$SHIBOKEN_WHEEL" ] || curl -fsL -o "$WORK_DIR/wheels/$SHIBOKEN_WHEEL" \
    "https://download.qt.io/official_releases/QtForPython/shiboken6/$SHIBOKEN_WHEEL"

echo "=== Python 3.11 host venv (buildozer refuses newer Python) ==="
PY311=""
for cand in python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>/dev/null; then
        PY311=$(command -v "$cand")
        break
    fi
done
if [ -z "$PY311" ]; then
    PY311=$(uv python find 3.11 2>/dev/null || true)
fi
if [ -z "$PY311" ]; then
    uv python install 3.11
    PY311=$(uv python find 3.11)
fi
"$PY311" -m venv --system-site-packages "$WORK_DIR/py311"
# PySide6 here is the *host* tooling that provides pyside6-android-deploy; it must match
# the Android wheels downloaded above or the deploy scripts and wheels can disagree.
"$WORK_DIR/py311/bin/python3" -m pip install --quiet \
    "PySide6==$PYSIDE_VERSION" "jinja2==3.1.4" "pkginfo==1.11.2" "tqdm==4.67.1" \
    "packaging==24.1" pip "buildozer==$BUILDOZER_VERSION" cython==0.29.33

echo "=== Patch: full app requirements list ==="
BUILDOZER_PY="$WORK_DIR/py311/lib/python3.11/site-packages/PySide6/scripts/deploy_lib/android/buildozer.py"
sed -i 's/"python3,shiboken6,PySide6"/"python3,shiboken6,PySide6,numpy,matplotlib,usb4a,usbserial4a,pyjnius"/' "$BUILDOZER_PY"
grep -q "usbserial4a,pyjnius" "$BUILDOZER_PY" || { echo "requirements patch failed"; exit 1; }

echo "=== Patch: bundle calibration_data.csv (default source.include_exts excludes .csv) ==="
sed -i 's/include_exts = f"{include_exts},qml,js"/include_exts = f"{include_exts},qml,js,csv"/' "$BUILDOZER_PY"
grep -q ',qml,js,csv"' "$BUILDOZER_PY" || { echo "include_exts patch failed"; exit 1; }

echo "=== Patch: use OSpRad's own launcher icon ==="
sed -i "s|self.set_value(\"app\", \"icon.filename\", pysidedeploy_config.icon)|self.set_value(\"app\", \"icon.filename\", \"$REPO_ROOT/packaging/assets/ospradicon.png\")|" "$BUILDOZER_PY"
grep -q "ospradicon.png" "$BUILDOZER_PY" || { echo "icon patch failed"; exit 1; }

echo "=== Patch: stamp version $OSPRAD_VERSION into the APK ==="
sed -i "/self.set_value(\"app\", \"icon.filename\"/a\\        self.set_value(\"app\", \"version\", \"$OSPRAD_VERSION\")" "$BUILDOZER_PY"
grep -q "\"version\", \"$OSPRAD_VERSION\"" "$BUILDOZER_PY" || { echo "version patch failed"; exit 1; }

echo "=== Patch: pin p4a to the last commit targeting Python 3.11 (see header) ==="
sed -i '/self.set_value("app", "p4a.branch", "develop")/a\        self.set_value("app", "p4a.commit", "6b66944a2f51e0c848c7ac51e04a771324067ecc")' "$BUILDOZER_PY"
grep -q "p4a.commit" "$BUILDOZER_PY" || { echo "p4a.commit patch failed"; exit 1; }

echo "=== Patch: inject local recipes ==="
# Staging them before invoking the tool would not survive its own rmtree cleanup; this
# patch site runs after that cleanup and after recipe_dir has been created.
sed -i "/self.set_value('app', \"p4a.local_recipes\", str(pysidedeploy_config.recipe_dir))/a\\
        import shutil as _osprad_shutil\\
        for _osprad_recipe in ('python3', 'kiwisolver', 'matplotlib', 'numpy', 'Pillow', 'pyjnius'):\\
            _osprad_shutil.copytree('$REPO_ROOT/packaging/android/local_recipes/' + _osprad_recipe, str(pysidedeploy_config.recipe_dir / _osprad_recipe), dirs_exist_ok=True)" "$BUILDOZER_PY"
grep -q "_osprad_shutil" "$BUILDOZER_PY" || { echo "local recipe injection patch failed"; exit 1; }

echo "=== Patch: raise buildozer's default minapi/ndk_api to 24 (numpy's recipe requires it) ==="
DEFAULT_SPEC="$WORK_DIR/py311/lib/python3.11/site-packages/buildozer/default.spec"
sed -i 's/^#android.minapi = 21/android.minapi = 24/' "$DEFAULT_SPEC"
sed -i 's/^#android.ndk_api = 21/android.ndk_api = 24/' "$DEFAULT_SPEC"
grep -q "^android.minapi = 24" "$DEFAULT_SPEC" || { echo "minapi patch failed"; exit 1; }

echo "=== Prebuild a local wheel for usbserial4a (PyPI ships sdist only, pip needs a wheel) ==="
mkdir -p "$WORK_DIR/wheelhouse"
"$WORK_DIR/py311/bin/python3" -m pip wheel "usbserial4a==$USBSERIAL4A_VERSION" \
    --no-deps -w "$WORK_DIR/wheelhouse" --quiet

echo "=== bzip2 dev headers, patch, autotools, cmake ==="
if command -v brew >/dev/null 2>&1; then
    for pkg in bzip2 gpatch autoconf automake libtool cmake; do
        brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg"
    done
    BZ2_PREFIX=$(brew --prefix bzip2)
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    # libltdl-dev, not libtool, ships /usr/share/aclocal/ltdl.m4 on Debian/Ubuntu, and
    # that is where LT_SYS_SYMBOL_USCORE is defined. libffi's configure.ac uses it but
    # libtoolize won't copy ltdl.m4 in (libffi doesn't use libltdl), so without this
    # package its autogen.sh dies on "possibly undefined macro: LT_SYS_SYMBOL_USCORE".
    sudo apt-get install -y -qq patch autoconf automake libtool libltdl-dev cmake libbz2-dev unzip
    BZ2_PREFIX="/usr"
else
    echo "Neither brew nor apt-get found. Install patch/autoconf/automake/libtool/cmake" >&2
    echo "and bzip2 dev headers manually, then re-run." >&2
    exit 1
fi

echo "=== Staging app source ==="
mkdir -p "$WORK_DIR/osprad_full"
cp "$REPO_ROOT/app/OSpRad.py" "$WORK_DIR/osprad_full/main.py"
for f in analysis.py calibration.py calibration_io.py calibration_wizard.py datalog.py \
         file_io.py monitor_calibration.py plotting.py qt_worker.py serial_io.py touch.py ui.py \
         _version.py _icon_bundled.py _calibration_data_bundled.py; do
    cp "$REPO_ROOT/app/$f" "$WORK_DIR/osprad_full/$f"
done
cp "$REPO_ROOT/app/calibration_data.csv" "$WORK_DIR/osprad_full/calibration_data.csv"

# p4a fetches each recipe's source from whatever URL the recipe hardcodes, retries four
# times over ~15s and then fails the whole build. download.savannah.gnu.org (freetype)
# goes down for hours at a time and has killed otherwise good builds, so seed p4a's
# package cache from a mirror first. p4a skips downloading when both the tarball and its
# .mark-<file> marker are already there.
#
# The sha256 is pinned and checked, so a mirror serving something else can't slip a
# different tarball into the build. Any failure here is non fatal: the build just falls
# back to p4a's own download, which is what would have happened anyway.
seed_recipe_source() {
    local recipe="$1" filename="$2" sha256="$3" url="$4"
    local dir="$WORK_DIR/osprad_full/.buildozer/android/platform/build-arm64-v8a/packages/$recipe"
    mkdir -p "$dir"
    [ -f "$dir/.mark-$filename" ] && [ -f "$dir/$filename" ] && return 0
    echo "Preseeding $recipe source from a mirror"
    if ! curl -fsL --retry 3 --connect-timeout 20 -o "$dir/$filename.part" "$url"; then
        echo "WARNING: could not pre-seed $recipe. Leaving it to p4a." >&2
        rm -f "$dir/$filename.part"
        return 0
    fi
    if [ "$(sha256sum "$dir/$filename.part" | cut -d' ' -f1)" != "$sha256" ]; then
        echo "WARNING: $recipe mirror checksum mismatch. Discarding, leaving it to p4a." >&2
        rm -f "$dir/$filename.part"
        return 0
    fi
    mv "$dir/$filename.part" "$dir/$filename"
    touch "$dir/.mark-$filename"
    echo "Seeded $recipe ($filename)"
}

echo "=== Preseed flaky recipe downloads ==="
seed_recipe_source freetype freetype-2.10.1.tar.gz \
    3a60d391fd579440561bf0e7f31af2222bc610ad6ce4d9d7bd2165bca8669110 \
    "https://downloads.sourceforge.net/project/freetype/freetype2/2.10.1/freetype-2.10.1.tar.gz"

echo "=== Building APK ==="
export JAVA_HOME
export TMPDIR="${TMPDIR:-$WORK_DIR/tmp}"
mkdir -p "$TMPDIR"
export PIP_FIND_LINKS="$WORK_DIR/wheelhouse"
export PATH="$WORK_DIR/py311/bin:/home/linuxbrew/.linuxbrew/bin:$JAVA_HOME/bin:$PATH"
export CPPFLAGS="-I$BZ2_PREFIX/include ${CPPFLAGS:-}"
export LDFLAGS="-L$BZ2_PREFIX/lib ${LDFLAGS:-}"
export PKG_CONFIG_PATH="$BZ2_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export LD_LIBRARY_PATH="$BZ2_PREFIX/lib:${LD_LIBRARY_PATH:-}"

cd "$WORK_DIR/osprad_full"
# `yes` keeps writing after the deploy tool exits, so it always dies on EPIPE. Under
# pipefail that becomes the pipeline's status and set -e aborts the script *after* a
# successful build, so scope pipefail off here. The pipeline then reports the deploy
# tool's own status, which set -e still catches.
set +o pipefail
yes | pyside6-android-deploy -f \
    --name OSpRad \
    --wheel-pyside "$WORK_DIR/wheels/$PYSIDE_WHEEL" \
    --wheel-shiboken "$WORK_DIR/wheels/$SHIBOKEN_WHEEL" \
    --ndk-path "$WORK_DIR/android_sdk/android-ndk-$NDK_VERSION"
set -o pipefail

# maxdepth 2 covers both the project dir and buildozer's bin/ subdirectory; it stays
# shallow enough to skip the unversioned intermediate APK deep inside .buildozer.
apks=$(find "$WORK_DIR/osprad_full" -maxdepth 2 -iname "*.apk")

# Don't trust the deploy tool's exit status: it prints a traceback for a failed
# buildozer run and still exits 0, so set -e never fires and the script would report
# success with no APK. The failure then surfaced a step later as "no APK produced",
# pointing at the wrong place. Check for the artifact itself instead.
if [ -z "$apks" ]; then
    echo "ERROR: the build produced no APK. See the buildozer output above for the" >&2
    echo "real error (a recipe download failing is the usual cause)." >&2
    exit 1
fi

echo "Done. APK(s):"
echo "$apks"
