# v-EMU UEFI firmware

Firmware tùy biến cho các VM EFI **tạo mới** (x64, aarch64, riscv64):

- boot logo v-EMU + tagline **Desktop Virtualization**;
- boot manager hiển thị **v-EMU Boot Manager**;
- độ phân giải mặc định **1280×800** (có thể dùng mode cao hơn);
- x64 UEFI mới **Secure Boot mặc định bật**, VARS dual-CA 2011+2023, có
  checkbox tắt cho ISO unsigned; chi tiết ở `docs/SECUREBOOT.md`;
- TPM 2.0 bật cho x64, aarch64 và riscv64 (boot được cả khi VM có hoặc không
  có TPM; VM RISC-V có TPM đã boot distro, guest visibility nên kiểm tra);
- giữ nguyên EDK II/OVMF BSD-2-Clause-Patent và copyright gốc.

VM đã tạo trước bản cập nhật **không bị đổi firmware/NVRAM** (trừ VM đang trỏ
tới firmware custom trong `/usr/share/vemu/firmware` — nhận bản vá khi chạy
lại `setup.sh`).

## Phiên bản

Firmware/platform version hiện tại: **1.0**, lưu trong `firmware/VERSION` và
được cài vào `/usr/share/vemu/firmware/VERSION`.

Version này **độc lập** với version ứng dụng v-EMU (`0.x.y`). Chỉ tăng khi
thay đổi firmware/EDK2/patch/logo/Secure Boot CA; các release app không cần
rebuild firmware. SMBIOS của VM mới dùng cùng platform version `1.0`.

**Quy tắc release:** không chạy `build.sh`, không đổi `VERSION`, không sửa
`SHA256SUMS` cho release app thông thường. Chỉ xác minh
`sha256sum -c SHA256SUMS` và tái sử dụng firmware hiện có. Các bản sau tập
trung vào code fixes và roadmap, không lặp lại vòng build firmware.

## Nguồn và license

- EDK II: `https://github.com/tianocore/edk2`, commit `5a52bd1`.
- Build từ `ArmVirtPkg/ArmVirtQemu.dsc`, `OvmfPkg/OvmfPkgX64.dsc`,
  `OvmfPkg/RiscVVirt/RiscVVirtQemu.dsc`.
- License: BSD-2-Clause-Patent; OpenSSL (CryptoPkg): Apache-2.0.
- Xem `LICENSE`, `NOTICE`, `licenses/`.
- Logo TianoCore không được đưa vào; logo trong `boot-logo.bmp` là artwork
  của v-EMU.
- Không xoá copyright/license/disclaimer; chỉ thay branding hiển thị.

## Artifact

```
x64/OVMF_CODE.fd              3.65 MB  (raw, dùng trực tiếp)
x64/OVMF_VARS.fd              528 KB   (raw template)
x64/OVMF_CODE.secboot.fd      3.65 MB  (SMM + Secure Boot)
x64/OVMF_VARS.secboot.fd      528 KB   (dual-CA 2011+2023)
aarch64/QEMU_EFI.fd           3 MB
aarch64/QEMU_VARS.fd          768 KB
riscv64/RISCV_VIRT_CODE.fd    8 MB
riscv64/RISCV_VIRT_VARS.fd    768 KB
```

Các file `.xz` là bản phân phối trong repo; `setup.sh` giải nén vào
`/usr/share/vemu/firmware` và tạo pflash:

- aarch64: code và vars 64 MiB (zero pad).
- riscv64: code và vars 32 MiB (zero pad).
- x64: dùng raw trực tiếp.

## Build lại (maintainer)

Yêu cầu: `git`, `gcc`, `aarch64-linux-gnu-gcc`, `riscv64-linux-gnu-gcc`,
`nasm`, `iasl`, `python3`, `xz`, EDK2 source đã init submodule và **sạch**
(commit `5a52bd1`).

