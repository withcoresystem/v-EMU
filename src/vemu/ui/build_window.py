"""Modeless build log window for Image Builder jobs."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QTextOption
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..imagebuilder import BuildJob
from .components import InfoBadge
from .theme import DARK, LIGHT


class BuildWindow(QMainWindow):
    finished = Signal(object)
    hidden = Signal(str)
    closed = Signal(object)

    def __init__(self, job: BuildJob, *, dark: bool = True) -> None:
        super().__init__(None)
        self.job = job
        self.dark = dark
        self._tokens = DARK if dark else LIGHT
        self._finished_emitted = False
        self._allow_close = False
        self._last_log = ""
        self._log_count_offset = 0
        self._download_count = 0
        self._cached_count = 0
        self._rate_samples: list[tuple[float, int]] = []
        self._last_progress_time: float | None = None
        self._harmless_count = 0
        self.setWindowTitle(f"Building {job.image.name}")
        self.resize(920, 620)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title = QLabel(f"Building {job.image.name}")
        self.title.setObjectName("DialogTitle")
        header.addWidget(self.title)
        header.addStretch(1)
        self.status = QLabel()
        self.status.setObjectName("Muted")
        header.addWidget(self.status)
        layout.addLayout(header)

        meta = QHBoxLayout()
        self.target = QLabel(
            f"{job.image.target} · {job.image.created}"
        )
        self.target.setObjectName("Muted")
        meta.addWidget(self.target)
        meta.addStretch(1)
        self.elapsed = QLabel("00:00")
        self.elapsed.setObjectName("Muted")
        meta.addWidget(self.elapsed)
        layout.addLayout(meta)

        self.download_label = QLabel("Download: waiting for packages…")
        self.download_label.setObjectName("Muted")
        layout.addWidget(self.download_label)
        self.download_bar = QProgressBar()
        self.download_bar.setTextVisible(False)
        self.download_bar.setFixedHeight(6)
        self.download_bar.setRange(0, 0)
        self.download_bar.hide()
        layout.addWidget(self.download_bar)

        self.warning_row = QWidget()
        warning_layout = QHBoxLayout(self.warning_row)
        warning_layout.setContentsMargins(0, 0, 0, 0)
        warning_layout.setSpacing(6)
        self.warning_badge = InfoBadge(
            "Known harmless osbuild warning: "
            "/sys/fs/selinux/checkreqprot is read-only in the build "
            "sandbox. It does not fail the build. The raw log still "
            "contains the line; Save log keeps it.",
            kind="info",
            color=self._tokens.muted,
        )
        self.warning_label = QLabel("Known harmless warning hidden")
        self.warning_label.setObjectName("Muted")
        self.warning_label.setToolTip(self.warning_badge.toolTip())
        warning_layout.addWidget(self.warning_badge)
        warning_layout.addWidget(self.warning_label)
        warning_layout.addStretch(1)
        self.warning_row.hide()
        layout.addWidget(self.warning_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log.setWordWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        self.log.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.log.setFont(
            QFontDatabase.systemFont(
                QFontDatabase.SystemFont.FixedFont
            )
        )
        layout.addWidget(self.log, 1)

        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel build")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.cancel_button)
        self.save_button = QPushButton("Save log…")
        self.save_button.setObjectName("SecondaryButton")
        self.save_button.clicked.connect(self._save_log)
        self.save_button.hide()
        actions.addWidget(self.save_button)
        actions.addStretch(1)
        self.hide_button = QPushButton("Hide")
        self.hide_button.setObjectName("SecondaryButton")
        self.hide_button.clicked.connect(self.hide)
        actions.addWidget(self.hide_button)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("SecondaryButton")
        self.close_button.clicked.connect(self._close)
        self.close_button.hide()
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.setCentralWidget(root)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        self._poll()

    # ------------------------------------------------------------------ state

    def _poll(self) -> None:
        state = self.job.poll()
        terminal = state in {"Succeeded", "Failed", "Cancelled"}
        if state == "Succeeded":
            heading = f"Built {self.job.image.name}"
        elif terminal:
            heading = f"Build {state.lower()} · {self.job.image.name}"
        else:
            heading = f"Building {self.job.image.name}"
        self.title.setText(heading)
        self.setWindowTitle(heading)
        self.elapsed.setText(self._format_elapsed(self.job.elapsed()))
        if self.job.preview:
            self.status.setText("Queued (preview)")
        else:
            self.status.setText(state)
        self.cancel_button.setEnabled(not terminal)
        self.cancel_button.setVisible(not terminal)
        self.hide_button.setVisible(not terminal)
        self.save_button.setVisible(terminal)
        self.close_button.setVisible(terminal)
        self._update_log()
        self._update_network()
        if terminal:
            self._timer.stop()
            if not self._finished_emitted:
                self._finished_emitted = True
                self.finished.emit(self.job)

    def _update_log(self) -> None:
        path: Path = self.job.log_path
        text = ""
        if path.is_file():
            try:
                size = path.stat().st_size
                with path.open("rb") as handle:
                    if size < self._log_count_offset:
                        self._log_count_offset = 0
                        self._download_count = 0
                        self._cached_count = 0
                        self._harmless_count = 0
                        self._rate_samples.clear()
                        self._last_progress_time = None
                    handle.seek(self._log_count_offset)
                    chunk = handle.read()
                    complete = chunk.rfind(b"\n") + 1
                    if complete:
                        body = chunk[:complete]
                        downloaded = body.count(b"Downloaded Packages")
                        cached = body.count(
                            b"Already downloaded Packages"
                        )
                        harmless = body.count(b"checkreqprot")
                        self._log_count_offset += complete
                        if downloaded or cached:
                            self._download_count += downloaded
                            self._cached_count += cached
                            self._last_progress_time = time.monotonic()
                        if harmless:
                            self._harmless_count += harmless
                    handle.seek(max(0, size - 65536))
                    text = handle.read().decode(
                        "utf-8", errors="replace"
                    )
            except OSError as exc:
                text = f"Cannot read build log: {exc}"
        elif self.job.preview:
            text = (
                "Preview adapter: no privileged build process was started.\n"
                "Install image-builder/osbuild on the host and restart vEMU "
                "to use the real adapter."
            )
        shown = self._filter_harmless(text)
        if shown != self._last_log:
            self._last_log = shown
            self.log.setPlainText(shown)
            bar = self.log.verticalScrollBar()
            bar.setValue(bar.maximum())
        if self._harmless_count:
            self.warning_label.setText(
                f"Known harmless warning hidden ({self._harmless_count})"
            )
            if self.warning_row.isHidden():
                self.warning_row.show()
        else:
            self.warning_row.hide()

    @staticmethod
    def _filter_harmless(text: str) -> str:
        lines = []
        for line in text.splitlines():
            if (
                "checkreqprot" in line
                and "Read-only file system" in line
            ):
                continue
            lines.append(line)
        return "\n".join(lines)

    def _update_network(self) -> None:
        if self.job.preview:
            self.download_label.hide()
            self.download_bar.hide()
            return
        self.download_label.show()
        now = time.monotonic()
        state = self.job.state
        running = state in {"Queued", "Running"}
        terminal = state in {"Succeeded", "Failed", "Cancelled"}
        total = self.job.expected_packages
        ready = self._download_count + self._cached_count
        if state == "Succeeded" and not total and ready:
            total = ready
        if terminal and total and ready < total:
            self._cached_count += total - ready
            ready = total
        if running:
            self._rate_samples.append((now, ready))
            while (
                len(self._rate_samples) > 2
                and now - self._rate_samples[0][0] > 30
            ):
                self._rate_samples.pop(0)
        rate = 0.0
        if len(self._rate_samples) >= 2:
            first_time, first_count = self._rate_samples[0]
            span = now - first_time
            if span > 0.5:
                rate = (ready - first_count) / span * 60
        downloading = "org.osbuild.librepo" in self._last_log[-4096:]
        idle = (
            int(now - self._last_progress_time)
            if self._last_progress_time is not None
            else 0
        )
        if state == "Succeeded":
            text = "Build complete"
        elif terminal:
            text = f"Build {state.lower()}"
        elif downloading:
            text = "Packages"
        elif ready:
            text = "Packages ready"
        else:
            text = "Packages"
        if total:
            text += f": {ready} / {total}"
        elif ready:
            text += f": {ready}"
        text += f" · {self._download_count} downloaded"
        if self._cached_count:
            text += f" · {self._cached_count} cached"
        if running and downloading and ready:
            text += f" · {rate:.0f} pkg/min"
        if running and downloading and idle >= 30:
            text += f" · last package {idle}s ago"
        if not downloading and not ready and not terminal:
            text += " · waiting for download…"
        slow = (
            running
            and downloading
            and ready >= 5
            and rate < 30
            and (not total or ready < total)
            and self.job.elapsed() > 60
        )
        if slow:
            text += " · slow, you can cancel and retry"
        self.download_label.setText(text)
        color = self._tokens.warning if slow else self._tokens.muted
        self.download_label.setStyleSheet(f"color: {color};")

        if total:
            self.download_bar.setRange(0, total)
            self.download_bar.setValue(min(ready, total))
        else:
            self.download_bar.setRange(0, 0)
        if (running and downloading and (ready > 0 or total > 0)) or (
            terminal and total
        ):
            self.download_bar.show()
        else:
            self.download_bar.hide()

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 60:02d}:{total % 60:02d}"

    def _save_log(self) -> None:
        source = self.job.log_path
        suggested = f"{self.job.image.name}-build.log"
        target, _filter = QFileDialog.getSaveFileName(
            self,
            "Save build log",
            suggested,
            "Log files (*.log);;All files (*)",
        )
        if not target:
            return
        try:
            if source.is_file():
                shutil.copyfile(source, target)
            else:
                Path(target).write_text(
                    self.log.toPlainText() + "\n", encoding="utf-8"
                )
        except OSError as exc:
            QMessageBox.critical(
                self, "Save build log", f"Cannot save the log: {exc}"
            )
            return
        self.statusBar().showMessage(f"Log saved to {target}", 6000)

    def _close(self) -> None:
        self._allow_close = True
        self.close()

    def _cancel(self) -> None:
        if self.job.state in {"Succeeded", "Failed", "Cancelled"}:
            return
        answer = QMessageBox.question(
            self,
            "Cancel build",
            "Cancel this build? The log and partial artifacts are kept.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.job.cancel()
        self._poll()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        terminal = self.job.state in {"Succeeded", "Failed", "Cancelled"}
        if self._allow_close or terminal:
            super().closeEvent(event)
            self.closed.emit(self.job)
            return
        self.hide()
        event.ignore()
        self.hidden.emit(self.job.image.name)

    def shutdown(self) -> None:
        self._timer.stop()
        self._allow_close = True
        self.close()


__all__ = ["BuildWindow"]
