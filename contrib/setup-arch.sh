#!/usr/bin/env bash
# Experimental Arch-family installer for v-EMU.
#
# Supported hosts: Arch Linux, CachyOS and Arch-based desktops (for example
# Omarchy). This is not an official v-EMU host tier. Image Builder remains
# disabled because image-builder/osbuild are not packaged for Arch.
#
# This file is intentionally kept out of the .run release artifact. Clone the
# repository and run it from the source tree.
#
# Usage:
#   ./contrib/setup-arch.sh --precheck
#   ./contrib/setup-arch.sh
#   ./contrib/setup-arch.sh --uninstall
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODE=install
FULL=0

usage() {
    cat <<'EOF'
Experimental Arch-family installer for v-EMU.

Usage:
  ./contrib/setup-arch.sh --precheck   Check packages and KVM only.
  ./contrib/setup-arch.sh --full       Also install aarch64/riscv64 TCG
                                       packages and firmware.
  ./contrib/setup-arch.sh              Install core v-EMU (UI + service).
  ./contrib/setup-arch.sh --uninstall  Remove the service, daemon copy and
                                       firmware; user data is kept.

This installer does not enable Image Builder. Arch family remains
experimental/as-is and is not part of the official host matrix.
EOF
}

for arg in "$@"; do
    case "$arg" in
        --precheck) MODE=precheck ;;
        --full) FULL=1 ;;
        --uninstall) MODE=uninstall ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ $(id -u) -eq 0 ]]; then
    echo "Run this script as your normal desktop user, not root." >&2
    echo "It invokes sudo for system steps." >&2
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
    *arch*|*cachyos*) ;;
    *)
        echo "This installer is only for Arch-family hosts." >&2
        echo "Detected: ${PRETTY_NAME:-unknown}" >&2
        exit 1
        ;;
esac

SUDO=(sudo)
if [[ $MODE != "precheck" ]]; then
    echo "Requesting sudo privileges..."
    "${SUDO[@]}" -v
fi

PACKAGES=(
    qemu-full libvirt libvirt-python virt-install dnsmasq nftables
    swtpm edk2-ovmf virtiofsd pyside6 guestfs-tools libisoburn
    dosfstools mtools tigervnc virt-viewer python python-pip polkit
)
if (( FULL )); then
    PACKAGES+=(edk2-aarch64 edk2-riscv64)
fi

