#!/usr/bin/env bash
# Build FreeRDP 3.x from source on AlmaLinux/RHEL 9 for v-EMU seamless mode.
#
# AlmaLinux 9.9 ships FreeRDP 2.11.7, while v-EMU's RemoteApp/RAIL support
# requires FreeRDP >= 3.26. This script builds a current FreeRDP client into
# /usr/local and leaves the distribution's 2.x package installed. /usr/local/bin
# normally wins in PATH, so v-EMU finds the new xfreerdp first.
#
# This is a contrib helper, not an official host tier. It was tested on
# AlmaLinux 9.9 (x86_64) with FreeRDP 3.33.1. The client is built for X11
# (Xwayland is enough for a Wayland desktop), without audio, printing,
# smartcard, USB redirection or Kerberos. Rebuild with more options below if
# those features are needed.
#
# Usage:
#   ./contrib/setup-freerdp-el9.sh --precheck
#   ./contrib/setup-freerdp-el9.sh
#   ./contrib/setup-freerdp-el9.sh --clean
#   FREERDP_VERSION=3.33.1 FREERDP_PREFIX=/usr/local \
#     ./contrib/setup-freerdp-el9.sh
set -euo pipefail

TAG=${FREERDP_VERSION:-3.33.1}
PREFIX=${FREERDP_PREFIX:-/usr/local}
BUILD_ROOT=${FREERDP_BUILD_ROOT:-$HOME/.cache/vemu-freerdp}
MODE=install

usage() {
    cat <<'EOF'
Build FreeRDP 3.x for v-EMU seamless mode on AlmaLinux/RHEL 9.

Options:
  --precheck  Report the current client, OS and missing build packages.
  --clean     Remove the downloaded source and build tree.
  -h, --help  Show this help.

Environment:
  FREERDP_VERSION     FreeRDP tag to build (default: 3.33.1)
  FREERDP_PREFIX      Install prefix (default: /usr/local)
  FREERDP_BUILD_ROOT  Source/build directory
                      (default: ~/.cache/vemu-freerdp)

The script needs sudo for dnf and for installing into the prefix. It does not
remove the distribution's FreeRDP 2.x package, and it does not change v-EMU.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --precheck) MODE=precheck ;;
        --clean) MODE=clean ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $(id -u) -eq 0 ]]; then
    echo "Run this script as your normal user, not root." >&2
    echo "It invokes sudo for dnf and the install step." >&2
    exit 1
fi

if [[ ! -r /etc/os-release ]]; then
    echo "Cannot read /etc/os-release." >&2
    exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
FAMILY=" ${ID:-} ${ID_LIKE:-} "
case "$FAMILY" in
    *almalinux*|*rhel*|*rocky*|*centos*) ;;
    *)
        echo "This helper is for AlmaLinux/RHEL 9-family hosts." >&2
        echo "Detected: ${PRETTY_NAME:-unknown}" >&2
        exit 1
        ;;
esac
case "${VERSION_ID:-}" in
    9.*) ;;
    *)
        echo "This helper targets EL9; detected ${VERSION_ID:-unknown}." >&2
        echo "Use the distribution FreeRDP on newer hosts." >&2
        exit 1
        ;;
esac
if [[ $(uname -m) != "x86_64" ]]; then
    echo "This helper was tested on x86_64 only; detected $(uname -m)." >&2
    exit 1
