#!/bin/bash
# Example vEMU first-boot payload.
#
# Installs bat and btop, removes a conservative list of optional desktop
# packages when present, and writes a marker plus a log. It is intentionally
# non-interactive and idempotent so it can run from the first-boot TUI over a
# USB stick or an HTTP(S) URL.
#
# Fedora/RHEL-family only: Image Builder does not support Debian, Ubuntu or
# openSUSE hosts, so this payload deliberately uses dnf/rpm only.
set -euo pipefail

LOG=/var/log/vemu-post-setup-test.log
MARKER=/var/lib/vemu/post-setup-test.done

mkdir -p "$(dirname "$LOG")" "$(dirname "$MARKER")"
exec > >(tee -a "$LOG") 2>&1

echo "=== vEMU test post-setup: $(date -Is)"
echo "host=$(hostname) user=$(id -un)"

if ! command -v dnf >/dev/null 2>&1; then
    echo "This payload targets Fedora/RHEL-family images only (dnf/rpm)." >&2
    echo "Image Builder does not support Debian, Ubuntu or openSUSE hosts." >&2
    exit 1
fi

MANAGER=dnf
install() { dnf -y install "$@"; }
remove() { dnf -y remove "$@"; }
installed() { rpm -q "$1" >/dev/null 2>&1; }

echo "manager=$MANAGER"

# Tools this test wants on every image.
install bat btop

# Conservative bloatware list: remove only packages that are actually present.
# It covers both common GNOME and KDE extras and never includes core desktop,
# login-manager or networking packages.
BLOAT=(
    gnome-tour
    gnome-maps
    gnome-weather
    gnome-contacts
    cheese
    rhythmbox
    totem
    simple-scan
    shotwell
    kmahjongg
    kmines
    kpat
    ksudoku
    ktorrent
    dragon
    elisa
    kamoso
    kde-connect
    khelpcenter
    plasma-welcome
    keditbookmarks
)

removed=()
for package in "${BLOAT[@]}"; do
    if installed "$package"; then
        echo "removing $package"
        if remove "$package"; then
            removed+=("$package")
        else
            echo "warning: could not remove $package" >&2
        fi
    fi
done

{
    echo "date=$(date -Is)"
    echo "manager=$MANAGER"
    echo "installed=bat,btop"
    echo "removed=${removed[*]:-none}"
} >"$MARKER"

echo "=== vEMU test post-setup complete: $(date -Is)"
