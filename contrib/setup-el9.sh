#!/usr/bin/env bash
# AlmaLinux/RHEL 9-family host setup for v-EMU (0.4.0 development path).
#
# The main setup.sh and the .run release artifact target Fedora 45 and the
# RHEL-family 10.3 baseline. This helper adds the EL9 bootstrap that the 0.4.0
# tree does not have yet, then calls the same setup.sh:
#
#   - install Python 3.11 (Alma/RHEL 9 still defaults to Python 3.9);
#   - enable CRB and install libvirt-devel + python3.11-devel;
#   - install libvirt-python for the system Python used by vemud;
#   - install image-builder/osbuild for the Image Builder page;
#   - optionally build FreeRDP 3.33.1 from source for seamless mode.
#
# It does not modify setup.sh, setup.run or the release tree. This is a contrib
# helper for EL9, tested on AlmaLinux 9.9. Alma 9.9 is an optional installation-test path, not a primary host;
# see docs/design/alma-9.9-spike.md.
#
# Usage:
#   ./contrib/setup-el9.sh --precheck
#   ./contrib/setup-el9.sh
#   ./contrib/setup-el9.sh --full --with-freerdp
#   ./contrib/setup-el9.sh --uninstall
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODE=standard
WITH_FREERDP=0
WITH_IMAGE_BUILDER=1
UNINSTALL_ARGS=()

usage() {
    cat <<'EOF'
AlmaLinux/RHEL 9 host bootstrap for v-EMU.

Usage:
  ./contrib/setup-el9.sh --precheck
  ./contrib/setup-el9.sh                 Install core v-EMU (standard)
  ./contrib/setup-el9.sh --full          Also install aarch64/riscv64 TCG
  ./contrib/setup-el9.sh --with-freerdp   Build FreeRDP 3.33.1 after setup
  ./contrib/setup-el9.sh --uninstall     Remove v-EMU, keep user data
  ./contrib/setup-el9.sh --no-image-builder
                                         Skip image-builder/osbuild packages

Options are passed to the main setup.sh where applicable. Run as your normal
user; sudo is used for packages and system steps. The main setup.sh and the
release .run are not changed by this helper.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --precheck) MODE=precheck ;;
        --standard) MODE=standard ;;
        --full) MODE=full ;;
        --with-freerdp) WITH_FREERDP=1 ;;
        --no-image-builder) WITH_IMAGE_BUILDER=0 ;;
        --uninstall) MODE=uninstall ;;
        --yes|-y) UNINSTALL_ARGS+=("--yes") ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $(id -u) -eq 0 ]]; then
    echo "Run this helper as your normal user, not root." >&2
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
        echo "Use setup.sh on Fedora/RHEL 10.x." >&2
        exit 1
        ;;
esac
if [[ $(uname -m) != "x86_64" ]]; then
    echo "This helper was tested on x86_64 only; detected $(uname -m)." >&2
    exit 1
fi

if [[ "$MODE" == "uninstall" ]]; then
    exec "$ROOT/setup.sh" --uninstall "${UNINSTALL_ARGS[@]}"
fi

PYTHON_BIN=/usr/bin/python3.11
if [[ ! -x "$PYTHON_BIN" ]]; then
    if [[ "$MODE" == "precheck" ]]; then
        echo "Python 3.11 is not installed. Install python3.11 and python3.11-pip." >&2
        exit 1
    fi
    SUDO=(sudo)
    if ! "${SUDO[@]}" -n true 2>/dev/null; then
        echo "Requesting sudo privileges for the EL9 bootstrap..."
        "${SUDO[@]}" -v
    fi
    "${SUDO[@]}" dnf install -y python3.11 python3.11-pip
fi

if [[ "$MODE" == "precheck" ]]; then
    "$PYTHON_BIN" --version
    if ! "$PYTHON_BIN" -c 'import libvirt' >/dev/null 2>&1; then
        echo "python3.11 libvirt binding is missing (install libvirt-devel and libvirt-python)." >&2
    fi
    exec env PYTHON="$PYTHON_BIN" SYSTEM_PYTHON="$PYTHON_BIN" \
        "$ROOT/setup.sh" --precheck
fi

SUDO=(sudo)
if ! "${SUDO[@]}" -n true 2>/dev/null; then
    echo "Requesting sudo privileges for the EL9 bootstrap..."
    "${SUDO[@]}" -v
fi

if ! dnf repolist --enabled 2>/dev/null | grep -qE '^crb([[:space:]]|$)'; then
    echo "Enabling CRB for libvirt-devel..."
    "${SUDO[@]}" dnf config-manager --set-enabled crb
fi

PACKAGES=(libvirt-devel python3.11-devel)
if (( WITH_IMAGE_BUILDER )); then
    PACKAGES+=(image-builder osbuild osbuild-depsolve-dnf)
fi
missing=()
for pkg in "${PACKAGES[@]}"; do
    rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if (( ${#missing[@]} )); then
    echo "Installing EL9 bootstrap packages: ${missing[*]}"
    "${SUDO[@]}" dnf install -y "${missing[@]}"
fi

if ! "$PYTHON_BIN" -c 'import libvirt' >/dev/null 2>&1; then
    echo "Installing libvirt-python for system Python 3.11 (vemud)..."
    "${SUDO[@]}" "$PYTHON_BIN" -m pip install --disable-pip-version-check libvirt-python
fi

CORE_ARGS=("--$MODE")
echo "Running the main setup.sh for EL9..."
env PYTHON="$PYTHON_BIN" SYSTEM_PYTHON="$PYTHON_BIN" \
    "$ROOT/setup.sh" "${CORE_ARGS[@]}"

if (( WITH_FREERDP )); then
    echo
    echo "Building FreeRDP 3.x for seamless mode..."
    "$ROOT/contrib/setup-freerdp-el9.sh"
fi

echo
echo "EL9 setup finished. Reboot or log out/in for group and socket changes."
echo "For seamless, use: vemu seamless prepare <domain> --username <user>"