```bash
EDK2_DIR=$HOME/edk2 ./firmware/build.sh
```

Script tạo bản copy tạm, áp patch `patches/vemu-firmware.patch`, copy
`boot-logo.bmp` vào `MdeModulePkg/Logo/Logo.bmp`, build bản thường rồi bản
Secure Boot x64 (`SECURE_BOOT_ENABLE=TRUE`, `SMM_REQUIRE=TRUE`) và cập nhật
artifact. Mã nguồn EDK2 gốc không bị sửa. VARS secboot dual-CA là artifact
đóng gói riêng, script không ghi đè.

Các thay đổi chính trong patch:

- `ArmVirtPkg/ArmVirtQemu.dsc`, `OvmfPkg/OvmfPkgX64.dsc`:
  boot timeout 3 giây (để logo hiển thị).
- `OvmfPkg/QemuRamfbDxe/QemuRamfb.c`: thêm mode 1280×800/1366×768/1440×900/
  1920×1080; default 1280×800; override qua fw_cfg `opt/ramfb/mode`.
- `MdeModulePkg/Logo/Logo.bmp`: logo v-EMU, nội dung canh giữa canvas.
- `BootManagerStrings.uni` / `FrontPageStrings.uni`: chuỗi v-EMU.
- `OvmfPkg/RiscVVirt/.../PlatformBm.c`: banner phiên bản v-EMU.
- `ArmVirtPkg/Library/QemuVirtMemInfoLib/QemuVirtMemInfoLib.c`: khôi phục
  mapping vùng peripheral `0x08000000–0x10000000` (Device) để PEI truy cập
  TPM TIS tại `0x0C000000`. Không có patch này, firmware aarch64 bật
  `TPM2_ENABLE=TRUE` sẽ data abort (FAR `0xC000000`) ngay trong `Tcg2ConfigPei`.

Build cả 3 arch dùng thêm `-D TPM2_CONFIG_ENABLE=TRUE`; riscv64 dùng
`-D TPM2_ENABLE=TRUE` (firmware-level; VM có TPM đã boot, guest visibility nên kiểm tra).

## Tích hợp

`vemu` dùng firmware này cho domain EFI mới nếu các file tồn tại
(`VEMU_FIRMWARE_DIR` có thể override, mặc định `/usr/share/vemu/firmware`).
Domain x64 UEFI mới mặc định dùng cặp `.secboot` và thêm
`<loader secure='yes'>` + `<smm state='on'/>`; người dùng có thể tắt bằng
`--no-secure-boot`/checkbox. Thiếu cặp secboot → fallback firmware thường và
cảnh báo; thiếu toàn bộ custom firmware → firmware libvirt mặc định.

Cài bằng:

```bash
./setup.sh --standard   # x64 firmware only
./setup.sh --full       # x64 + aarch64 + riscv64 firmware
```

`setup.sh` chỉ giải nén/pad, không build tại máy user; chạy lại là idempotent.

## Kiểm tra

- Logo hiển thị ở 1280×800, canh giữa, trên cả 3 arch.
- Boot manager hiển thị v-EMU.
- TPM 2.0 trong firmware (x64/aarch64 đã kiểm chứng; riscv64 firmware-level và
  boot-to-firmware); `Boot to firmware` vào menu khi VM có hoặc không có TPM.
- NVMe/USB ISO vẫn hoạt động.
- VM cũ và fallback không đổi.
- RISC-V: firmware boot tới UEFI Shell và Boot to firmware vào v-EMU Setup
  khi có TPM; **OpenSUSE Tumbleweed RISC-V và Ubuntu-RISCV đã boot**, chậm
  hơn aarch64; VM có TPM đã boot, guest visibility nên kiểm tra.
- `./setup.sh --precheck` kiểm tra host/firmware; `--standard`/`--full` verify
  lại file đã cài và kích thước pflash (`--standard` chỉ x64).
