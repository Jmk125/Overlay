"""
Page selector dialog - pick which pages of a PDF to include.

For large PDFs (hundreds of sheets), callers can bypass this dialog by typing
a page range before opening it. If the user reaches this picker, they need
visual previews, so thumbnails start loading immediately. Large documents use
multiple worker processes so preview rendering can use available CPU cores.
"""
import fitz
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QScrollArea, QWidget, QGridLayout, QCheckBox, QFrame, QLineEdit,
    QMessageBox, QRubberBand, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QEvent, QPoint, QRect
from PyQt6.QtGui import QFont
from core import renderer as R


def parse_page_ranges(text: str, max_pages: int) -> list:
    """Parse '1-20, 45, 60-70' (1-based) into a sorted list of 0-based indices."""
    result = set()
    for part in text.replace(' ', '').split(','):
        if not part:
            continue
        if '-' in part:
            a, _, b = part.partition('-')
            try:
                a, b = int(a), int(b)
            except ValueError:
                continue
            if a > b:
                a, b = b, a
            for p in range(a, b + 1):
                if 1 <= p <= max_pages:
                    result.add(p - 1)
        else:
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= max_pages:
                result.add(p - 1)
    return sorted(result)


def _render_thumbnail_page(args):
    """Render one thumbnail in a worker process.

    Each process opens the PDF for its own page because PyMuPDF document/page
    objects are not safe to share across processes. Returning a PIL image keeps
    all QPixmap creation on the GUI thread.
    """
    pdf_path, page_index, max_size = args
    doc = fitz.open(pdf_path)
    try:
        return page_index, R.render_thumbnail_doc(doc, page_index, max_size=max_size)
    finally:
        doc.close()


class ThumbnailLoader(QThread):
    thumbnail_ready = pyqtSignal(int, object)  # page_index, PIL image
    progress = pyqtSignal(int, int)            # rendered, total

    PARALLEL_THRESHOLD = 8

    def __init__(self, pdf_path: str, page_count: int):
        super().__init__()
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def _emit_thumbnail(self, index: int, img, done: int):
        if not self.cancelled:
            self.thumbnail_ready.emit(index, img)
            self.progress.emit(done, self.page_count)

    def _run_single_process(self):
        doc = fitz.open(self.pdf_path)   # open once, render many
        try:
            for done, i in enumerate(range(self.page_count), start=1):
                if self.cancelled:
                    return
                try:
                    img = R.render_thumbnail_doc(doc, i, max_size=180)
                    self._emit_thumbnail(i, img, done)
                except Exception:
                    self.progress.emit(done, self.page_count)
        finally:
            doc.close()

    def _run_process_pool(self):
        workers = max(1, min(self.page_count, (os.cpu_count() or 2)))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [
                ex.submit(_render_thumbnail_page, (self.pdf_path, i, 180))
                for i in range(self.page_count)
            ]
            for done, fut in enumerate(as_completed(futs), start=1):
                if self.cancelled:
                    ex.shutdown(wait=False, cancel_futures=True)
                    return
                try:
                    index, img = fut.result()
                    self._emit_thumbnail(index, img, done)
                except Exception:
                    self.progress.emit(done, self.page_count)

    def run(self):
        if self.page_count <= 0:
            return
        try:
            if self.page_count >= self.PARALLEL_THRESHOLD:
                self._run_process_pool()
            else:
                self._run_single_process()
        except Exception as e:
            print(f"Parallel thumbnail rendering unavailable ({e}); using single process.")
            if not self.cancelled:
                try:
                    self._run_single_process()
                except Exception:
                    return


class PageThumbWidget(QWidget):
    def __init__(self, page_index: int, checked: bool = True, parent=None):
        super().__init__(parent)
        self.page_index = page_index
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.img_label = QLabel()
        self.img_label.setFixedSize(180, 180)
        self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_label.setStyleSheet("background: #2a2a2a; border: 1px solid #555; color:#777;")
        self.img_label.setText(f"Page {page_index + 1}")
        layout.addWidget(self.img_label)

        self.checkbox = QCheckBox(f"Page {page_index + 1}")
        self.checkbox.setChecked(checked)
        self.checkbox.setStyleSheet("""
            QCheckBox { color: #eee; font-weight: 600; padding: 2px; }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border: 2px solid #aaa;
                border-radius: 3px;
                background: #101010;
            }
            QCheckBox::indicator:hover { border-color: #fff; }
            QCheckBox::indicator:checked {
                background: #27a350;
                border-color: #6ee08a;
            }
        """)
        layout.addWidget(self.checkbox)

        self.setFixedWidth(200)
        self.setStyleSheet("background: #1a1a1a; border: 1px solid #444; border-radius: 4px;")

    def set_pixmap(self, pix):
        scaled = pix.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        self.img_label.setPixmap(scaled)

    def is_selected(self) -> bool:
        return self.checkbox.isChecked()


