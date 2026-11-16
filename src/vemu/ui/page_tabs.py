"""Segmented page tabs used by page-level peer views.

The component intentionally does not use QTabWidget: vEMU's visual language is
flat, border-first, and based on the existing button/nav tokens.  Consumers put
their content in a separate QStackedWidget and connect ``currentChanged``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from .tokens import CONTROL


class _PageTabButton(QPushButton):
    def __init__(self, label: str, tabs: "PageTabs", index: int) -> None:
        super().__init__(label)
        self._tabs = tabs
        self._index = index
        self.setObjectName("PageTab")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(label)
        self.setFixedHeight(CONTROL.height_sm)
        self.setMinimumWidth(82)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.key() in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
        ):
            self._tabs.move_focus(self, event.key())
            event.accept()
            return
        super().keyPressEvent(event)


class PageTabs(QWidget):
    """A compact segmented tab bar.

    This is a bar-only widget; the page owns a QStackedWidget and connects
    ``currentChanged`` to it.  Keep it page-level: do not nest page tabs.
    """

    currentChanged = Signal(int)

    def __init__(self, labels: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PageTabs")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(36)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._buttons: list[_PageTabButton] = []
        self._current = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, label in enumerate(labels):
            button = _PageTabButton(label, self, index)
            self._group.addButton(button)
            button.clicked.connect(
                lambda _checked=False, index=index: self.setCurrentIndex(
                    index
                )
            )
            layout.addWidget(button)
            self._buttons.append(button)

        if self._buttons:
            self._buttons[0].setChecked(True)

    @property
    def buttons(self) -> list[QPushButton]:
        return list(self._buttons)

    def currentIndex(self) -> int:  # noqa: N802 - Qt API
        return self._current

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt API
        if not self._buttons:
            return
        index = max(0, min(index, len(self._buttons) - 1))
        if index == self._current and self._buttons[index].isChecked():
            return
        self._current = index
        self._buttons[index].setChecked(True)
        self.currentChanged.emit(index)

    def move_focus(self, button: _PageTabButton, key: int) -> None:
        if not self._buttons:
            return
        index = self._buttons.index(button)
        if key == Qt.Key.Key_Home:
            index = 0
        elif key == Qt.Key.Key_End:
            index = len(self._buttons) - 1
        elif key == Qt.Key.Key_Left:
            index = (index - 1) % len(self._buttons)
        elif key == Qt.Key.Key_Right:
            index = (index + 1) % len(self._buttons)
        self.setCurrentIndex(index)
        self._buttons[index].setFocus(Qt.FocusReason.ShortcutFocusReason)


__all__ = ["PageTabs"]