check_tools() {
    local missing=()
    local tool
    for tool in qemu-system-x86_64 qemu-img virsh nft swtpm \
                virt-customize xorriso python3; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if [[ ! -e /dev/kvm ]]; then
        missing+=("/dev/kvm")
    fi
    if ! python3 -c 'import PySide6' >/dev/null 2>&1; then
        missing+=("PySide6 (pyside6)")
    fi
    if ! python3 -c 'import libvirt' >/dev/null 2>&1; then
        missing+=("libvirt-python")
    fi
    if (( FULL )); then
        for tool in qemu-system-aarch64 qemu-system-riscv64; do
            command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
        done
    fi
    if (( ${#missing[@]} )); then
        printf 'Missing: %s\n' "${missing[*]}" >&2
        return 1
    fi
    echo "Core tools and /dev/kvm found."
    python3 - <<'PY'
import PySide6
print("PySide6", PySide6.__version__)
PY
}

check_firmware() {
    local missing=()
    local file
    for file in \
        /usr/share/vemu/firmware/aarch64/QEMU_EFI-pflash.raw \
        /usr/share/vemu/firmware/aarch64/vars-template.raw \
        /usr/share/vemu/firmware/riscv64/RISCV_VIRT_CODE-pflash.raw \
        /usr/share/vemu/firmware/riscv64/vars-template.raw; do
        [[ -f "$file" ]] || missing+=("$file")
    done
    if (( ${#missing[@]} )); then
        printf 'Missing firmware: %s\n' "${missing[*]}" >&2
        return 1
    fi
    echo "Cross-architecture firmware found."
}

if [[ $MODE == "precheck" ]]; then
    status=0
    check_tools || status=1
    if (( FULL )); then
        check_firmware || status=1
    fi
    exit "$status"
fi

if [[ $MODE == "uninstall" ]]; then
    "${SUDO[@]}" systemctl disable --now vemud 2>/dev/null || true
    "${SUDO[@]}" rm -f /etc/systemd/system/vemud.service
    "${SUDO[@]}" rm -rf /usr/local/lib/vemu /usr/share/vemu/firmware
    "${SUDO[@]}" rm -f /etc/vemu/qemu.env \
        /usr/share/polkit-1/actions/org.vemu.policy \
        /usr/lib/sysusers.d/vemu.conf
    "${SUDO[@]}" systemctl daemon-reload
    echo "Removed the v-EMU service, daemon copy and firmware."
    echo "Kept $ROOT/.venv, storage, VMs and the vemu/libvirt groups."
    exit 0
fi

echo "Installing Arch-family packages..."
"${SUDO[@]}" pacman -S --needed --noconfirm "${PACKAGES[@]}"
check_tools

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "Creating venv with system PySide6/libvirt access..."
    python3 -m venv --system-site-packages "$ROOT/.venv"
fi
echo "Installing v-EMU into the venv..."
"$ROOT/.venv/bin/pip" install --disable-pip-version-check -e "$ROOT"

mkdir -p "$HOME/.local/bin"
for tool in vemu vemu-ui vemud; do
    ln -sfn "$ROOT/.venv/bin/$tool" "$HOME/.local/bin/$tool"
done
VEMU_EXEC="vemu ui" VEMU_TRYEXEC="vemu" \
    "$ROOT/packaging/install-desktop-user.sh"

if ! getent group vemu >/dev/null 2>&1; then
    "${SUDO[@]}" groupadd --system vemu
fi
"${SUDO[@]}" usermod -aG vemu "$USER"
for group in libvirt kvm; do
    if getent group "$group" >/dev/null 2>&1; then
        "${SUDO[@]}" usermod -aG "$group" "$USER"
    fi
done

install_firmware() {
    local src="$ROOT/firmware" dst=/usr/share/vemu/firmware
    local file dir
    local -a files=(
        x64/OVMF_CODE.fd x64/OVMF_VARS.fd
        x64/OVMF_CODE.secboot.fd x64/OVMF_VARS.secboot.fd
    )
    local -a dirs=(x64)
    if (( FULL )); then
        files+=(
            aarch64/QEMU_EFI.fd aarch64/QEMU_VARS.fd
            riscv64/RISCV_VIRT_CODE.fd riscv64/RISCV_VIRT_VARS.fd
        )
        dirs+=(aarch64 riscv64)
    fi
    echo "Installing v-EMU firmware (${dirs[*]})..."
    "${SUDO[@]}" install -d -m 0755 "$dst"
    for dir in "${dirs[@]}"; do
        "${SUDO[@]}" install -d -m 0755 "$dst/$dir"
    done
    for file in "${files[@]}"; do
        if [[ -f "$src/$file.xz" ]]; then
            xz -dc "$src/$file.xz" | \
                "${SUDO[@]}" tee "$dst/$file" >/dev/null
        elif [[ -f "$src/$file" ]]; then
            "${SUDO[@]}" install -m 0644 "$src/$file" "$dst/$file"
        else
            echo "Missing firmware artifact: $file" >&2
            return 1
        fi
        "${SUDO[@]}" chmod 0644 "$dst/$file"
    done
    if (( FULL )); then
        "${SUDO[@]}" truncate -s 64M "$dst/aarch64/QEMU_EFI-pflash.raw"
        "${SUDO[@]}" dd if="$dst/aarch64/QEMU_EFI.fd" \
            of="$dst/aarch64/QEMU_EFI-pflash.raw" \
            conv=notrunc status=none
        "${SUDO[@]}" truncate -s 64M "$dst/aarch64/vars-template.raw"
        "${SUDO[@]}" dd if="$dst/aarch64/QEMU_VARS.fd" \
            of="$dst/aarch64/vars-template.raw" \
            conv=notrunc status=none
        "${SUDO[@]}" truncate -s 32M "$dst/riscv64/RISCV_VIRT_CODE-pflash.raw"
        "${SUDO[@]}" dd if="$dst/riscv64/RISCV_VIRT_CODE.fd" \
            of="$dst/riscv64/RISCV_VIRT_CODE-pflash.raw" \
            conv=notrunc status=none
        "${SUDO[@]}" truncate -s 32M "$dst/riscv64/vars-template.raw"
        "${SUDO[@]}" dd if="$dst/riscv64/RISCV_VIRT_VARS.fd" \
            of="$dst/riscv64/vars-template.raw" \
            conv=notrunc status=none
        "${SUDO[@]}" chmod 0644 \
            "$dst/aarch64/QEMU_EFI-pflash.raw" \
            "$dst/aarch64/vars-template.raw" \
            "$dst/riscv64/RISCV_VIRT_CODE-pflash.raw" \
            "$dst/riscv64/vars-template.raw"
    fi
    if [[ -f "$src/VERSION" ]]; then
        "${SUDO[@]}" install -m 0644 "$src/VERSION" "$dst/VERSION"
    fi
    for file in LICENSE NOTICE README.md; do
        if [[ -f "$src/$file" ]]; then
            "${SUDO[@]}" install -m 0644 "$src/$file" "$dst/$file"
        fi
    done
    if [[ -d "$src/licenses" ]]; then
        "${SUDO[@]}" install -d -m 0755 "$dst/licenses"
        for file in "$src"/licenses/*; do
            [[ -f "$file" ]] || continue
            "${SUDO[@]}" install -m 0644 "$file" "$dst/licenses/"
        done
    fi
}

install_firmware
if (( FULL )); then
    check_firmware
fi

if getent passwd libvirt-qemu >/dev/null 2>&1; then
    QEMU_USER=libvirt-qemu
    QEMU_GROUP=libvirt-qemu
else
    QEMU_USER=qemu
    QEMU_GROUP=qemu
fi
"${SUDO[@]}" install -d -m 0755 /etc/vemu
TMP_ENV=$(mktemp)
printf 'VEMU_QEMU_USER=%s\nVEMU_QEMU_GROUP=%s\n' \
    "$QEMU_USER" "$QEMU_GROUP" >"$TMP_ENV"
"${SUDO[@]}" install -Dm644 "$TMP_ENV" /etc/vemu/qemu.env
rm -f "$TMP_ENV"

echo "Installing daemon copy under /usr/local/lib/vemu..."
"${SUDO[@]}" install -d -m 0755 /usr/local/lib/vemu
"${SUDO[@]}" rm -rf /usr/local/lib/vemu/vemu
"${SUDO[@]}" cp -a "$ROOT/src/vemu" /usr/local/lib/vemu/vemu
"${SUDO[@]}" find /usr/local/lib/vemu/vemu -name '__pycache__' \
    -type d -prune -exec rm -rf {} +

"${SUDO[@]}" install -Dm644 \
    "$ROOT/packaging/vemu.sysusers" /usr/lib/sysusers.d/vemu.conf
"${SUDO[@]}" systemd-sysusers
"${SUDO[@]}" install -Dm644 \
    "$ROOT/packaging/org.vemu.policy" \
    /usr/share/polkit-1/actions/org.vemu.policy

TMP_SERVICE=$(mktemp)
cat >"$TMP_SERVICE" <<EOF
[Unit]
Description=v-EMU daemon (Arch experimental)
Documentation=https://github.com/withcoresystem/v-EMU
After=libvirtd.service virtqemud.socket virtnodedevd.socket virtnetworkd.service virtstoraged.service
Wants=virtqemud.socket virtnodedevd.socket virtnetworkd.service virtstoraged.service

[Service]
Type=simple
Group=vemu
Environment=PYTHONPATH=/usr/local/lib/vemu
EnvironmentFile=-/etc/vemu/qemu.env
ExecStart=/usr/bin/python3 -m vemu.daemon --foreground
RuntimeDirectory=vemu
RuntimeDirectoryMode=0750
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
LockPersonality=yes
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
"${SUDO[@]}" install -Dm644 "$TMP_SERVICE" \
    /etc/systemd/system/vemud.service
rm -f "$TMP_SERVICE"

if systemctl is-active --quiet libvirtd; then
    # Keep the user's monolithic stack; do not start competing modular daemons.
    "${SUDO[@]}" systemctl enable --now libvirtd >/dev/null
elif systemctl cat virtqemud.service >/dev/null 2>&1; then
    for unit in virtqemud.socket virtqemud.service \
                virtnodedevd.socket virtnetworkd.socket \
                virtstoraged.socket virtsecretd.socket; do
        if systemctl cat "$unit" >/dev/null 2>&1; then
            "${SUDO[@]}" systemctl enable --now "$unit" >/dev/null 2>&1 || true
        fi
    done
else
    "${SUDO[@]}" systemctl enable --now libvirtd
fi
"${SUDO[@]}" virsh net-start default 2>/dev/null || true
"${SUDO[@]}" virsh net-autostart default 2>/dev/null || true

if ! grep -qs 'net.ipv4.ip_forward' /etc/sysctl.d/90-vemu.conf; then
    printf 'net.ipv4.ip_forward = 1\n' | \
        "${SUDO[@]}" tee /etc/sysctl.d/90-vemu.conf >/dev/null
fi
"${SUDO[@]}" sysctl --system >/dev/null

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable vemud >/dev/null
"${SUDO[@]}" systemctl restart vemud
sleep 1
"${SUDO[@]}" systemctl is-active --quiet vemud || {
    echo "vemud did not start. Check: systemctl status vemud" >&2
    exit 1
}

cat <<EOF

v-EMU is installed for the Arch family (experimental).

  User:       $USER
  Source:     $ROOT
  Venv:       $ROOT/.venv
  Daemon:     /usr/local/lib/vemu (system Python)
  Service:    vemud.service
  Socket:     /run/vemu/vemud.sock (group vemu)
  QEMU user:  $QEMU_USER:$QEMU_GROUP
  Firmware:   /usr/share/vemu/firmware
  Full TCG:   $([[ $FULL -eq 1 ]] && echo yes || echo no)

Log out and back in so the vemu/libvirt/kvm groups apply. Then run:
  vemu doctor
  vemu ui

Image Builder is disabled on Arch-family hosts: image-builder/osbuild are
not packaged here. This host tier is experimental and not officially
supported.
EOF