class PageSelectorDialog(QDialog):
    AUTO_THUMB_LIMIT = 40   # above this many pages, explain that previews load in parallel

    def __init__(self, pdf_path: str, page_count: int, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.page_count = page_count
        self.thumb_widgets: list[PageThumbWidget] = []
        self._thumbs_started = False
        self._large = page_count > self.AUTO_THUMB_LIMIT
        self._drag_container = None
        self._drag_origin = QPoint()
        self._drag_selecting = False
        self._rubber_band = None
        self.setWindowTitle(f"Select Pages — {page_count} pages")
        self.setMinimumSize(820, 620)
        self.setStyleSheet("background: #121212; color: #eee;")
        self._build_ui()
        self._start_thumbnails()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Page-range row (the fast path for big sets) ──
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Pages:"))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("e.g.  1-20, 45, 60-70   (leave blank to use checkboxes)")
        self.range_edit.setStyleSheet("background:#2a2a2a; color:#eee; border:1px solid #555; padding:4px;")
        self.range_edit.returnPressed.connect(self._apply_range)
        range_row.addWidget(self.range_edit, 1)
        apply_btn = QPushButton("Select these")
        apply_btn.clicked.connect(self._apply_range)
        apply_btn.setStyleSheet("background:#1a6b35; color:white; border:none; padding:5px 12px; border-radius:4px;")
        range_row.addWidget(apply_btn)
        layout.addLayout(range_row)

        # ── Toolbar ──
        toolbar = QHBoxLayout()
        select_all = QPushButton("Select All")
        select_all.clicked.connect(lambda: self._set_all(True))
        select_none = QPushButton("Select None")
        select_none.clicked.connect(lambda: self._set_all(False))
        for btn in [select_all, select_none]:
            btn.setStyleSheet("background: #2a2a5e; color: white; border: none; padding: 5px 12px; border-radius: 4px;")
        toolbar.addWidget(select_all)
        toolbar.addWidget(select_none)

        self.load_prev_btn = QPushButton("Loading previews…")
        self.load_prev_btn.setStyleSheet("background: #3a3a3a; color: white; border: none; padding: 5px 12px; border-radius: 4px;")
        self.load_prev_btn.clicked.connect(self._start_thumbnails)
        toolbar.addWidget(self.load_prev_btn)

        toolbar.addStretch()
        self.count_label = QLabel(self._count_text())
        self.count_label.setStyleSheet("color: #888;")
        toolbar.addWidget(self.count_label)
        layout.addLayout(toolbar)

        if self._large:
            hint = QLabel("⚡ Large document — previews are loading in parallel. "
                          "For the fastest path next time, type a page range before opening this picker.")
            hint.setStyleSheet("color:#FFD700; font-size:10px;")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        drag_hint = QLabel("Tip: drag a box across thumbnails to select multiple pages. Hold Ctrl while dragging to deselect.")
        drag_hint.setStyleSheet("color:#9aa7ff; font-size:10px;")
        drag_hint.setWordWrap(True)
        layout.addWidget(drag_hint)

        # ── Grid of pages ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        container = QWidget()
        self.grid = QGridLayout(container)
        self.grid.setSpacing(8)

        cols = 4
        default_checked = not self._large   # big sets start empty -> pick a range
        for i in range(self.page_count):
            thumb = PageThumbWidget(i, checked=default_checked)
            thumb.checkbox.stateChanged.connect(self._update_count)
            self.thumb_widgets.append(thumb)
            self.grid.addWidget(thumb, i // cols, i % cols)

        self._install_drag_selection(container)
        scroll.setWidget(container)
        layout.addWidget(scroll)

        # ── Buttons ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setStyleSheet("background: #5e2a2a; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        ok_btn = QPushButton("Use Selected Pages")
        ok_btn.clicked.connect(self._accept_if_any)
        ok_btn.setFixedHeight(36)
        ok_btn.setStyleSheet("background: #2a5e2a; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        cancel_btn.setFixedHeight(36)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _count_text(self) -> str:
        return f"{len(self.selected_indices())} of {self.page_count} selected"

    def _update_count(self):
        self.count_label.setText(self._count_text())

    def _install_drag_selection(self, container: QWidget):
        self._drag_container = container
        self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, container)
        container.installEventFilter(self)
        for thumb in self.thumb_widgets:
            thumb.installEventFilter(self)
            thumb.img_label.installEventFilter(self)
            thumb.checkbox.installEventFilter(self)

    def _event_pos_in_drag_container(self, obj, event) -> QPoint:
        pos = event.position().toPoint()
        if obj is self._drag_container:
            return pos
        return obj.mapTo(self._drag_container, pos)

    def eventFilter(self, obj, event):
        if self._drag_container is None or self._rubber_band is None:
            return super().eventFilter(obj, event)

        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = self._event_pos_in_drag_container(obj, event)
            self._drag_selecting = False
            self._rubber_band.hide()
            return False

        if event.type() == QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
            current = self._event_pos_in_drag_container(obj, event)
            if not self._drag_selecting:
                if (current - self._drag_origin).manhattanLength() < QApplication.startDragDistance():
                    return False
                self._drag_selecting = True
                self._rubber_band.show()
            self._rubber_band.setGeometry(QRect(self._drag_origin, current).normalized())
            return True

        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            if not self._drag_selecting:
                return False
            current = self._event_pos_in_drag_container(obj, event)
            rect = QRect(self._drag_origin, current).normalized()
            self._rubber_band.hide()
            self._drag_selecting = False
            self._apply_drag_selection(rect, event.modifiers())
            return True

        return super().eventFilter(obj, event)

    def _apply_drag_selection(self, rect: QRect, modifiers):
        if rect.width() < 4 or rect.height() < 4:
            return
        should_select = not (modifiers & Qt.KeyboardModifier.ControlModifier)
        for thumb in self.thumb_widgets:
            if rect.intersects(thumb.geometry()):
                thumb.checkbox.setChecked(should_select)
        self._update_count()

    def _apply_range(self):
        text = self.range_edit.text().strip()
        if not text:
            return
        indices = set(parse_page_ranges(text, self.page_count))
        if not indices:
            QMessageBox.information(self, "Page range",
                                    "Couldn't read any valid page numbers from that.")
            return
        for w in self.thumb_widgets:
            w.checkbox.setChecked(w.page_index in indices)
        self._update_count()

    def _start_thumbnails(self):
        if self._thumbs_started:
            return
        self._thumbs_started = True
        self.load_prev_btn.setEnabled(False)
        self.load_prev_btn.setText("Loading previews…")
        self.loader = ThumbnailLoader(self.pdf_path, self.page_count)
        self.loader.thumbnail_ready.connect(self._on_thumbnail)
        self.loader.progress.connect(self._on_thumbnail_progress)
        self.loader.finished.connect(lambda: self.load_prev_btn.setText("Previews loaded"))
        self.loader.start()

    def closeEvent(self, event):
        if hasattr(self, 'loader') and self.loader.isRunning():
            self.loader.cancel()
            self.loader.wait(500)
        super().closeEvent(event)

    def _on_thumbnail_progress(self, done: int, total: int):
        self.load_prev_btn.setText(f"Loading previews… {done}/{total}")

    def _on_thumbnail(self, index: int, pil_img):
        if index < len(self.thumb_widgets):
            # Convert to QPixmap on the GUI thread (thread-safe).
            self.thumb_widgets[index].set_pixmap(R.pil_to_qpixmap(pil_img))

    def _set_all(self, state: bool):
        for w in self.thumb_widgets:
            w.checkbox.setChecked(state)
        self._update_count()

    def _accept_if_any(self):
        if not self.selected_indices():
            QMessageBox.information(self, "No pages selected",
                                    "Select at least one page (type a range above "
                                    "or tick some boxes).")
            return
        self.accept()

    def selected_indices(self) -> list:
        return [w.page_index for w in self.thumb_widgets if w.is_selected()]
