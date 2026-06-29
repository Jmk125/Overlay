"""
Landing / New Overlay screen
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
    QColorDialog, QFrame, QSizePolicy, QMessageBox, QSpinBox,
    QGroupBox, QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPixmap, QPainter
import os
from core.models import DrawingPage, OverlaySet
from core import renderer as R


class ColorButton(QPushButton):
    """A button that shows and sets a color"""
    def __init__(self, hex_color: str, parent=None):
        super().__init__(parent)
        self.hex_color = hex_color
        self._update_style()
        self.setFixedSize(40, 28)
        self.clicked.connect(self._pick_color)

    def _update_style(self):
        self.setStyleSheet(
            f"background-color: {self.hex_color}; border: 2px solid #555; border-radius: 4px;"
        )

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.hex_color), self, "Pick Drawing Color")
        if c.isValid():
            self.hex_color = c.name()
            self._update_style()

    def color(self) -> str:
        return self.hex_color


class PageListWidget(QWidget):
    """Shows loaded pages for one set with thumbnails"""
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.pages: list[DrawingPage] = []
        self._build_ui(label)

    def _build_ui(self, label):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QLabel(label)
        header.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        layout.addWidget(header)

        self.list_widget = QListWidget()
        self.list_widget.setMinimumHeight(200)
        self.list_widget.setStyleSheet("QListWidget { background: #1e1e1e; color: #eee; border: 1px solid #444; }")
        layout.addWidget(self.list_widget)

    def set_pages(self, pages: list[DrawingPage]):
        self.pages = pages
        self.list_widget.clear()
        for p in pages:
            label = p.sheet_number if p.sheet_number else p.display_name
            item = QListWidgetItem(f"  {label}  —  {os.path.basename(p.pdf_path)}")
            self.list_widget.addItem(item)


class LandingScreen(QWidget):
    start_matching = pyqtSignal(object)   # emits OverlaySet
    start_viewer = pyqtSignal(object)     # emits OverlaySet (single-drawing fast path)
    open_project = pyqtSignal(str)        # emits filepath

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.pages_a: list[DrawingPage] = []
        self.pages_b: list[DrawingPage] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(24, 24, 24, 24)

        # Title
        title = QLabel("Drawing Overlay Tool")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        root.addWidget(title)

        subtitle = QLabel("Compare two drawing sets with intelligent overlay, alignment, and version toggling.")
        subtitle.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        root.addWidget(subtitle)

        # Open existing project
        open_row = QHBoxLayout()
        open_btn = QPushButton("📂  Open Existing Project (.overlay)")
        open_btn.setFixedHeight(36)
        open_btn.setStyleSheet(self._btn_style("#2a4a6b", "#3a6491"))
        open_btn.clicked.connect(self._open_project)
        open_row.addWidget(open_btn)
        open_row.addStretch()
        root.addLayout(open_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #444;")
        root.addWidget(sep)

        new_label = QLabel("— or start a new overlay —")
        new_label.setStyleSheet("color: #888; font-size: 11px;")
        new_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(new_label)

        # Two set panels side by side
        sets_layout = QHBoxLayout()
        sets_layout.setSpacing(16)

        self.panel_a = self._build_set_panel("Set A (Base)", "a")
        self.panel_b = self._build_set_panel("Set B (Revision)", "b")
        sets_layout.addWidget(self.panel_a["widget"])
        sets_layout.addWidget(self.panel_b["widget"])
        root.addLayout(sets_layout)

        # Color row
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Set A color:"))
        self.color_btn_a = ColorButton(self.settings.get('default_color_a', '#FF0000'))
        color_row.addWidget(self.color_btn_a)
        color_row.addSpacing(24)
        color_row.addWidget(QLabel("Set B color:"))
        self.color_btn_b = ColorButton(self.settings.get('default_color_b', '#0000FF'))
        color_row.addWidget(self.color_btn_b)
        color_row.addSpacing(24)
        color_row.addWidget(QLabel("Render DPI:"))
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 300)
        self.dpi_spin.setValue(self.settings.get('render_dpi', 150))
        self.dpi_spin.setFixedWidth(70)
        color_row.addWidget(self.dpi_spin)
        color_row.addStretch()
        root.addLayout(color_row)

        # Start button
        self.start_btn = QPushButton("▶   Start Overlay")
        self.start_btn.setFixedHeight(44)
        self.start_btn.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        self.start_btn.setStyleSheet(self._btn_style("#1a6b35", "#27a350"))
        self.start_btn.clicked.connect(self._start)
        root.addWidget(self.start_btn)

        root.addStretch()

    def _build_set_panel(self, title: str, side: str) -> dict:
        group = QGroupBox(title)
        group.setStyleSheet("""
            QGroupBox { border: 1px solid #444; border-radius: 6px; margin-top: 8px; padding: 8px; color: #ddd; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; }
        """)
        layout = QVBoxLayout(group)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Label:"))
        name_edit = QLineEdit(title)
        name_edit.setStyleSheet("background: #2a2a2a; color: #eee; border: 1px solid #555; padding: 3px;")
        name_row.addWidget(name_edit)
        layout.addLayout(name_row)

        load_single_btn = QPushButton("Load Single PDF")
        load_single_btn.setStyleSheet(self._btn_style("#2a2a5e", "#3a3a8e"))
        layout.addWidget(load_single_btn)

        load_set_btn = QPushButton("Load Drawing Set (multi-page PDF)")
        load_set_btn.setStyleSheet(self._btn_style("#2a2a5e", "#3a3a8e"))
        layout.addWidget(load_set_btn)

        page_list = PageListWidget("")
        layout.addWidget(page_list)

        status = QLabel("No files loaded")
        status.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(status)

        panel = {
            'widget': group,
            'name_edit': name_edit,
            'page_list': page_list,
            'status': status,
        }

        load_single_btn.clicked.connect(lambda: self._load_single(side, panel))
        load_set_btn.clicked.connect(lambda: self._load_set(side, panel))

        return panel

    def _load_single(self, side: str, panel: dict):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Load PDF(s)", self.settings.get('last_open_dir', ''),
            "PDF Files (*.pdf)"
        )
        if not paths:
            return
        pages = []
        for path in paths:
            pages.append(DrawingPage(pdf_path=path, page_index=0,
                                      display_name=os.path.splitext(os.path.basename(path))[0]))
        self._assign_pages(side, pages)
        panel['page_list'].set_pages(pages)
        panel['status'].setText(f"{len(pages)} drawing(s) loaded")

    def _load_set(self, side: str, panel: dict):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Drawing Set PDF", self.settings.get('last_open_dir', ''),
            "PDF Files (*.pdf)"
        )
        if not path:
            return
        count = R.get_page_count(path)
        # Show page selector dialog
        from ui.page_selector import PageSelectorDialog
        dlg = PageSelectorDialog(path, count, self)
        if dlg.exec():
            selected_indices = dlg.selected_indices()
            pages = [DrawingPage(pdf_path=path, page_index=i,
                                  display_name=f"Page {i+1}")
                     for i in selected_indices]
            self._assign_pages(side, pages)
            panel['page_list'].set_pages(pages)
            panel['status'].setText(f"{len(pages)} page(s) selected from {os.path.basename(path)}")

    def _assign_pages(self, side: str, pages):
        if side == 'a':
            self.pages_a = pages
        else:
            self.pages_b = pages

    def _start(self):
        if not self.pages_a or not self.pages_b:
            QMessageBox.warning(self, "Missing Pages", "Please load pages for both Set A and Set B.")
            return

        overlay_set = OverlaySet(
            set_a_label=self.panel_a['name_edit'].text(),
            set_b_label=self.panel_b['name_edit'].text(),
            color_a=self.color_btn_a.color(),
            color_b=self.color_btn_b.color(),
            render_dpi=self.dpi_spin.value(),
        )
        overlay_set._pages_a = self.pages_a
        overlay_set._pages_b = self.pages_b

        # Single drawing on each side — skip matching, pair directly
        if len(self.pages_a) == 1 and len(self.pages_b) == 1:
            from core.models import OverlayPair
            pa = self.pages_a[0]
            pb = self.pages_b[0]
            pa.sheet_number = pa.sheet_number or pa.display_name
            pb.sheet_number = pb.sheet_number or pb.display_name
            overlay_set.pairs = [OverlayPair(page_a=pa, page_b=pb)]
            self.start_viewer.emit(overlay_set)
        else:
            self.start_matching.emit(overlay_set)

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", self.settings.get('last_open_dir', ''),
            "Overlay Projects (*.overlay)"
        )
        if path:
            self.open_project.emit(path)

    def _btn_style(self, bg: str, hover: str) -> str:
        return f"""
            QPushButton {{
                background: {bg}; color: white; border: none;
                border-radius: 5px; padding: 6px 14px;
            }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:pressed {{ background: {bg}; }}
        """