fi
if [[ ! "$TAG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "FREERDP_VERSION must look like 3.33.1." >&2
    exit 1
fi
if [[ "$PREFIX" != /* ]]; then
    echo "FREERDP_PREFIX must be an absolute path." >&2
    exit 1
fi

PACKAGES=(
    cmake ninja-build gcc gcc-c++ make pkgconf-pkg-config tar curl
    openssl-devel uriparser-devel libicu-devel libusbx-devel
    libX11-devel libXext-devel libXinerama-devel libXcursor-devel
    libXrandr-devel libXi-devel libXfixes-devel libXrender-devel
    libxkbfile-devel libXdamage-devel libXcomposite-devel libXv-devel
    libXScrnSaver-devel libxkbcommon-devel
)

missing=()
for pkg in "${PACKAGES[@]}"; do
    rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done

existing=""
if command -v xfreerdp >/dev/null 2>&1; then
    existing=$(xfreerdp /version 2>&1 | head -1 || true)
fi
if command -v xfreerdp3 >/dev/null 2>&1; then
    existing="${existing}${existing:+; }$(xfreerdp3 /version 2>&1 | head -1 || true)"
fi

if [[ "$MODE" == "precheck" ]]; then
    echo "OS:       ${PRETTY_NAME:-unknown}"
    echo "Existing: ${existing:-none}"
    echo "Target:   FreeRDP $TAG -> $PREFIX"
    if (( ${#missing[@]} )); then
        printf 'Missing build packages: %s\n' "${missing[*]}"
        echo "Run the script without --precheck to install them."
        exit 1
    fi
    echo "Build packages present."
    exit 0
fi

if [[ "$MODE" == "clean" ]]; then
    rm -rf "$BUILD_ROOT"
    echo "Removed $BUILD_ROOT. The installed FreeRDP remains in $PREFIX."
    exit 0
fi

SUDO=(sudo)
if ! "${SUDO[@]}" -n true 2>/dev/null; then
    echo "Requesting sudo privileges for packages and installation..."
    "${SUDO[@]}" -v
fi

if (( ${#missing[@]} )); then
    echo "Installing build packages..."
    "${SUDO[@]}" dnf install -y "${missing[@]}"
else
    echo "Build packages already installed."
fi

mkdir -p "$BUILD_ROOT"
cd "$BUILD_ROOT"
ARCHIVE="FreeRDP-${TAG}.tar.gz"
SRC="FreeRDP-${TAG}"
if [[ ! -f "$ARCHIVE" ]]; then
    echo "Downloading FreeRDP ${TAG}..."
    curl -L --fail -o "$ARCHIVE" \
        "https://github.com/FreeRDP/FreeRDP/archive/refs/tags/${TAG}.tar.gz"
fi
if [[ ! -d "$SRC" ]]; then
    tar -xzf "$ARCHIVE"
fi
cd "$SRC"
rm -rf build
cmake -S . -B build -GNinja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DWITH_CLIENT=ON -DWITH_SERVER=OFF \
    -DWITH_X11=ON -DWITH_WAYLAND=OFF \
    -DWITH_FFMPEG=OFF -DWITH_SWSCALE=OFF \
    -DWITH_CUPS=OFF -DWITH_PULSE=OFF -DWITH_ALSA=OFF \
    -DWITH_PCSC=OFF -DWITH_KRB5=OFF -DWITH_USB=OFF -DWITH_FUSE=OFF \
    -DBUILD_TESTING=OFF
cmake --build build --parallel "$(nproc)"
"${SUDO[@]}" cmake --install build

echo
if [[ -x "$PREFIX/bin/xfreerdp" ]]; then
    echo "Installed: $PREFIX/bin/xfreerdp"
    if [[ -n "${DISPLAY:-}" || -e /tmp/.X11-unix/X0 ]]; then
        DISPLAY="${DISPLAY:-:0}" "$PREFIX/bin/xfreerdp" /version 2>&1 | head -1 || true
    else
        "$PREFIX/bin/xfreerdp" /version 2>&1 | head -1 || true
    fi
    detected=$(command -v xfreerdp 2>/dev/null || true)
    if [[ -n "$detected" && "$detected" != "$PREFIX/bin/xfreerdp" ]]; then
        echo "Warning: PATH resolves xfreerdp to $detected, not $PREFIX/bin/xfreerdp."
        echo "Put $PREFIX/bin first in PATH or set VEMU_XFREERDP=$PREFIX/bin/xfreerdp."
    fi
else
    echo "Install finished but $PREFIX/bin/xfreerdp is missing." >&2
    exit 1
fi
echo "v-EMU should now report FreeRDP >= 3.26; open Seamless Precheck in v-EMU."
