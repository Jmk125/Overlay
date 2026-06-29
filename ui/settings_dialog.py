"""
Preferences / Settings dialog.

Lets the user customize viewer controls (zoom & pan behavior), rendering
quality (antialiasing, default DPI) and default drawing colors. Settings are
returned via updated_settings() and persisted by the caller.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QComboBox, QCheckBox, QSpinBox, QGroupBox, QColorDialog, QFrame
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont


class _ColorSwatch(QPushButton):
    """A small button that displays and edits a hex color."""
    def __init__(self, hex_color: str, parent=None):
        super().__init__(parent)
        self.hex_color = hex_color or '#000000'
        self.setFixedSize(48, 26)
        self._refresh()
        self.clicked.connect(self._pick)

    def _refresh(self):
        self.setStyleSheet(
            f"background-color: {self.hex_color}; border: 2px solid #555; border-radius: 4px;"
        )

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.hex_color), self, "Pick Color")
        if c.isValid():
            self.hex_color = c.name()
            self._refresh()

    def color(self) -> str:
        return self.hex_color


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        # Work on a copy so Cancel leaves the originals untouched.
        self._settings = dict(settings)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(420)
        self.setStyleSheet(self._style())
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("Preferences")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        root.addWidget(title)

        # ── Controls ──────────────────────────────────────────────
        controls = QGroupBox("Controls")
        controls.setStyleSheet(self._group_style())
        cform = QFormLayout(controls)
        cform.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        cform.setSpacing(8)

        self.zoom_combo = QComboBox()
        self.zoom_combo.addItem("Scroll to zoom", True)
        self.zoom_combo.addItem("Ctrl + Scroll to zoom", False)
        self.zoom_combo.setCurrentIndex(0 if self._settings.get('zoom_on_scroll', True) else 1)
        cform.addRow("Zoom:", self.zoom_combo)

        self.pan_combo = QComboBox()
        for label, value in [("Right-click drag", 'right'),
                             ("Middle-click drag", 'middle'),
                             ("Left-click drag", 'left')]:
            self.pan_combo.addItem(label, value)
        cur_pan = self._settings.get('pan_button', 'right')
        idx = self.pan_combo.findData(cur_pan)
        self.pan_combo.setCurrentIndex(idx if idx >= 0 else 0)
        cform.addRow("Pan:", self.pan_combo)

        pan_hint = QLabel("Left-click drag pans only when no align tool is active.")
        pan_hint.setStyleSheet("color: #888; font-size: 9px;")
        pan_hint.setWordWrap(True)
        cform.addRow("", pan_hint)

        root.addWidget(controls)

        # ── Rendering ─────────────────────────────────────────────
        render = QGroupBox("Rendering")
        render.setStyleSheet(self._group_style())
        rform = QFormLayout(render)
        rform.setSpacing(8)

        self.bg_combo = QComboBox()
        self.bg_combo.addItem("White", 'white')
        self.bg_combo.addItem("Dark", 'dark')
        bg_idx = self.bg_combo.findData(self._settings.get('canvas_bg', 'white'))
        self.bg_combo.setCurrentIndex(bg_idx if bg_idx >= 0 else 0)
        rform.addRow("Canvas background:", self.bg_combo)

        self.aa_check = QCheckBox("Smooth edges (antialiasing)")
        self.aa_check.setChecked(bool(self._settings.get('antialiasing', True)))
        rform.addRow("Quality:", self.aa_check)

        aa_hint = QLabel("Softens jagged lines when zooming low-DPI renders.")
        aa_hint.setStyleSheet("color: #888; font-size: 9px;")
        aa_hint.setWordWrap(True)
        rform.addRow("", aa_hint)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setValue(int(self._settings.get('render_dpi', 150)))
        self.dpi_spin.setSuffix(" DPI")
        rform.addRow("Default render DPI:", self.dpi_spin)

        dpi_hint = QLabel("Higher DPI = sharper but slower. Applies to new overlays.")
        dpi_hint.setStyleSheet("color: #888; font-size: 9px;")
        dpi_hint.setWordWrap(True)
        rform.addRow("", dpi_hint)

        root.addWidget(render)

        # ── Default colors ────────────────────────────────────────
        colors = QGroupBox("Default Colors")
        colors.setStyleSheet(self._group_style())
        col_row = QHBoxLayout(colors)
        col_row.addWidget(QLabel("Set A:"))
        self.color_a = _ColorSwatch(self._settings.get('default_color_a', '#FF0000'))
        col_row.addWidget(self.color_a)
        col_row.addSpacing(20)
        col_row.addWidget(QLabel("Set B:"))
        self.color_b = _ColorSwatch(self._settings.get('default_color_b', '#0000FF'))
        col_row.addWidget(self.color_b)
        col_row.addStretch()
        root.addWidget(colors)

        # ── Buttons ───────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")
        root.addWidget(sep)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet("background: #444; color: #ddd; border: none; padding: 6px 16px; border-radius: 4px;")
        save = QPushButton("Save")
        save.clicked.connect(self.accept)
        save.setStyleSheet("background: #1a6b35; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        root.addLayout(btn_row)

    def updated_settings(self) -> dict:
        """Return the settings dict with the user's choices merged in."""
        self._settings['zoom_on_scroll'] = bool(self.zoom_combo.currentData())
        self._settings['pan_button'] = self.pan_combo.currentData()
        self._settings['antialiasing'] = self.aa_check.isChecked()
        self._settings['canvas_bg'] = self.bg_combo.currentData()
        self._settings['render_dpi'] = self.dpi_spin.value()
        self._settings['default_color_a'] = self.color_a.color()
        self._settings['default_color_b'] = self.color_b.color()
        return self._settings

    def _group_style(self) -> str:
        return """
            QGroupBox {
                border: 1px solid #333; border-radius: 5px;
                margin-top: 8px; padding: 10px; color: #bbb; font-weight: bold;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; }
        """

    def _style(self) -> str:
        return """
            QDialog { background: #161616; color: #e0e0e0; }
            QLabel { color: #ddd; }
            QComboBox, QSpinBox {
                background: #2a2a2a; color: #eee; border: 1px solid #555;
                padding: 3px; border-radius: 3px; min-width: 160px;
            }
            QComboBox QAbstractItemView {
                background: #2a2a2a; color: #eee; selection-background-color: #3a6491;
            }
            QCheckBox { color: #ddd; }
        """
