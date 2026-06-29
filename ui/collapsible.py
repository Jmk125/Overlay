"""
Small reusable collapsible UI helpers used by the overlay viewer.

CollapsibleSection — a titled section with a clickable header that expands /
collapses its body. Used for the View / Align / Rotation / Scale / Export
groups in the right-hand tools pane.
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QToolButton, QFrame
from PyQt6.QtCore import Qt


class CollapsibleSection(QWidget):
    def __init__(self, title: str, collapsed: bool = True, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet("""
            QToolButton {
                background: #1f1f1f; color: #ddd; border: 1px solid #333;
                border-radius: 4px; padding: 6px; font-weight: bold;
                font-size: 10px; text-align: left;
            }
            QToolButton:hover { background: #262626; }
            QToolButton:checked { background: #233246; color: #fff; border-color: #3a5d82; }
        """)
        self.toggle.toggled.connect(self._on_toggled)
        outer.addWidget(self.toggle)

        self.body = QFrame()
        self.body.setStyleSheet("QFrame { border: none; }")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(6, 6, 6, 6)
        self.body_layout.setSpacing(6)
        outer.addWidget(self.body)

        # Initial state
        self.toggle.setChecked(not collapsed)
        self._on_toggled(not collapsed)

    def _on_toggled(self, expanded: bool):
        self.body.setVisible(expanded)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    def addWidget(self, w):
        self.body_layout.addWidget(w)

    def addLayout(self, lay):
        self.body_layout.addLayout(lay)

    def set_expanded(self, expanded: bool):
        self.toggle.setChecked(expanded)
