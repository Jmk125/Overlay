"""
Sheet Matching Screen
- User draws OCR box on a sample page to define where sheet numbers live
- App OCRs all pages in both sets
- Auto-matches by sheet number
- Shows unmatched queue for manual pairing
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QSplitter, QFrame, QProgressBar,
    QScrollArea, QMessageBox, QLineEdit, QDialog
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, pyqtSlot, QRectF, QPointF, QTimer
from PyQt6.QtGui import QFont, QPixmap, QPainter, QPen, QColor, QBrush, QCursor
from PyQt6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem
from PyQt6.QtCore import QRectF
import os
from core.models import DrawingPage, OverlayPair, OverlaySet
from core import renderer as R


class OCRBoxView(QGraphicsView):
    """
    View where the user draws an OCR bounding box.
    - Left-drag      = draw the box
    - Scroll         = zoom (centered on the cursor)
    - Right/Middle-drag = pan
    Zooming lets the user enlarge a small title block so the box can be drawn
    accurately (a sloppy box is the usual cause of "unread" sheet numbers).
    """
    box_drawn = pyqtSignal(QRectF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setStyleSheet("background: #1a1a1a; border: 1px solid #555;")
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._pixmap_item = None
        self._rect_item = None
        self._start = None
        self._panning = False
        self._pan_start = QPointF()
        self._user_zoomed = False
        self._img_w = 1
        self._img_h = 1

    def load_page(self, pdf_path: str, page_index: int):
        # Render at a higher resolution than a plain thumbnail so the title
        # block stays crisp when zoomed in.
        pix = R.render_thumbnail(pdf_path, page_index, max_size=1600)
        self._img_w = pix.width()
        self._img_h = pix.height()
        self.scene.clear()
        self._pixmap_item = self.scene.addPixmap(pix)
        self.scene.setSceneRect(QRectF(pix.rect()))
        self._rect_item = None
        self._user_zoomed = False
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    # ── Zoom controls ─────────────────────────────────────────────
    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.scale(factor, factor)
        self._user_zoomed = True

    def zoom_in(self):
        self.scale(1.25, 1.25); self._user_zoomed = True

    def zoom_out(self):
        self.scale(1 / 1.25, 1 / 1.25); self._user_zoomed = True

    def fit(self):
        if self._pixmap_item:
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._user_zoomed = False

    # ── Mouse: left = draw box, right/middle = pan ────────────────
    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = self.mapToScene(event.position().toPoint())
            if self._rect_item:
                self.scene.removeItem(self._rect_item)
                self._rect_item = None

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y()))
            return
        if self._start:
            end = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._start, end).normalized()
            if self._rect_item:
                self._rect_item.setRect(rect)
            else:
                pen = QPen(QColor("#FFD700"), 2)
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setCosmetic(True)   # constant on-screen width regardless of zoom
                self._rect_item = self.scene.addRect(rect, pen,
                    QBrush(QColor(255, 215, 0, 40)))

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = False
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            return
        if event.button() == Qt.MouseButton.LeftButton and self._start:
            end = self.mapToScene(event.position().toPoint())
            rect = QRectF(self._start, end).normalized()
            if self._rect_item:
                self._rect_item.setRect(rect)
            self._start = None
            # Ignore an accidental click with no real area.
            if rect.width() < 2 or rect.height() < 2:
                return
            # Emit normalized rect (clamped to the page)
            norm = QRectF(
                max(0.0, rect.x() / self._img_w),
                max(0.0, rect.y() / self._img_h),
                min(1.0, rect.width() / self._img_w),
                min(1.0, rect.height() / self._img_h),
            )
            self.box_drawn.emit(norm)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Only auto-fit until the user takes manual zoom control.
        if self._pixmap_item and not self._user_zoomed:
            self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)


class OCRWorker(QThread):
    progress = pyqtSignal(int, int, str)   # current, total, label
    finished = pyqtSignal(list, list)       # pages_a_ocr'd, pages_b_ocr'd

    def __init__(self, pages_a, pages_b, norm_rect: QRectF):
        super().__init__()
        self.pages_a = pages_a
        self.pages_b = pages_b
        self.norm_rect = norm_rect
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        rect = (self.norm_rect.x(), self.norm_rect.y(),
                self.norm_rect.x() + self.norm_rect.width(),
                self.norm_rect.y() + self.norm_rect.height())

        def ocr_pages(pages, offset, total):
            for i, page in enumerate(pages):
                if self.cancelled:
                    return
                text = R.ocr_region(page.pdf_path, page.page_index, rect)
                # Clean up OCR text
                text = text.strip().replace('\n', ' ').replace('\r', '')
                page.sheet_number = text if text else f"(unread-{i})"
                self.progress.emit(offset + i + 1, total, f"OCR: {page.display_name} → {page.sheet_number}")

        total = len(self.pages_a) + len(self.pages_b)
        ocr_pages(self.pages_a, 0, total)
        ocr_pages(self.pages_b, len(self.pages_a), total)
        if not self.cancelled:
            self.finished.emit(self.pages_a, self.pages_b)


class ManualMatchDialog(QDialog):
    """Simple dialog to manually set a sheet number"""
    def __init__(self, page: DrawingPage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Sheet Number")
        self.setStyleSheet("background: #1e1e1e; color: #eee;")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Sheet number for: {page.display_name}"))
        self.edit = QLineEdit(page.sheet_number)
        self.edit.setStyleSheet("background: #2a2a2a; color: #eee; padding: 4px; border: 1px solid #555;")
        layout.addWidget(self.edit)
        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        for b in [ok, cancel]:
            b.setStyleSheet("background: #2a5e2a; color: white; border: none; padding: 5px 12px; border-radius: 4px;")
        btn_row.addStretch()
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)

    def value(self) -> str:
        return self.edit.text().strip()


class MatchingScreen(QWidget):
    matching_done = pyqtSignal(object)  # emits OverlaySet with pairs filled in

    def __init__(self, overlay_set: OverlaySet, parent=None):
        super().__init__(parent)
        self.overlay_set = overlay_set
        self.pages_a: list[DrawingPage] = overlay_set._pages_a
        self.pages_b: list[DrawingPage] = overlay_set._pages_b
        self.norm_rect = None
        self.matched_pairs: list[OverlayPair] = []
        self.unmatched_a: list[DrawingPage] = []
        self.unmatched_b: list[DrawingPage] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Sheet Matching")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setStyleSheet("color: #fff;")
        root.addWidget(title)

        desc = QLabel(
            "Draw a box around the sheet number area on the preview below. "
            "The app will OCR that region on every page to auto-match sheets by number."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; font-size: 11px;")
        root.addWidget(desc)

        # OCR box area + controls side by side
        top_split = QHBoxLayout()

        # Left: preview with box drawing
        left_col = QVBoxLayout()
        ocr_label = QLabel("Draw OCR box on sample page:")
        ocr_label.setStyleSheet("color: #ddd; font-weight: bold;")
        left_col.addWidget(ocr_label)
        self.ocr_view = OCRBoxView()
        self.ocr_view.setMinimumSize(400, 400)
        self.ocr_view.box_drawn.connect(self._on_box_drawn)
        left_col.addWidget(self.ocr_view)

        # Zoom controls + hint (zoom in to draw a tight box around the number)
        zoom_row = QHBoxLayout()
        zoom_in_btn = QPushButton("＋ Zoom In")
        zoom_in_btn.clicked.connect(self.ocr_view.zoom_in)
        zoom_out_btn = QPushButton("－ Zoom Out")
        zoom_out_btn.clicked.connect(self.ocr_view.zoom_out)
        fit_btn = QPushButton("⤢ Fit")
        fit_btn.clicked.connect(self.ocr_view.fit)
        for b in (zoom_in_btn, zoom_out_btn, fit_btn):
            b.setStyleSheet(self._btn_style("#2a4a6b", "#3a6491"))
            zoom_row.addWidget(b)
        zoom_row.addStretch()
        left_col.addLayout(zoom_row)

        zoom_hint = QLabel("Scroll = zoom · right-drag = pan · left-drag = draw box")
        zoom_hint.setStyleSheet("color: #777; font-size: 10px;")
        left_col.addWidget(zoom_hint)

        # Load first page from set A as sample
        self._sample_page = self.pages_a[0] if self.pages_a else None
        if self._sample_page:
            self.ocr_view.load_page(self._sample_page.pdf_path, self._sample_page.page_index)

        top_split.addLayout(left_col, 2)

        # Right: controls
        right_col = QVBoxLayout()
        right_col.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.box_status = QLabel("No box drawn yet")
        self.box_status.setStyleSheet("color: #FFD700; font-size: 11px;")
        right_col.addWidget(self.box_status)

        # ── Live preview of what OCR reads on the sample page ──
        preview_frame = QFrame()
        preview_frame.setStyleSheet("QFrame { background: #161616; border: 1px solid #333; border-radius: 5px; }")
        pf_layout = QVBoxLayout(preview_frame)
        pf_layout.setContentsMargins(8, 8, 8, 8)
        pf_layout.addWidget(QLabel("Sample read preview:", styleSheet="color:#bbb; font-weight:bold; border:none;"))
        self.preview_img = QLabel("Draw a box to test OCR")
        self.preview_img.setFixedHeight(70)
        self.preview_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_img.setStyleSheet("background:#0d0d0d; border:1px solid #333; color:#666;")
        pf_layout.addWidget(self.preview_img)
        self.preview_text = QLabel("")
        self.preview_text.setWordWrap(True)
        self.preview_text.setStyleSheet("color:#aaa; font-size:11px; border:none;")
        pf_layout.addWidget(self.preview_text)
        right_col.addWidget(preview_frame)

        self.run_ocr_btn = QPushButton("▶  Run OCR & Auto-Match")
        self.run_ocr_btn.setFixedHeight(38)
        self.run_ocr_btn.setEnabled(False)
        self.run_ocr_btn.setStyleSheet(self._btn_style("#1a6b35", "#27a350"))
        self.run_ocr_btn.clicked.connect(self._run_ocr)
        right_col.addWidget(self.run_ocr_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("QProgressBar { background: #2a2a2a; border: 1px solid #555; border-radius: 3px; } QProgressBar::chunk { background: #27a350; }")
        right_col.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: #888; font-size: 10px;")
        self.progress_label.setWordWrap(True)
        right_col.addWidget(self.progress_label)

        right_col.addSpacing(16)

        # Skip OCR option
        skip_btn = QPushButton("Skip OCR — Match Manually")
        skip_btn.setStyleSheet(self._btn_style("#3a3a3a", "#555"))
        skip_btn.clicked.connect(self._skip_to_manual)
        right_col.addWidget(skip_btn)

        top_split.addLayout(right_col, 1)
        root.addLayout(top_split)

        # Unmatched queue (hidden until needed)
        self.unmatched_frame = QFrame()
        self.unmatched_frame.setVisible(False)
        unmatched_layout = QVBoxLayout(self.unmatched_frame)

        unmatched_title = QLabel("⚠  Unmatched Sheets — Click to pair or dismiss")
        unmatched_title.setStyleSheet("color: #FFD700; font-weight: bold;")
        unmatched_layout.addWidget(unmatched_title)

        queue_row = QHBoxLayout()

        um_a_col = QVBoxLayout()
        um_a_col.addWidget(QLabel(f"Set A unmatched:"))
        self.unmatched_list_a = QListWidget()
        self.unmatched_list_a.setStyleSheet("background: #1e1e1e; color: #eee; border: 1px solid #555;")
        self.unmatched_list_a.setMaximumHeight(120)
        um_a_col.addWidget(self.unmatched_list_a)
        queue_row.addLayout(um_a_col)

        pair_col = QVBoxLayout()
        pair_col.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pair_btn = QPushButton("Pair Selected →")
        pair_btn.clicked.connect(self._manual_pair)
        pair_btn.setStyleSheet(self._btn_style("#2a4a6b", "#3a6491"))
        pair_col.addWidget(pair_btn)
        edit_a_btn = QPushButton("Edit A #")
        edit_a_btn.clicked.connect(lambda: self._edit_sheet_number('a'))
        edit_a_btn.setStyleSheet(self._btn_style("#3a3a3a", "#555"))
        pair_col.addWidget(edit_a_btn)
        edit_b_btn = QPushButton("Edit B #")
        edit_b_btn.clicked.connect(lambda: self._edit_sheet_number('b'))
        edit_b_btn.setStyleSheet(self._btn_style("#3a3a3a", "#555"))
        pair_col.addWidget(edit_b_btn)
        queue_row.addLayout(pair_col)

        um_b_col = QVBoxLayout()
        um_b_col.addWidget(QLabel(f"Set B unmatched:"))
        self.unmatched_list_b = QListWidget()
        self.unmatched_list_b.setStyleSheet("background: #1e1e1e; color: #eee; border: 1px solid #555;")
        self.unmatched_list_b.setMaximumHeight(120)
        um_b_col.addWidget(self.unmatched_list_b)
        queue_row.addLayout(um_b_col)

        unmatched_layout.addLayout(queue_row)
        root.addWidget(self.unmatched_frame)

        # Matched pairs summary
        self.matched_label = QLabel("Matched pairs: 0")
        self.matched_label.setStyleSheet("color: #27a350; font-weight: bold;")
        root.addWidget(self.matched_label)

        self.matched_list = QListWidget()
        self.matched_list.setStyleSheet("background: #1e1e1e; color: #eee; border: 1px solid #555;")
        self.matched_list.setMaximumHeight(140)
        root.addWidget(self.matched_list)

        # Proceed button
        self.proceed_btn = QPushButton("Continue to Overlay Viewer  ▶")
        self.proceed_btn.setFixedHeight(42)
        self.proceed_btn.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.proceed_btn.setEnabled(False)
        self.proceed_btn.setStyleSheet(self._btn_style("#1a6b35", "#27a350"))
        self.proceed_btn.clicked.connect(self._proceed)
        root.addWidget(self.proceed_btn)

    def _on_box_drawn(self, rect: QRectF):
        self.norm_rect = rect
        self.box_status.setText(
            f"Box set: ({rect.x():.2f}, {rect.y():.2f}) "
            f"— {rect.width():.2f}×{rect.height():.2f} (normalized)"
        )
        self.run_ocr_btn.setEnabled(True)
        # Show what OCR reads on the sample page before committing to a full run.
        self.preview_text.setText("Reading…")
        self.preview_text.setStyleSheet("color:#FFD700; font-size:11px; border:none;")
        QTimer.singleShot(30, self._update_preview)

    def _update_preview(self):
        """Render the boxed crop and OCR it on the sample page so the user can
        confirm the number is being read before running the whole batch."""
        if not self._sample_page or not self.norm_rect:
            return
        rect = (self.norm_rect.x(), self.norm_rect.y(),
                self.norm_rect.x() + self.norm_rect.width(),
                self.norm_rect.y() + self.norm_rect.height())
        page = self._sample_page

        # Show the cropped region so the user sees exactly what's being OCR'd.
        try:
            pix = R.render_region_pixmap(page.pdf_path, page.page_index, rect)
            scaled = pix.scaledToHeight(64, Qt.TransformationMode.SmoothTransformation)
            self.preview_img.setPixmap(scaled)
        except Exception:
            self.preview_img.setText("(could not render crop)")

        if not R.tesseract_available():
            self.preview_text.setText(
                "⚠ Tesseract OCR not found. Install it (and ensure it's on your "
                "PATH), or use “Skip OCR — Match Manually”.")
            self.preview_text.setStyleSheet("color:#ff8c69; font-size:11px; border:none;")
            return

        text = R.ocr_region(page.pdf_path, page.page_index, rect)
        if text:
            self.preview_text.setText(f"Read:  “{text}”   — looks good? Run the full match.")
            self.preview_text.setStyleSheet("color:#27a350; font-size:12px; font-weight:bold; border:none;")
        else:
            self.preview_text.setText(
                "No text detected. Zoom in and draw a tighter box around just "
                "the sheet number, then try again.")
            self.preview_text.setStyleSheet("color:#FFD700; font-size:11px; border:none;")

    def _run_ocr(self):
        self.run_ocr_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        total = len(self.pages_a) + len(self.pages_b)
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(0)

        self.ocr_worker = OCRWorker(self.pages_a, self.pages_b, self.norm_rect)
        self.ocr_worker.progress.connect(self._on_ocr_progress)
        self.ocr_worker.finished.connect(self._on_ocr_done)
        self.ocr_worker.start()

    def _on_ocr_progress(self, current: int, total: int, label: str):
        self.progress_bar.setValue(current)
        self.progress_label.setText(label)

    def _on_ocr_done(self, pages_a, pages_b):
        self.progress_bar.setVisible(False)
        self.progress_label.setText("OCR complete")
        self.pages_a = pages_a
        self.pages_b = pages_b
        self._do_matching()

    def _skip_to_manual(self):
        """Skip OCR, treat all as unmatched for manual pairing"""
        self.unmatched_a = list(self.pages_a)
        self.unmatched_b = list(self.pages_b)
        self._refresh_unmatched_lists()
        self.unmatched_frame.setVisible(True)
        if not self.matched_pairs:
            self.proceed_btn.setEnabled(False)

    def _do_matching(self):
        """Match pages by sheet number"""
        lookup_b = {p.sheet_number: p for p in self.pages_b if p.sheet_number}
        self.matched_pairs = []
        self.unmatched_a = []
        matched_b_keys = set()

        for pa in self.pages_a:
            if pa.sheet_number in lookup_b:
                pb = lookup_b[pa.sheet_number]
                self.matched_pairs.append(OverlayPair(page_a=pa, page_b=pb))
                matched_b_keys.add(pa.sheet_number)
            else:
                self.unmatched_a.append(pa)

        self.unmatched_b = [p for p in self.pages_b if p.sheet_number not in matched_b_keys]

        self._refresh_matched_list()
        self._refresh_unmatched_lists()

        if self.unmatched_a or self.unmatched_b:
            self.unmatched_frame.setVisible(True)

        self.proceed_btn.setEnabled(len(self.matched_pairs) > 0)

    def _refresh_matched_list(self):
        self.matched_list.clear()
        for pair in self.matched_pairs:
            self.matched_list.addItem(
                f"  {pair.page_a.sheet_number}  ↔  {pair.page_b.sheet_number}"
            )
        self.matched_label.setText(f"Matched pairs: {len(self.matched_pairs)}")

    def _refresh_unmatched_lists(self):
        self.unmatched_list_a.clear()
        for p in self.unmatched_a:
            label = p.sheet_number if p.sheet_number else p.display_name
            self.unmatched_list_a.addItem(label)

        self.unmatched_list_b.clear()
        for p in self.unmatched_b:
            label = p.sheet_number if p.sheet_number else p.display_name
            self.unmatched_list_b.addItem(label)

    def _manual_pair(self):
        ia = self.unmatched_list_a.currentRow()
        ib = self.unmatched_list_b.currentRow()
        if ia < 0 or ib < 0:
            QMessageBox.information(self, "Select Sheets", "Select one sheet from each list.")
            return
        pa = self.unmatched_a.pop(ia)
        pb = self.unmatched_b.pop(ib)
        self.matched_pairs.append(OverlayPair(page_a=pa, page_b=pb))
        self._refresh_matched_list()
        self._refresh_unmatched_lists()
        self.proceed_btn.setEnabled(True)

    def _edit_sheet_number(self, side: str):
        lst = self.unmatched_list_a if side == 'a' else self.unmatched_list_b
        lst_data = self.unmatched_a if side == 'a' else self.unmatched_b
        idx = lst.currentRow()
        if idx < 0:
            return
        page = lst_data[idx]
        dlg = ManualMatchDialog(page, self)
        if dlg.exec():
            page.sheet_number = dlg.value()
            self._refresh_unmatched_lists()

    def _proceed(self):
        self.overlay_set.pairs = self.matched_pairs
        self.overlay_set.unmatched_a = self.unmatched_a
        self.overlay_set.unmatched_b = self.unmatched_b
        self.matching_done.emit(self.overlay_set)

    def _btn_style(self, bg, hover):
        return f"""
            QPushButton {{ background: {bg}; color: white; border: none; border-radius: 5px; padding: 6px 14px; }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:disabled {{ background: #333; color: #666; }}
        """
