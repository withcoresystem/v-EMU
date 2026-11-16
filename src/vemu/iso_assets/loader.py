#!/usr/bin/env python3
"""vEMU first-boot TUI loader.

Runs once on tty2 before the display manager.  It can run a setup payload from
a USB stick or download one over HTTP(S).  After success, quit or exhausted
retries it removes its own service/payload from the installed OS.

Only Python 3 standard library is used.
"""

from __future__ import annotations

import curses
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import textwrap
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None

PREFIX = Path(os.environ.get("VEMU_FIRSTBOOT_ROOT", "/"))


def p(path: str) -> Path:
    return PREFIX / path.lstrip("/")


RUN_DIR = p("/run/vemu-firstboot")
PAYLOAD_DIR = RUN_DIR / "payload"
MOUNT_DIR = RUN_DIR / "mnt"
CONFIG = p("/etc/vemu/firstboot.json")
BRANDING = p("/etc/vemu/branding.json")
INSTALL_DIR = p("/usr/libexec/vemu-firstboot")
BUNDLED_DIR = p("/usr/libexec/vemu-firstboot.d")
UNIT = p("/etc/systemd/system/vemu-firstboot.service")
WANTS = p("/etc/systemd/system/multi-user.target.wants/vemu-firstboot.service")
VAR_DIR = p("/var/lib/vemu")
LOG = p("/var/log/vemu-firstboot.log")
DONE = VAR_DIR / "firstboot.done"
SKIPPED = VAR_DIR / "firstboot.skipped"

HEADER_HEIGHT = 3
FOOTER_HEIGHT = 2
AUTO_REBOOT_SECONDS = 10

_CONSOLE_STATE: dict[str, str] | None = None


