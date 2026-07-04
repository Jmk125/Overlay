"""
Main application window - manages screen transitions
Landing → Matching → Viewer
"""
import os
import sys
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget, QFileDialog,
    QMessageBox, QApplication, QMenuBar, QMenu
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QFont, QColor, QPalette, QIcon

from core.models import OverlaySet
from core.persistence import (
    load_settings, save_settings, save_project, load_project, export_notes
)
from core import renderer as R
from ui.landing import LandingScreen
from ui.matching import MatchingScreen
from ui.viewer import OverlayViewer
from ui.settings_dialog import SettingsDialog

SETTINGS_PATH = os.path.expanduser("~/.drawing_overlay/settings.json")


def resource_path(name: str):
    """Locate a bundled resource next to the app, the PyInstaller bundle, or
    the source tree. Returns the full path, or None if not found."""
    bases = []
    if getattr(sys, 'frozen', False):
        bases.append(getattr(sys, '_MEIPASS', ''))
        bases.append(os.path.dirname(sys.executable))
    bases.append(os.path.dirname(os.path.abspath(__file__)))
    for base in bases:
        if base:
            path = os.path.join(base, name)
            if os.path.exists(path):
                return path
    return None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings(SETTINGS_PATH)
        # Point OCR at a bundled/explicit Tesseract if one is configured.
        R.configure_tesseract(self.settings.get('tesseract_path'))
        icon = resource_path('app.ico')
        if icon:
            self.setWindowIcon(QIcon(icon))
        self.setWindowTitle("Drawing Overlay Tool")
        self.setMinimumSize(1200, 750)
        self._apply_dark_theme()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self._show_landing()
        self._build_menu()

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #121212;
                color: #e0e0e0;
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 11px;
            }
            QLabel { color: #e0e0e0; }
            QGroupBox { color: #bbb; }
            QScrollBar:vertical {
                background: #1e1e1e; width: 10px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background: #444; border-radius: 5px; min-height: 20px;
            }
            QScrollBar:horizontal {
                background: #1e1e1e; height: 10px; border: none;
            }
            QScrollBar::handle:horizontal {
                background: #444; border-radius: 5px;
            }
            QComboBox {
                background: #2a2a2a; color: #eee;
                border: 1px solid #555; padding: 3px; border-radius: 3px;
            }
            QComboBox QAbstractItemView {
                background: #2a2a2a; color: #eee; selection-background-color: #3a6491;
            }
            QDoubleSpinBox, QSpinBox {
                background: #2a2a2a; color: #eee;
                border: 1px solid #555; padding: 2px;
            }
            QListWidget::item:selected { background: #2a4a6b; }
            QProgressBar { color: #eee; }
            QMenuBar { background: #1a1a1a; color: #ddd; }
            QMenuBar::item:selected { background: #2a4a6b; }
            QMenu { background: #1e1e1e; color: #ddd; border: 1px solid #444; }
            QMenu::item:selected { background: #2a4a6b; }
        """)

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        new_act = QAction("New Overlay", self)
        new_act.setShortcut("Ctrl+N")
        new_act.triggered.connect(self._show_landing)
        file_menu.addAction(new_act)

        open_act = QAction("Open Project...", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._open_project_dialog)
        file_menu.addAction(open_act)

        save_act = QAction("Save Project...", self)
        save_act.setShortcut("Ctrl+S")
        save_act.triggered.connect(self._save_project_dialog)
        file_menu.addAction(save_act)

        file_menu.addSeparator()
        export_notes_act = QAction("Export Notes...", self)
        export_notes_act.triggered.connect(self._export_notes_dialog)
        file_menu.addAction(export_notes_act)

        file_menu.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        edit_menu = menubar.addMenu("Edit")
        prefs_act = QAction("Preferences...", self)
        prefs_act.setShortcut("Ctrl+,")
        prefs_act.triggered.connect(self._open_settings_dialog)
        edit_menu.addAction(prefs_act)

        help_menu = menubar.addMenu("Help")
        shortcuts_act = QAction("Keyboard Shortcuts", self)
        shortcuts_act.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_act)

    def _show_landing(self):
        self._clear_stack()
        screen = LandingScreen(self.settings)
        screen.start_matching.connect(self._show_matching)
        screen.start_viewer.connect(self._show_viewer)
        screen.open_project.connect(self._load_project)
        self.stack.addWidget(screen)
        self.stack.setCurrentWidget(screen)

    def _show_matching(self, overlay_set: OverlaySet):
        self._clear_stack()
        screen = MatchingScreen(overlay_set, self.settings)
        screen.matching_done.connect(self._show_viewer)
        self.stack.addWidget(screen)
        self.stack.setCurrentWidget(screen)

    def _show_viewer(self, overlay_set: OverlaySet):
        self._clear_stack()
        screen = OverlayViewer(overlay_set, self.settings)
        screen.back_to_matching.connect(lambda: self._show_matching(overlay_set))
        screen.save_project.connect(self._save_project_dialog_with_set)
        self.stack.addWidget(screen)
        self.stack.setCurrentWidget(screen)
        self._current_overlay_set = overlay_set

    def _clear_stack(self):
        while self.stack.count():
            w = self.stack.widget(0)
            # Let screens stop background threads before they're destroyed.
            if hasattr(w, '_shutdown'):
                try:
                    w._shutdown()
                except Exception:
                    pass
            self.stack.removeWidget(w)
            w.deleteLater()

    def _export_notes_dialog(self):
        if not hasattr(self, '_current_overlay_set') or not self._current_overlay_set.pairs:
            QMessageBox.information(self, "Export Notes",
                                    "Open or create an overlay first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Notes",
            os.path.join(self.settings.get('export_path', ''), "overlay_notes.xlsx"),
            "Excel (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        try:
            written = export_notes(self._current_overlay_set, path)
            note = ""
            if written.lower().endswith('.csv') and not path.lower().endswith('.csv'):
                note = "\n\n(openpyxl not installed — wrote a CSV instead, which Excel opens.)"
            QMessageBox.information(self, "Notes Exported",
                                    f"Saved to:\n{written}{note}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

    def _open_settings_dialog(self):
        current = self.stack.currentWidget()
        # Seed the dialog's background choice from the open project so it
        # reflects what's actually on screen.
        if isinstance(current, OverlayViewer):
            self.settings['canvas_bg'] = current.overlay_set.canvas_bg

        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            # Merge choices into the live settings dict (shared with screens).
            self.settings.update(dlg.updated_settings())
            save_settings(self.settings, SETTINGS_PATH)
            # Re-point OCR in case the Tesseract path changed.
            R.configure_tesseract(self.settings.get('tesseract_path'))
            # Apply control/render preferences immediately if a viewer is open.
            if isinstance(current, OverlayViewer):
                current.apply_settings()
                current.set_background_mode(self.settings.get('canvas_bg', 'white'))
                current.apply_render_dpi(self.settings.get('render_dpi', 120),
                                         self.settings.get('export_dpi', 200))

    def _open_project_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project",
            self.settings.get('last_open_dir', ''),
            "Overlay Projects (*.overlay)"
        )
        if path:
            self._load_project(path)

    def _load_project(self, path: str):
        try:
            overlay_set = load_project(path)
            self._show_viewer(overlay_set)
            self.settings['last_open_dir'] = os.path.dirname(path)
            save_settings(self.settings, SETTINGS_PATH)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Could not load project:\n{e}")

    def _save_project_dialog(self):
        if hasattr(self, '_current_overlay_set'):
            self._save_project_dialog_with_set(self._current_overlay_set)

    def _save_project_dialog_with_set(self, overlay_set: OverlaySet):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project",
            self.settings.get('last_open_dir', ''),
            "Overlay Projects (*.overlay)"
        )
        if path:
            if not path.endswith('.overlay'):
                path += '.overlay'
            try:
                save_project(overlay_set, path)
                self.settings['last_open_dir'] = os.path.dirname(path)
                save_settings(self.settings, SETTINGS_PATH)
                QMessageBox.information(self, "Saved", f"Project saved to:\n{path}")
            except Exception as e:
                QMessageBox.critical(self, "Save Error", str(e))

    def _show_shortcuts(self):
        QMessageBox.information(self, "Keyboard Shortcuts", """
Overlay Viewer:
  1 — Overlay (both drawings)
  2 — Set A only
  3 — Set B only
  F — Fit to window
  Scroll — Zoom in/out (configurable in Preferences)
  Right-click drag — Pan (configurable in Preferences)
  Shift+drag — Fine movement/rotation

General:
  Ctrl+N — New overlay
  Ctrl+O — Open project
  Ctrl+S — Save project
  Ctrl+, — Preferences
  Ctrl+Q — Quit

Zoom, pan and antialiasing can be customized in Edit ▸ Preferences.
        """.strip())


def main():
    # Must run before any multiprocessing (parallel OCR) is used, so that
    # worker processes in a frozen .exe don't relaunch the whole GUI.
    import multiprocessing
    multiprocessing.freeze_support()

    app = QApplication(sys.argv)
    app.setApplicationName("Drawing Overlay Tool")
    app.setFont(QFont("Segoe UI", 10))
    icon = resource_path('app.ico')
    if icon:
        app.setWindowIcon(QIcon(icon))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
