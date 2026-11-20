# Image Builder branding

This page covers the branding layer of the v-EMU **installer ISO** build. It is
for users and template authors who want a custom Anaconda, GRUB and Plymouth
look. The qcow2 flow has no branding step.

Branding is optional. Without it, Image Builder uses the bundled v-EMU mark and
the defaults below.

## Scope

- Installer ISO only; qcow2 images are not branded.
- Supported bases: Fedora and RHEL-family Anaconda GTK ISOs with Plymouth
  `two-step`.
- Debian, Ubuntu and openSUSE are not Image Builder hosts in 0.2.0.
- The base boot chain and Secure Boot signatures are never modified.

## Wizard fields

Image Builder → **New ISO build** → **Branding**.

| Field | Default | Rule |
|---|---|---|
| Product name | `vEMU Lab` | Required; Anaconda shows it uppercase plus `INSTALLATION` |
| Product version | `0.2` | Required; keep it short |
| Sidebar background | `#111827` | `#RRGGBB` only |
| Accent | `#0f766e` | `#RRGGBB` only; header and spinner base |
| Logo SVG | bundled v-EMU mark | Valid XML with an `<svg>` root, at most 512 KiB |
| GRUB title | `Install <product name>` | One line; v-EMU appends its fixed suffixes |

Invalid XML, a non-SVG root, an oversized file, a bad colour or an empty
name/version blocks the build during preflight.

## Logo standards

- **Transparent background.** The same file is composited on the Anaconda
  sidebar and the Plymouth gradient; a baked white background shows as a box.
- **Square or slightly wide artwork.** The logo is fitted into `150x69` for the
  installer sidebar and `280x240` for the boot screen, preserving aspect ratio.
- **No baked text.** Text inside the SVG is unreadable when scaled; the product
  name is rendered by Anaconda and the boot credit is fixed.
- **Check contrast yourself.** v-EMU does not recolour the SVG. Verify it against
  both the sidebar colour and the dark boot gradient.
- **Keep detail low.** Thin strokes and fine gradients disappear at 69 px high.

## Colour and copy

- Use six-digit `#RRGGBB`; three-digit hex and named colours are rejected.
- The boot background is a gradient from the sidebar colour to a mix containing
  22% accent. Keep the sidebar dark for white text, or accept Anaconda's
  automatic dark header text.
- The spinner is 65% white blended with the accent so it stays visible on the
  gradient.
- GRUB titles should be short. v-EMU appends `(basic graphics)`, `(FIPS)`,
  `Rescue a <product> system` and `Test media & install <product>` where needed.
- The boot screen shows only the logo and the fixed credit `Powered by v-EMU`.
  Do not add the product name or version under the logo.

## What gets branded

| Layer | Where it appears | What v-EMU does |
|---|---|---|
| Anaconda | Installer ISO | Sets `.buildstamp`, injects CSS and the sidebar logo through `updates.img` |
| GRUB | Installer ISO | Renames menu entries and sets the timeout; the default entry stays first |
| Plymouth | Installed system | Adds a theme, makes it the default and rebuilds the initramfs |
| First boot | Installed system | Optional one-shot TUI on `tty2` before the display manager |

Plymouth applies after installation, not to the installer ISO itself. It uses a
transparent watermark (`780x400`, logo at most `280x240`), a 30-frame accent
spinner and the gradient background. The required `lock.png`, `entry.png` and
`bullet.png` widgets are generated automatically; do not supply them.

## First-boot TUI

The optional TUI runs once as root before the display manager. It can show a
short company name and run a payload from a bundled file, USB/HTTP(S) source or
a URL. The TUI is not a general provisioning system; use cloud-init or a
template for normal guest setup.

## Limits

- Branding does not change the boot chain, kernel signatures or Secure Boot.
- The ISO remains a user-owned file; v-EMU does not register, relabel or delete
  it.
- Branding is supported only for the ISO targets listed above.
- The logo and colours are the author's responsibility; v-EMU validates the
  format, not the visual result.

## Related

- [`BUILDER.md`](BUILDER.md) — full Image Builder qcow2/ISO guide.
- [`../templates/README.md`](../templates/README.md) — blueprints and ISO
  variants.