def suppress_console_status() -> None:
    """Hide systemd/kernel boot messages for the firstboot TUI session."""
    global _CONSOLE_STATE
    if _CONSOLE_STATE is not None:
        return
    old_printk = ""
    try:
        old_printk = p("/proc/sys/kernel/printk").read_text().strip()
        p("/proc/sys/kernel/printk").write_text("1\n")
    except OSError:
        pass

    show_status = "true"
    try:
        result = subprocess.run(
            [
                "busctl",
                "get-property",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "ShowStatus",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            show_status = "true" if "true" in result.stdout.lower() else "false"
    except (OSError, subprocess.SubprocessError):
        pass

    _CONSOLE_STATE = {"printk": old_printk, "show_status": show_status}
    try:
        subprocess.run(
            [
                "busctl",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "SetShowStatus",
                "s",
                "no",
            ],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        try:
            subprocess.run(["kill", "-s", "RTMIN+21", "1"], capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass


def restore_console_status() -> None:
    """Restore the original systemd/kernel console settings."""
    global _CONSOLE_STATE
    state = _CONSOLE_STATE
    if state is None:
        return
    _CONSOLE_STATE = None
    value = "yes" if state.get("show_status") == "true" else "no"
    try:
        subprocess.run(
            [
                "busctl",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "SetShowStatus",
                "s",
                value,
            ],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        try:
            signal_name = "RTMIN+20" if value == "yes" else "RTMIN+21"
            subprocess.run(["kill", "-s", signal_name, "1"], capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    old_printk = state.get("printk") or ""
    if old_printk:
        try:
            p("/proc/sys/kernel/printk").write_text(old_printk + "\n")
        except OSError:
            pass


def load_json(path: Path, default: dict) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def write_log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        return (255, 255, 255)
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def xterm256(value: str) -> int:
    r, g, b = hex_rgb(value)
    levels = (0, 95, 135, 175, 215, 255)
    ri = min(range(6), key=lambda i: abs(levels[i] - r))
    gi = min(range(6), key=lambda i: abs(levels[i] - g))
    bi = min(range(6), key=lambda i: abs(levels[i] - b))
    return 16 + 36 * ri + 6 * gi + bi


def basic_color(value: str) -> int:
    """Approximate a hex colour with the 8 basic ANSI colours."""
    r, g, b = hex_rgb(value)
    if r > 200 and g > 200 and b > 200:
        return 7  # white
    if r > 180 and g < 100 and b < 100:
        return 1  # red
    if g > 150 and r < 150 and b < 150:
        return 2  # green
    if r > 180 and g > 140 and b < 100:
        return 3  # yellow
    if b > 150 and r < 150:
        return 4  # blue
    if r > 150 and b > 150 and g < 150:
        return 5  # magenta
    if g > 80 and b > 80 and r < 110:
        return 6  # cyan
    if g > 80 and r < 110 and b < 110:
        return 2  # green
    return 0  # black


class Tui:
    PAIR_BG = 1
    PAIR_HEADER = 2
    PAIR_ACCENT = 3
    PAIR_SELECT = 4
    PAIR_DIM = 5
    PAIR_WARN = 6
    PAIR_ERROR = 7
    PAIR_SUCCESS = 8
    PAIR_FOOTER = 9
    PAIR_PANEL = 10
    PAIR_BORDER = 11
    PAIR_TITLE = 12

    def __init__(self) -> None:
        self.config = load_json(
            CONFIG,
            {
                "company_name": "vEMU",
                "sources": ["usb", "url"],
                "url": "",
                "retries": 3,
                "allow_skip": True,
                "reboot_prompt": True,
            },
        )
        self.branding = load_json(
            BRANDING,
            {
                "product_name": "vEMU",
                "product_version": "",
                "sidebar_bg": "#111827",
                "accent": "#0f766e",
                "text_color": "#ffffff",
            },
        )
        self.company = str(self.config.get("company_name") or "vEMU")
        self.sources = [str(item) for item in self.config.get("sources", ["usb", "url"])]
        self.retries = max(0, int(self.config.get("retries", 3)))
        self.default_url = str(self.config.get("url") or "")
        self.allow_skip = bool(self.config.get("allow_skip", True))
        self.reboot_prompt = bool(self.config.get("reboot_prompt", True))
        self.script_name = str(self.config.get("script_name") or "").strip()
        self.script_url = str(self.config.get("script_url") or "").strip()
        self.auto_mode = bool(self.script_name or self.script_url)

        self.stdscr = None
        self.payload: Path | None = None
        self.source_name = ""
        self.attempts = 0
        self.lines: list[str] = []
        self.process: subprocess.Popen | None = None

    # ------------------------------------------------------------------ setup
    def run(self, stdscr) -> None:
        self.stdscr = stdscr
        curses_setup()
        self.init_colors()
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(False)
        # Repaint periodically so systemd/service console messages cannot
        # leave stale lines on top of the TUI.
        stdscr.timeout(250)

        if self.auto_mode:
            if self.run_automatic():
                self.finish(
                    "done",
                    "Automatic first boot setup completed successfully.",
                    auto_reboot=True,
                )
                return
            self.message(
                "Automatic setup failed",
                [
                    "The configured payload could not be found or failed.",
                    "Falling back to manual setup.",
                    "",
                    f"Log: {LOG}",
                ],
            )

        while True:
            choice = self.menu()
            if choice == "quit":
                if self.confirm_quit():
                    self.finish("skipped", "First boot setup skipped.")
                    return
                continue

            if choice == "usb":
                self.source_name = "usb"
                payload = self.choose_usb()
            elif choice == "url":
                self.source_name = "url"
                payload = self.choose_url()
            elif choice == "bundled":
                self.source_name = "bundled"
                payload = self.choose_bundled()
            else:
                continue

            if payload is None:
                continue

            self.payload = payload
            self.attempts = 0
            result = self.run_with_retries()
            if result == "success":
                self.finish("done", "First boot setup completed successfully.")
                return
            if result == "quit":
                self.finish("skipped", "First boot setup quit before completion.")
                return
            # "source" -> back to the menu
            self.payload = None

    # ------------------------------------------------------------------- draw
    def init_colors(self) -> None:
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        def init(pair: int, fg: int, bg: int) -> None:
            try:
                curses.init_pair(pair, fg, bg)
            except curses.error:
                pass

        if curses.COLORS >= 256:
            accent = xterm256("#0f766e")
            text = xterm256("#ffffff")
            panel = xterm256("#333333")
            outer = xterm256("#1e1e1e")
            border = xterm256("#d4d4d4")
            muted = xterm256("#a3a3a3")
            warn, error, success = 214, 196, 46
        else:
            # On the Linux console only 8/16 colours exist. Redefine the cyan
            # slot to the vEMU logo green (#0f766e) so header/footer and the
            # selected row keep the mockup colour instead of bright cyan.
            accent = curses.COLOR_CYAN
            try:
                if curses.can_change_color():
                    r, g, b = hex_rgb("#0f766e")
                    curses.init_color(
                        curses.COLOR_CYAN,
                        r * 1000 // 255,
                        g * 1000 // 255,
                        b * 1000 // 255,
                    )
            except curses.error:
                pass
            text = curses.COLOR_WHITE
            panel = curses.COLOR_BLACK
            outer = curses.COLOR_BLACK
            border = curses.COLOR_WHITE
            muted = curses.COLOR_WHITE
            warn = curses.COLOR_YELLOW
            error = curses.COLOR_RED
            success = curses.COLOR_GREEN

        init(self.PAIR_BG, text, outer)
        init(self.PAIR_HEADER, text, accent)
        init(self.PAIR_FOOTER, text, accent)
        init(self.PAIR_SELECT, text, accent)
        init(self.PAIR_PANEL, text, panel)
        init(self.PAIR_BORDER, border, panel)
        init(self.PAIR_TITLE, text, panel)
        init(self.PAIR_DIM, muted, panel)
        init(self.PAIR_WARN, warn, panel)
        init(self.PAIR_ERROR, error, panel)
        init(self.PAIR_SUCCESS, success, panel)
    def cp(self, pair: int) -> int:
        import curses

        return curses.color_pair(pair)

    def put(self, y: int, x: int, text: str, pair: int | None = None) -> None:
        import curses

        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text.expandtabs(4)
        max_len = max(0, w - x)
        try:
            if pair is None:
                self.stdscr.addnstr(y, x, text, max_len)
            else:
                self.stdscr.addnstr(y, x, text, max_len, pair)
        except curses.error:
            pass

    def fill(self, y: int, pair: int) -> None:
        import curses

        h, w = self.stdscr.getmaxyx()
        if 0 <= y < h:
            try:
                self.stdscr.addnstr(y, 0, " " * w, w, pair)
            except curses.error:
                pass

    def draw_frame(self) -> None:
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()
        try:
            self.stdscr.bkgd(" ", self.cp(self.PAIR_BG))
        except curses.error:
            pass

        header = f" {self.company.upper()} - FIRST BOOT SETUP "
        self.fill(0, self.cp(self.PAIR_HEADER))
        self.put(0, max(0, (w - len(header)) // 2), header, self.cp(self.PAIR_HEADER))

        product = f"{self.branding.get('product_name', 'vEMU')} {self.branding.get('product_version', '')}".strip()
        self.put(1, 2, product, self.cp(self.PAIR_DIM))
        self.put(2, 0, "-" * w, self.cp(self.PAIR_DIM))

        self.fill(h - 1, self.cp(self.PAIR_HEADER))
        self.put(h - 2, 0, "-" * w, self.cp(self.PAIR_DIM))

    def draw_footer(self, text: str) -> None:
        h, w = self.stdscr.getmaxyx()
        self.fill(h - 1, self.cp(self.PAIR_HEADER))
        self.put(h - 1, 2, text[: max(0, w - 4)], self.cp(self.PAIR_HEADER))

    # ------------------------------------------------------------------- menu
    def fill_rect(self, y: int, x: int, height: int, width: int, pair: int) -> None:
        screen_h, screen_w = self.stdscr.getmaxyx()
        for row in range(y, min(y + height, screen_h)):
            if x >= screen_w:
                break
            length = max(0, min(width, screen_w - x))
            try:
                self.stdscr.addnstr(row, x, " " * length, length, pair)
            except curses.error:
                pass

    def box(self, x: int, y: int, width: int, height: int, title: str) -> None:
        if width < 4 or height < 3:
            return
        self.fill_rect(y, x, height, width, self.cp(self.PAIR_PANEL))
        border = self.cp(self.PAIR_BORDER)
        for col in range(1, width - 1):
            try:
                self.stdscr.addch(y, x + col, curses.ACS_HLINE, border)
                self.stdscr.addch(y + height - 1, x + col, curses.ACS_HLINE, border)
            except curses.error:
                pass
        for row in range(1, height - 1):
            try:
                self.stdscr.addch(y + row, x, curses.ACS_VLINE, border)
                self.stdscr.addch(y + row, x + width - 1, curses.ACS_VLINE, border)
            except curses.error:
                pass
        for corner_y, corner_x, char in (
            (y, x, curses.ACS_ULCORNER),
            (y, x + width - 1, curses.ACS_URCORNER),
            (y + height - 1, x, curses.ACS_LLCORNER),
            (y + height - 1, x + width - 1, curses.ACS_LRCORNER),
        ):
            try:
                self.stdscr.addch(corner_y, corner_x, char, border)
            except curses.error:
                pass
        self.put(y + 1, x + 2, title, self.cp(self.PAIR_TITLE) | curses.A_BOLD)

    def wrap_text(self, text: str, width: int) -> list[str]:
        if width <= 1:
            return [str(text)]
        return textwrap.wrap(
            str(text),
            width=width,
            break_long_words=False,
            replace_whitespace=False,
        ) or [""]

    def draw_shell(
        self,
        detail_title: str,
        detail_lines: list[str],
        footer: str,
        left_title: str = "Select a setup source",
    ) -> None:
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()
        try:
            self.stdscr.bkgd(" ", self.cp(self.PAIR_BG))
        except curses.error:
            pass

        self.fill_rect(0, 0, 3, width, self.cp(self.PAIR_HEADER))
        header = f"{self.company.upper()} - FIRST BOOT SETUP"
        self.put(
            1,
            max(0, (width - len(header)) // 2),
            header,
            self.cp(self.PAIR_HEADER) | curses.A_BOLD,
        )

        self.fill_rect(height - 1, 0, 1, width, self.cp(self.PAIR_FOOTER))
        date = datetime.now(timezone.utc).strftime("%d-%b-%Y %H:%M UTC")
        hint_width = max(0, width - len(date) - 4)
        self.put(height - 1, 1, footer[:hint_width], self.cp(self.PAIR_FOOTER))
        self.put(
            height - 1,
            max(1, width - len(date) - 1),
            date,
            self.cp(self.PAIR_FOOTER),
        )

        top = 3
        panel_height = height - top - 1
        left_x = 1
        left_width = max(30, width // 4)
        right_x = left_x + left_width + 1
        right_width = width - right_x - 1
        if right_width < 20:
            right_x = left_x + left_width
            right_width = width - right_x - 1

        self._left = (left_x, top, left_width, panel_height)
        self._right = (right_x, top, right_width, panel_height)
        self.box(left_x, top, left_width, panel_height, left_title)
        self.box(right_x, top, right_width, panel_height, detail_title)

        line_y = top + 3
        limit = top + panel_height - 1
        for line in detail_lines:
            for wrapped in self.wrap_text(line, max(1, right_width - 4)):
                if line_y >= limit:
                    break
                self.put(line_y, right_x + 2, wrapped, self.cp(self.PAIR_PANEL))
                line_y += 1
        self.stdscr.refresh()

    def draw_left(self, labels: list[str], selected: int) -> None:
        left_x, top, left_width, panel_height = self._left
        row = top + 3
        for index, label in enumerate(labels):
            if row >= top + panel_height - 1:
                break
            if index == selected:
                self.fill_rect(row, left_x + 1, 1, max(0, left_width - 2), self.cp(self.PAIR_SELECT))
                self.put(
                    row,
                    left_x + 2,
                    "> " + label,
                    self.cp(self.PAIR_SELECT) | curses.A_BOLD,
                )
            else:
                self.put(row, left_x + 2, "  " + label, self.cp(self.PAIR_PANEL))
            row += 1

    def item_detail(self, key: str) -> list[str]:
        if key == "usb":
            return [
                "USB setup stick",
                "",
                "Label: VEMU-FBOOT",
                "Expected payload:",
                "  vemu-firstboot/run.sh",
                "  vemu-firstboot/manifest.toml",
                "",
                "The stick is mounted read-only.",
                "The payload is copied to /run before",
                "it is executed as root.",
            ]
        if key == "url":
            return [
                "Network setup",
                "",
                "Use an HTTP or HTTPS URL.",
                "Payload: run.sh, .tar.gz or .tgz",
                "",
                "No checksum validation.",
                "Review the payload before running it.",
                "The script runs as root.",
            ]
        if key == "bundled":
            return [
                "Bundled setup",
                "",
                "Run the setup script embedded in this ISO.",
                "This is optional and normally used for",
                "validation or offline lab images.",
            ]
        return [
            "Skip first boot setup",
            "",
            "You will be asked to confirm.",
            "The first-boot service, loader and payload",
            "are removed before handover.",
            "This setup will not run again.",
        ]

    def menu_items(self) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        if "usb" in self.sources:
            items.append(("usb", "Run setup from USB", "Look for a VEMU-FBOOT USB stick"))
        if "url" in self.sources:
            items.append(("url", "Download setup from network", "Fetch a script or tar.gz over HTTP(S)"))
        if "bundled" in self.sources and (BUNDLED_DIR / "10-bundled.sh").is_file():
            items.append(("bundled", "Run bundled setup", "Use the script embedded in this ISO"))
        if self.allow_skip:
            items.append(("quit", "Quit first boot setup", "Skip and remove the first-boot payload"))
        return items

    def menu(self) -> str:
        items = self.menu_items()
        labels = [label for _, label, _ in items]
        selected = 0
        while True:
            self.draw_shell(
                "Detail pane",
                self.item_detail(items[selected][0]),
                "Up/Down Select    Enter Confirm    Q Quit",
            )
            self.draw_left(labels, selected)
            self.stdscr.refresh()

            ch = self.stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(items)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(items)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return items[selected][0]
            elif ch in (ord("q"), 27):
                return "quit"
    def message(self, title: str, lines: list[str], footer: str = "Press any key") -> None:
        self.draw_shell(title, lines, footer, left_title="Status")
        self.stdscr.timeout(-1)
        try:
            self.stdscr.getch()
        finally:
            self.stdscr.timeout(250)
    def confirm(self, question: str, default: bool = False) -> bool:
        self.stdscr.timeout(-1)
        try:
            while True:
                self.draw_shell(
                    "Confirm",
                    [question, "", "Y = Yes        N = No"],
                    "Y Confirm    N Cancel",
                    left_title="Confirmation",
                )
                ch = self.stdscr.getch()
                if ch in (ord("y"), ord("Y")):
                    return True
                if ch in (ord("n"), ord("N"), 27):
                    return False
                if ch in (10, 13):
                    return default
        finally:
            self.stdscr.timeout(250)

    def input_box(self, title: str, default: str = "") -> str | None:
        value = default
        self.stdscr.nodelay(False)
        self.stdscr.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                self.draw_shell(
                    "Setup URL",
                    [title, "", "Enter an HTTP(S) URL. Esc cancels."],
                    "Enter Accept    Esc Cancel",
                    left_title="Network",
                )
                right_x, top, right_width, panel_height = self._right
                field_y = top + 9
                field_x = right_x + 2
                field_width = max(10, right_width - 6)
                self.put(field_y - 1, field_x, "+" + "-" * field_width + "+", self.cp(self.PAIR_BORDER))
                shown = value[-field_width:] if field_width > 0 else value
                self.put(
                    field_y,
                    field_x,
                    "|" + shown.ljust(field_width) + "|",
                    self.cp(self.PAIR_PANEL),
                )
                self.put(field_y + 1, field_x, "+" + "-" * field_width + "+", self.cp(self.PAIR_BORDER))
                self.stdscr.move(
                    field_y,
                    min(field_x + 1 + len(shown), field_x + field_width),
                )
                self.stdscr.refresh()

                ch = self.stdscr.getch()
                if ch in (curses.KEY_ENTER, 10, 13):
                    return value.strip()
                if ch == 27:
                    return None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    value = value[:-1]
                elif ch == 21:
                    value = ""
                elif 32 <= ch < 127:
                    value += chr(ch)
        finally:
            curses.curs_set(0)
            self.stdscr.timeout(250)

    def usb_candidates(self) -> list[dict]:
        try:
            result = subprocess.run(
                ["lsblk", "-J", "-o", "NAME,LABEL,FSTYPE,RM,SIZE,TYPE,MOUNTPOINT"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []

        try:
            root = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", "/"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            root = ""

        found: list[dict] = []

        def walk(devices: list[dict]) -> None:
            for dev in devices:
                name = str(dev.get("name") or "")
                devpath = f"/dev/{name}"
                fstype = str(dev.get("fstype") or "")
                label = str(dev.get("label") or "")
                is_usb = bool(dev.get("rm"))
                if name and dev.get("type") in ("disk", "part") and fstype not in ("", "iso9660"):
                    if root == devpath or root.startswith(devpath):
                        # Never offer the disk that holds the running system.
                        pass
                    elif is_usb or label.upper() in ("VEMU-FIRSTBOOT", "VEMU_FIRSTBOOT", "VEMU-FBOOT", "VEMU_BOOT"):
                        found.append(
                            {
                                "dev": devpath,
                                "label": label or "USB",
                                "size": str(dev.get("size") or ""),
                                "fstype": fstype,
                            }
                        )
                walk(dev.get("children") or [])

        walk(data.get("blockdevices") or [])
        return found

    def prepare_usb(
        self,
        candidate: dict,
        *,
        script_name: str = "",
        interactive: bool = True,
    ) -> Path | None:
        if script_name and Path(script_name).name != script_name:
            self.report(
                "Invalid script name",
                ["The script name must be a simple file name."],
                interactive=interactive,
            )
            return None
        dev = candidate["dev"]
        mountpoint = MOUNT_DIR / dev.replace("/", "_")
        shutil.rmtree(mountpoint, ignore_errors=True)
        mountpoint.mkdir(parents=True, exist_ok=True)

        mounted = False
        for options in ("ro,noexec,nosuid,nodev", "ro"):
            result = subprocess.run(
                ["mount", "-o", options, dev, str(mountpoint)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                mounted = True
                break
        if not mounted:
            shutil.rmtree(mountpoint, ignore_errors=True)
            self.report(
                "USB mount failed",
                [f"Could not mount {dev}."],
                interactive=interactive,
            )
            return None

        try:
            source = mountpoint / "vemu-firstboot"
            if not source.is_dir():
                source = mountpoint
            entry = source / (script_name or "run.sh")
            manifest = source / "manifest.toml"
            if script_name:
                required = source / script_name
                if not required.is_file():
                    self.report(
                        "Script not found on USB",
                        [f"{dev} has no {script_name}."],
                        interactive=interactive,
                    )
                    return None
            elif not entry.is_file() and not manifest.is_file():
                self.report(
                    "No setup payload found",
                    [
                        f"{dev} is mounted, but no vemu-firstboot/run.sh",
                        "or manifest.toml was found.",
                    ],
                    interactive=interactive,
                )
                return None

            shutil.rmtree(PAYLOAD_DIR, ignore_errors=True)
            PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                if item.name.startswith("."):
                    continue
                target = PAYLOAD_DIR / item.name
                if item.is_dir():
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            return PAYLOAD_DIR
        finally:
            subprocess.run(["umount", str(mountpoint)], capture_output=True)
            shutil.rmtree(mountpoint, ignore_errors=True)

    def choose_usb(self) -> Path | None:
        candidates = self.usb_candidates()
        if not candidates:
            self.message(
                "No USB setup stick found",
                [
                    "Attach a USB stick labeled VEMU-FBOOT",
                    "with vemu-firstboot/run.sh or manifest.toml.",
                ],
            )
            return None

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            chosen = self.select_device(candidates)
            if chosen is None:
                return None
        return self.prepare_usb(chosen)

    def select_device(self, candidates: list[dict]) -> dict | None:
        labels = [f"{item['label']}  {item['dev']}  {item['size']}" for item in candidates]
        selected = 0
        while True:
            candidate = candidates[selected]
            self.draw_shell(
                "USB details",
                [
                    f"Device: {candidate['dev']}",
                    f"Label: {candidate['label']}",
                    f"Size: {candidate['size']}",
                    f"Filesystem: {candidate['fstype']}",
                    "",
                    "Looking for:",
                    "  vemu-firstboot/run.sh",
                    "  vemu-firstboot/manifest.toml",
                ],
                "Up/Down Select    Enter Confirm    Esc Cancel",
                left_title="USB devices",
            )
            self.draw_left(labels, selected)
            self.stdscr.refresh()

            ch = self.stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(candidates)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(candidates)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return candidates[selected]
            elif ch == 27:
                return None
    def fetch_url(self, url: str, *, interactive: bool = True) -> Path | None:
        if not (url.startswith("http://") or url.startswith("https://")):
            self.report(
                "Invalid URL",
                ["Only http:// and https:// URLs are supported."],
                interactive=interactive,
            )
            return None
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            self.report(
                "Invalid URL",
                ["The URL has no hostname."],
                interactive=interactive,
            )
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not self.wait_for_network(
            host, port, interactive=interactive
        ):
            return None

        shutil.rmtree(PAYLOAD_DIR, ignore_errors=True)
        PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = PAYLOAD_DIR / "download"

        self.draw_shell(
            "Downloading",
            ["Downloading setup payload...", "", url, "Please wait."],
            "Please wait",
            left_title="Network",
        )

        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "vEMU-firstboot/1.0"}
            )
            with urllib.request.urlopen(request, timeout=30) as response,                     target.open("wb") as handle:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if total:
                        self.put(
                            8,
                            2,
                            f"{received * 100 // total}%  "
                            f"({received // 1024} KiB)",
                            self.cp(self.PAIR_DIM),
                        )
                        self.stdscr.refresh()
        except (OSError, urllib.error.URLError) as exc:
            self.report(
                "Download failed", [str(exc)[:200]], interactive=interactive
            )
            return None

        if tarfile.is_tarfile(target):
            try:
                with tarfile.open(target) as archive:
                    members = []
                    for member in archive.getmembers():
                        path = Path(member.name)
                        if path.is_absolute() or ".." in path.parts:
                            continue
                        members.append(member)
                    archive.extractall(PAYLOAD_DIR, members=members)
                target.unlink(missing_ok=True)
                nested = PAYLOAD_DIR / "vemu-firstboot"
                if nested.is_dir():
                    for item in nested.iterdir():
                        shutil.move(str(item), str(PAYLOAD_DIR / item.name))
                    shutil.rmtree(nested, ignore_errors=True)
            except (OSError, tarfile.TarError) as exc:
                self.report(
                    "Extract failed",
                    [str(exc)[:200]],
                    interactive=interactive,
                )
                return None
        else:
            script = PAYLOAD_DIR / "run.sh"
            target.rename(script)
            script.chmod(0o755)

        if not self.find_entry(PAYLOAD_DIR):
            self.report(
                "No setup entrypoint",
                ["Expected run.sh or manifest.toml with run = ..."],
                interactive=interactive,
            )
            return None
        return PAYLOAD_DIR

    def choose_url(self) -> Path | None:
        url = self.input_box("Setup URL (http/https):", self.default_url)
        if not url:
            return None
        return self.fetch_url(url, interactive=True)

    def _nmcli(self, *args: str, timeout: int = 8):
        nmcli = shutil.which("nmcli")
        if not nmcli:
            return None
        try:
            return subprocess.run(
                [nmcli, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def ensure_dynamic_ip(self) -> str:
        """Bring up a DHCP ethernet connection if the target has no profile."""
        if not shutil.which("nmcli"):
            return "NetworkManager not found"
        self._nmcli("networking", "on")
        devices = self._nmcli("-t", "-f", "DEVICE,TYPE,STATE", "device")
        if devices is None or devices.returncode != 0:
            return "NetworkManager device query failed"

        ethernet: list[tuple[str, str]] = []
        for line in devices.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            device, kind, state = parts[0], parts[1], parts[2]
            if kind == "ethernet" and device != "lo":
                ethernet.append((device, state))
        if not ethernet:
            return "No ethernet interface found"

        active = self._nmcli("-t", "-f", "DEVICE,STATE", "device")
        if active is not None:
            for line in active.stdout.splitlines():
                if line.endswith(":connected"):
                    return ""

        for device, state in ethernet:
            if state == "unmanaged":
                self._nmcli("device", "set", device, "managed", "yes")
            connections = self._nmcli("-t", "-f", "NAME,DEVICE", "connection", "show")
            existing = ""
            if connections is not None:
                for line in connections.stdout.splitlines():
                    name, _, bound = line.partition(":")
                    if bound == device:
                        existing = name
                        break
            if existing:
                self._nmcli("connection", "up", existing)
                return f"DHCP requested on {existing}"
            added = self._nmcli(
                "connection",
                "add",
                "type",
                "ethernet",
                "ifname",
                device,
                "con-name",
                "vemu-firstboot-dhcp",
                "ipv4.method",
                "auto",
                "ipv6.method",
                "auto",
            )
            if added is not None and added.returncode == 0:
                self._nmcli("connection", "up", "vemu-firstboot-dhcp")
                return f"DHCP requested on {device}"
            return f"Could not create DHCP profile on {device}"
        return ""

    def _network_state(self) -> str:
        nmcli = shutil.which("nmcli")
        if not nmcli:
            return ""
        try:
            result = subprocess.run(
                [nmcli, "-t", "-f", "STATE", "general"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def wait_for_network(
        self, host: str, port: int, *, interactive: bool = True
    ) -> bool:
        """Wait for DNS/network before attempting an HTTP(S) download."""
        nmcli = shutil.which("nmcli")
        dhcp_note = self.ensure_dynamic_ip()
        attempted_enable = False
        deadline = time.monotonic() + 45
        last_error = ""
        while time.monotonic() < deadline:
            state = self._network_state()
            if nmcli and state in {"disconnected", "asleep", "unknown"} and not attempted_enable:
                subprocess.run([nmcli, "networking", "on"], capture_output=True)
                attempted_enable = True
            try:
                socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                return True
            except OSError as exc:
                last_error = str(exc)

            remaining = max(0, int(deadline - time.monotonic()))
            self.draw_shell(
                "Network",
                [
                    "Waiting for network and DNS...",
                    f"Host: {host}:{port}",
                    f"NetworkManager: {state or 'unknown'}",
                    f"DHCP: {dhcp_note or 'active connection'}",
                    f"Last error: {last_error[:120]}",
                    f"Retry in {remaining}s",
                ],
                "Please wait",
                left_title="Network",
            )
            time.sleep(0.5)

        self.report(
            "Network not ready",
            [
                "DNS could not resolve the setup host.",
                "Check this machine's network/DNS, or use the USB source.",
                f"Last error: {last_error[:160]}",
            ],
            interactive=interactive,
        )
        return False

    def choose_bundled(self) -> Path | None:
        script = BUNDLED_DIR / "10-bundled.sh"
        if not script.is_file():
            self.message("No bundled setup", ["No bundled setup script is installed."])
            return None
        shutil.rmtree(PAYLOAD_DIR, ignore_errors=True)
        PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, PAYLOAD_DIR / "run.sh")
        return PAYLOAD_DIR

    # ------------------------------------------------------------------- auto
    def report(self, title: str, lines: list[str], *, interactive: bool) -> None:
        if interactive:
            self.message(title, lines)
        else:
            write_log(f"{title}: " + " | ".join(lines))

    def run_automatic(self) -> bool:
        """Try the configured payload without opening the menu."""
        url = self.script_url
        if url and self.script_name:
            url = url + self.script_name
        if url:
            self.source_name = "url"
            write_log(f"automatic URL payload: {url}")
            payload = self.fetch_url(url, interactive=False)
            if payload is not None:
                found = self.find_entry(payload)
                if found is None:
                    write_log("automatic URL payload has no entry point")
                    return False
                entry, timeout = found
                return self.execute_automatic(entry, timeout)
            write_log("automatic URL payload unavailable; trying USB")

        if self.script_name:
            self.source_name = "usb"
            payload = self.auto_usb(self.script_name)
            if payload is not None:
                found = self.find_entry(payload, preferred=self.script_name)
                if found is None:
                    write_log(
                        f"USB payload has no entry point: {self.script_name}"
                    )
                    return False
                entry, timeout = found
                return self.execute_automatic(entry, timeout)
        return False

    def auto_usb(self, script_name: str) -> Path | None:
        candidates = self.usb_candidates()
        if len(candidates) != 1:
            write_log(
                "automatic USB skipped: expected exactly one VEMU-FBOOT "
                f"device, found {len(candidates)}"
            )
            return None
        return self.prepare_usb(
            candidates[0], script_name=script_name, interactive=False
        )

    def execute_automatic(self, entry: Path, timeout: int) -> bool:
        attempts = max(1, self.retries)
        for attempt in range(1, attempts + 1):
            code = self.execute(entry, timeout)
            self.attempts = attempt
            if code == 0:
                return True
            write_log(
                f"automatic attempt {attempt}/{attempts} failed "
                f"(exit {code})"
            )
            if code is None:
                break
        return False

    # ------------------------------------------------------------------- run
    def find_entry(
        self, payload: Path, preferred: str | None = None
    ) -> tuple[Path, int] | None:
        if preferred:
            entry = (payload / preferred).resolve()
            if payload.resolve() in entry.parents and entry.is_file():
                return entry, 0
        manifest = payload / "manifest.toml"
        if manifest.is_file() and tomllib is not None:
            try:
                with manifest.open("rb") as handle:
                    data = tomllib.load(handle)
                run = str(data.get("run") or "")
                timeout = int(data.get("timeout") or 0)
                if run:
                    entry = (payload / run).resolve()
                    if payload.resolve() in entry.parents and entry.is_file():
                        return entry, timeout
            except (OSError, ValueError, tomllib.TOMLDecodeError):
                pass
        for name in ("run.sh",):
            entry = payload / name
            if entry.is_file():
                return entry, 0
        scripts = sorted(payload.glob("*.sh"))
        if scripts:
            return scripts[0], 0
        return None

    def run_with_retries(self) -> str:
        assert self.payload is not None
        while True:
            found = self.find_entry(self.payload)
            if not found:
                self.message("No setup entrypoint", ["Expected run.sh or manifest.toml with run = ..."])
                return "source"
            entry, timeout = found
            code = self.execute(entry, timeout)
            self.attempts += 1
            if code == 0:
                return "success"
            if code is None:
                return "quit"

            remaining = self.retries - self.attempts
            if remaining > 0:
                choice = self.failure_choice(code, remaining)
                if choice == "retry":
                    continue
                if choice == "source":
                    return "source"
                if choice == "quit":
                    if self.confirm_quit():
                        return "quit"
                    continue
            else:
                self.message(
                    "Retry limit reached",
                    [
                        f"Setup failed after {self.attempts} attempt(s).",
                        "The first-boot payload will be removed.",
                        "You can run setup manually later.",
                    ],
                )
                if self.confirm_quit():
                    return "quit"
                return "source"

    def execute(self, entry: Path, timeout: int) -> int | None:
        import curses

        self.lines = []
        self.draw_shell(
            "Progress",
            [f"Running: {entry.name}", f"Source: {self.source_name}", "", "Please wait..."],
            "Esc Abort    Please wait",
            left_title="Running",
        )

        env = os.environ.copy()
        env.update(
            {
                "VEMU_FIRSTBOOT": "1",
                "VEMU_PAYLOAD_SOURCE": self.source_name,
                "VEMU_PROFILE": self.company,
            }
        )
        try:
            self.process = subprocess.Popen(
                ["/bin/bash", str(entry)],
                cwd=str(entry.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            write_log(f"cannot start {entry}: {exc}")
            self.message("Cannot start setup", [str(exc)])
            return 127

        assert self.process.stdout is not None
        write_log(f"=== {self.source_name}: {entry} ===")
        start = time.monotonic()
        self.stdscr.nodelay(True)

        try:
            while True:
                if self.process.poll() is not None:
                    break

                ready, _, _ = select.select([self.process.stdout], [], [], 0.1)
                if ready:
                    line = self.process.stdout.readline()
                    if line:
                        self.append_line(line)

                if timeout and time.monotonic() - start > timeout:
                    write_log(f"timeout after {timeout}s")
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except OSError:
                        pass
                    self.message("Setup timed out", [f"The script exceeded the {timeout}s timeout."])
                    break

                ch = self.stdscr.getch()
                if ch in (27, ord("q")):
                    self.stdscr.nodelay(False)
                    abort = self.confirm("Abort the running setup script?")
                    self.stdscr.nodelay(True)
                    if abort:
                        try:
                            os.killpg(self.process.pid, signal.SIGTERM)
                        except OSError:
                            pass
                        try:
                            self.process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(self.process.pid, signal.SIGKILL)
                            except OSError:
                                pass
                        self.append_line("aborted by user")
                        break

                self.draw_running()
                self.stdscr.refresh()
        finally:
            self.stdscr.nodelay(False)
            self.stdscr.timeout(250)

        try:
            remaining, _ = self.process.communicate(timeout=10)
            if remaining:
                for line in remaining.splitlines():
                    self.append_line(line)
        except subprocess.TimeoutExpired:
            self.process.kill()

        code = self.process.returncode
        write_log(f"=== exit {code} ===")
        return code

    def append_line(self, line: str) -> None:
        line = line.rstrip("\n")
        if not line:
            return
        self.lines.append(line)
        if len(self.lines) > 200:
            self.lines = self.lines[-200:]
        write_log(line)

    def draw_running(self) -> None:
        height, _ = self.stdscr.getmaxyx()
        visible = self.lines[-(max(1, height - 10)) :]
        self.draw_shell(
            "Progress",
            [f"Source: {self.source_name}", ""] + visible,
            "Esc Abort    Please wait",
            left_title="Running",
        )
        self.draw_left([self.source_name.upper() or "SETUP"], 0)
        self.stdscr.refresh()
    def failure_choice(self, code: int, remaining: int) -> str:
        labels = [
            f"Retry setup ({remaining} retry/retries left)",
            "Choose another source",
            "Quit and remove first-boot payload",
        ]
        selected = 0
        while True:
            detail = [f"Setup failed (exit {code}).", "", f"Retries left: {remaining}", ""]
            detail += self.lines[-8:]
            self.draw_shell(
                "Failure",
                detail,
                "Up/Down Select    Enter Confirm",
                left_title="Options",
            )
            self.draw_left(labels, selected)
            self.stdscr.refresh()

            ch = self.stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(labels)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(labels)
            elif ch in (curses.KEY_ENTER, 10, 13):
                return ("retry", "source", "quit")[selected]
    def confirm_quit(self) -> bool:
        return self.confirm(
            "Quit first boot setup? It will not run again and the payload will be removed.",
            default=False,
        )

    def cleanup(self) -> None:
        for path in (UNIT, WANTS):
            try:
                path.unlink()
            except OSError:
                pass
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        for path in (INSTALL_DIR, BUNDLED_DIR, p("/etc/vemu")):
            shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(PAYLOAD_DIR, ignore_errors=True)
        shutil.rmtree(MOUNT_DIR, ignore_errors=True)

    def finish(
        self, status: str, message: str, *, auto_reboot: bool = False
    ) -> None:
        VAR_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if status == "done":
                DONE.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
            else:
                SKIPPED.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
        except OSError:
            pass
        write_log(f"firstboot {status}")

        labels = ["Restart now", "Continue to desktop"]
        selected = 0
        deadline = (
            time.monotonic() + AUTO_REBOOT_SECONDS
            if auto_reboot and status == "done"
            else None
        )
        last_remaining: int | None = None
        while True:
            remaining = 0
            if deadline is not None:
                remaining = max(0, int(deadline - time.monotonic() + 0.999))
                if remaining == 0:
                    write_log("automatic restart after successful setup")
                    restore_console_status()
                    subprocess.run(
                        ["systemctl", "reboot"], capture_output=True
                    )
                    return
            if deadline is not None and remaining == last_remaining:
                # Keep polling the keyboard without repainting every frame.
                ch = self.stdscr.getch()
            else:
                body = [
                    message,
                    "",
                    "The first-boot service will be removed",
                    "when you exit.",
                    "",
                    f"Log: {LOG}",
                ]
                footer = "Up/Down Select    Enter Confirm    Q Continue"
                if deadline is not None:
                    body += [
                        "",
                        f"Automatic restart in {remaining}s.",
                        "Press any key to stay on the desktop.",
                    ]
                    footer = (
                        "Enter Confirm    Q Continue    "
                        "Any key cancels auto restart"
                    )
                self.draw_shell(
                    "Result",
                    body,
                    footer,
                    left_title="Result",
                )
                self.draw_left(labels, selected)
                self.stdscr.refresh()
                last_remaining = remaining
                ch = self.stdscr.getch()

            if ch != -1 and deadline is not None:
                deadline = None
                last_remaining = None
            if ch in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(labels)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(labels)
            elif ch in (curses.KEY_ENTER, 10, 13):
                if selected == 0:
                    restore_console_status()
                    subprocess.run(
                        ["systemctl", "reboot"], capture_output=True
                    )
                    return
                restore_console_status()
                return
            elif ch in (ord("r"), ord("R")):
                restore_console_status()
                subprocess.run(["systemctl", "reboot"], capture_output=True)
                return
            elif ch in (ord("q"), ord("Q"), 27):
                restore_console_status()
                return


def curses_setup() -> None:
    import curses

    curses.curs_set(0)
    curses.noecho()
    curses.cbreak()


def main() -> int:
    try:
        import curses
    except ImportError:
        return 1

    suppress_console_status()
    app = Tui()
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # keep a log even on a TUI crash
        write_log(f"loader crashed: {exc!r}")
        raise
    finally:
        restore_console_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
