"""
Page selector dialog - shows thumbnails of all pages in a PDF
so user can pick which ones to include
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QWidget, QGridLayout, QCheckBox, QFrame
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from core import renderer as R


class ThumbnailLoader(QThread):
    thumbnail_ready = pyqtSignal(int, object)  # page_index, QPixmap

    def __init__(self, pdf_path: str, page_count: int):
        super().__init__()
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        for i in range(self.page_count):
            if self.cancelled:
                return
            try:
                pix = R.render_thumbnail(self.pdf_path, i, max_size=180)
                if not self.cancelled:
                    self.thumbnail_ready.emit(i, pix)
            except Exception:
                pass


class PageThumbWidget(QWidget):
    def __init__(self, page_index: int, parent=None):
        super().__init__(parent)
        self.page_index = page_index
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.img_label = QLabel()
        self.img_label.setFixedSize(180, 180)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet("background: #2a2a2a; border: 1px solid #555;")
        self.img_label.setText("Loading...")
        layout.addWidget(self.img_label)

        bottom = QHBoxLayout()
        self.checkbox = QCheckBox(f"Page {page_index + 1}")
        self.checkbox.setChecked(True)
        self.checkbox.setStyleSheet("color: #ddd;")
        bottom.addWidget(self.checkbox)
        layout.addLayout(bottom)

        self.setFixedWidth(200)
        self.setStyleSheet("background: #1a1a1a; border: 1px solid #333; border-radius: 4px;")

    def set_pixmap(self, pix):
        scaled = pix.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self.img_label.setPixmap(scaled)
        self.img_label.setText("")

    def is_selected(self) -> bool:
        return self.checkbox.isChecked()


class PageSelectorDialog(QDialog):
    def __init__(self, pdf_path: str, page_count: int, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.thumb_widgets: list[PageThumbWidget] = []
        self.setWindowTitle(f"Select Pages — {page_count} pages")
        self.setMinimumSize(800, 600)
        self.setStyleSheet("background: #121212; color: #eee;")
        self._build_ui()
        self._load_thumbnails()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Toolbar
        toolbar = QHBoxLayout()
        select_all = QPushButton("Select All")
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none = QPushButton("Select None")
        select_none.clicked.connect(lambda: self._set_all(False))
        for btn in [select_all, select_none]:
            btn.setStyleSheet("background: #2a2a5e; color: white; border: none; padding: 5px 12px; border-radius: 4px;")
        toolbar.addWidget(select_all)
        toolbar.addWidget(select_none)
        toolbar.addStretch()
        count_label = QLabel(f"{self.page_count} pages total")
        count_label.setStyleSheet("color: #888;")
        toolbar.addWidget(count_label)
        layout.addLayout(toolbar)

        # Scroll area with grid of thumbnails
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        container = QWidget()
        self.grid = QGridLayout(container)
        self.grid.setSpacing(8)

        cols = 4
        for i in range(self.page_count):
            thumb = PageThumbWidget(i)
            self.thumb_widgets.append(thumb)
            self.grid.addWidget(thumb, i // cols, i % cols)

        scroll.setWidget(container)
        layout.addWidget(scroll)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Use Selected Pages")
        ok_btn.clicked.connect(self.accept)
        for btn in [cancel_btn, ok_btn]:
            btn.setFixedHeight(36)
            btn.setStyleSheet("background: #2a5e2a; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        cancel_btn.setStyleSheet("background: #5e2a2a; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _load_thumbnails(self):
        self.loader = ThumbnailLoader(self.pdf_path, self.page_count)
        self.loader.thumbnail_ready.connect(self._on_thumbnail)
        self.loader.start()

    def closeEvent(self, event):
        if hasattr(self, 'loader') and self.loader.isRunning():
            self.loader.cancel()
            self.loader.wait(500)
        super().closeEvent(event)

    def _on_thumbnail(self, index: int, pix):
        if index < len(self.thumb_widgets):
            self.thumb_widgets[index].set_pixmap(pix)

    def _set_all(self, state: bool):
        for w in self.thumb_widgets:
            w.checkbox.setChecked(state)

    def selected_indices(self) -> list[int]:
        return [w.page_index for w in self.thumb_widgets if w.is_selected()]
