"""
Preferences / Settings dialog.

Lets the user customize viewer controls (zoom & pan behavior), rendering
quality (antialiasing, default DPI) and default drawing colors. Settings are
returned via updated_settings() and persisted by the caller.
"""
import os
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QPushButton, QLabel,
    QComboBox, QCheckBox, QSpinBox, QGroupBox, QColorDialog, QFrame,
    QLineEdit, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from core import renderer as R


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
        self.dpi_spin.setValue(int(self._settings.get('render_dpi', 120)))
        self.dpi_spin.setSuffix(" DPI")
        rform.addRow("Screen render DPI:", self.dpi_spin)

        dpi_hint = QLabel("On-screen working resolution — lower is faster. Exports "
                          "use their own DPI below, so lowering this won't hurt "
                          "export quality.")
        dpi_hint.setStyleSheet("color: #888; font-size: 9px;")
        dpi_hint.setWordWrap(True)
        rform.addRow("", dpi_hint)

        self.export_dpi_spin = QSpinBox()
        self.export_dpi_spin.setRange(72, 600)
        self.export_dpi_spin.setValue(int(self._settings.get('export_dpi', 200)))
        self.export_dpi_spin.setSuffix(" DPI")
        rform.addRow("Export DPI:", self.export_dpi_spin)

        edpi_hint = QLabel("Resolution used when exporting PNG/PDF — higher is sharper.")
        edpi_hint.setStyleSheet("color: #888; font-size: 9px;")
        edpi_hint.setWordWrap(True)
        rform.addRow("", edpi_hint)

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

        # ── OCR (Tesseract) ───────────────────────────────────────
        ocr = QGroupBox("OCR (sheet matching)")
        ocr.setStyleSheet(self._group_style())
        ocr_layout = QVBoxLayout(ocr)
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Tesseract:"))
        self.tess_edit = QLineEdit(self._settings.get('tesseract_path', ''))
        self.tess_edit.setPlaceholderText("Auto-detected — or pick tesseract.exe / its folder")
        path_row.addWidget(self.tess_edit, 1)
        browse = QPushButton("Browse…")
        browse.setStyleSheet("background:#2a4a6b; color:white; border:none; padding:4px 10px; border-radius:3px;")
        browse.clicked.connect(self._browse_tesseract)
        path_row.addWidget(browse)
        ocr_layout.addLayout(path_row)

        self.tess_status = QLabel("")
        self.tess_status.setWordWrap(True)
        ocr_layout.addWidget(self.tess_status)
        self._refresh_tess_status()
        self.tess_edit.textChanged.connect(self._refresh_tess_status)
        root.addWidget(ocr)

        # ── Updates ──────────────────────────────────────────────
        updates = QGroupBox("Updates")
        updates.setStyleSheet(self._group_style())
        updates_layout = QVBoxLayout(updates)

        update_row = QHBoxLayout()
        update_row.addWidget(QLabel("Version host:"))
        self.update_url_edit = QLineEdit(self._settings.get('update_server_url', 'http://10.0.10.180:3090/'))
        self.update_url_edit.setPlaceholderText("http://10.0.10.180:3090/")
        update_row.addWidget(self.update_url_edit, 1)
        updates_layout.addLayout(update_row)

        self.update_check = QCheckBox("Check for updates when the tool starts")
        self.update_check.setChecked(bool(self._settings.get('check_for_updates', True)))
        updates_layout.addWidget(self.update_check)

        update_hint = QLabel("The app looks for JSON update metadata at the host URL and common version endpoints.")
        update_hint.setStyleSheet("color: #888; font-size: 9px;")
        update_hint.setWordWrap(True)
        updates_layout.addWidget(update_hint)

        root.addWidget(updates)

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
        self._settings['export_dpi'] = self.export_dpi_spin.value()
        self._settings['tesseract_path'] = self.tess_edit.text().strip()
        self._settings['update_server_url'] = self.update_url_edit.text().strip() or 'http://10.0.10.180:3090/'
        self._settings['check_for_updates'] = self.update_check.isChecked()
        self._settings['default_color_a'] = self.color_a.color()
        self._settings['default_color_b'] = self.color_b.color()
        return self._settings

    def _browse_tesseract(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate tesseract executable", "",
            "Tesseract (tesseract.exe tesseract);;All files (*)")
        if path:
            self.tess_edit.setText(path)

    def _refresh_tess_status(self):
        """Show whether the current path (or auto-detection) finds Tesseract."""
        path = self.tess_edit.text().strip()
        ok = R.configure_tesseract(path) or R.tesseract_available()
        if ok and R.tesseract_available():
            self.tess_status.setText("✓ Tesseract found — auto sheet matching is available.")
            self.tess_status.setStyleSheet("color:#27a350; font-size:10px;")
        else:
            self.tess_status.setText(
                "✗ Tesseract not found. Install it, or drop a portable "
                "'tesseract' folder next to the app, or point to tesseract.exe above.")
            self.tess_status.setStyleSheet("color:#ff8c69; font-size:10px;")

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
