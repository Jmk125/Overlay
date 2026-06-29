"""
Overlay Viewer - main workspace
Pan/zoom, layer toggling, alignment, rotation, export
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QSlider, QSplitter, QScrollArea, QFrame,
    QListWidget, QListWidgetItem, QDoubleSpinBox, QSpinBox,
    QFileDialog, QCheckBox, QGroupBox, QMessageBox, QSizePolicy,
    QLineEdit
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QThread, QPointF, QRectF, QSizeF, QTimer
)
from PyQt6.QtGui import (
    QFont, QPixmap, QWheelEvent, QMouseEvent, QPainter,
    QColor, QPen, QBrush, QKeySequence, QShortcut, QCursor
)
from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsEllipseItem, QApplication
)
import math
import os
from PIL import Image
from core.models import OverlayPair, OverlaySet, COMMON_SCALES, compute_scale_factor
from core import renderer as R


class RenderWorker(QThread):
    done = pyqtSignal(object, object, object)  # pix_a, pix_b, pix_composite

    def __init__(self, pair: OverlayPair, overlay_set: OverlaySet):
        super().__init__()
        self.pair = pair
        self.overlay_set = overlay_set
        self.cancelled = False
        self.setTerminationEnabled(True)

    def cancel(self):
        self.cancelled = True

    def run(self):
        dpi = self.overlay_set.render_dpi
        try:
            if self.cancelled:
                return
            img_a = R.render_page(self.pair.page_a.pdf_path, self.pair.page_a.page_index, dpi)
            if self.cancelled:
                return
            img_b_raw = R.render_page(self.pair.page_b.pdf_path, self.pair.page_b.page_index, dpi)
            if self.cancelled:
                return

            # Canvas size = size of img_a (anchor)
            canvas_size = img_a.size

            # Apply transforms to B
            img_b = R.apply_transform(
                img_b_raw,
                self.pair.offset_x, self.pair.offset_y,
                self.pair.rotation,
                self.pair.pivot_x, self.pair.pivot_y,
                self.pair.scale_factor,
                canvas_size
            )

            if self.cancelled:
                return
            # Composite
            composite = R.composite_overlay(img_a, img_b,
                                             self.overlay_set.color_a,
                                             self.overlay_set.color_b,
                                             shared_color=self.overlay_set.shared_color)
            if self.cancelled:
                return
            solo_a = R.render_single_colored(img_a, self.overlay_set.color_a)
            solo_b = R.render_single_colored(img_b, self.overlay_set.color_b)

            if self.cancelled:
                return
            pix_composite = R.pil_to_qpixmap(composite)
            pix_a = R.pil_to_qpixmap(solo_a)
            pix_b = R.pil_to_qpixmap(solo_b)
            self.done.emit(pix_a, pix_b, pix_composite)
        except Exception as e:
            if not self.cancelled:
                print(f"Render error: {e}")
                self.done.emit(None, None, None)


class OverlayCanvas(QGraphicsView):
    """
    The main canvas.
    - Right-click drag = pan
    - Ctrl+scroll = zoom
    - Left click drag (when in drag mode) = move drawing B
    - Rotation handle drag = rotate drawing B
    """
    pair_changed = pyqtSignal()

    MODE_VIEW = 0
    MODE_MOVE = 1
    MODE_ROTATE = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.gscene = QGraphicsScene(self)
        self.setScene(self.gscene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._bg_white = True
        self._apply_bg()

        self._mode = self.MODE_VIEW
        self._panning = False
        self._pan_start = QPointF()
        self._drag_start = QPointF()
        self._pair: OverlayPair = None

        # Pixmap items
        self._item_a = None
        self._item_b = None
        self._item_composite = None

        # Current view: 'composite', 'a', 'b'
        self._view_mode = 'composite'

        # Pivot dot for rotation
        self._pivot_item = None

        # Cached raw images for re-compositing on move (skipped; we re-render)
        self._pix_a = None
        self._pix_b = None
        self._pix_composite = None

    def _apply_bg(self):
        color = "#ffffff" if self._bg_white else "#0d0d0d"
        self.setStyleSheet(f"background: {color}; border: none;")
        self.gscene.setBackgroundBrush(QBrush(QColor(color)))

    def set_background(self, white: bool):
        self._bg_white = white
        self._apply_bg()

    def set_mode(self, mode: int):
        self._mode = mode
        if mode == self.MODE_VIEW:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        elif mode == self.MODE_MOVE:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        elif mode == self.MODE_ROTATE:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def load_pixmaps(self, pix_a, pix_b, pix_composite, pair: OverlayPair):
        self._pix_a = pix_a
        self._pix_b = pix_b
        self._pix_composite = pix_composite
        self._pair = pair
        self.gscene.clear()
        self._item_a = self.gscene.addPixmap(pix_a if pix_a else QPixmap())
        self._item_b = self.gscene.addPixmap(pix_b if pix_b else QPixmap())
        self._item_composite = self.gscene.addPixmap(pix_composite if pix_composite else QPixmap())
        self._update_visibility()
        self.fitInView(self.gscene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def set_view_mode(self, mode: str):
        """mode: 'composite', 'a', or 'b'"""
        self._view_mode = mode
        self._update_visibility()

    def _update_visibility(self):
        if not self._item_composite:
            return
        self._item_composite.setVisible(self._view_mode == 'composite')
        if self._item_a:
            self._item_a.setVisible(self._view_mode == 'a')
        if self._item_b:
            self._item_b.setVisible(self._view_mode == 'b')

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.RightButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode in (self.MODE_MOVE, self.MODE_ROTATE):
                self._drag_start = self.mapToScene(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y()))
            return

        if event.buttons() & Qt.MouseButton.LeftButton and self._pair:
            fine = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            factor = 0.1 if fine else 1.0

            scene_pos = self.mapToScene(event.position().toPoint())
            delta = scene_pos - self._drag_start
            self._drag_start = scene_pos

            if self._mode == self.MODE_MOVE:
                self._pair.offset_x += delta.x() * factor
                self._pair.offset_y += delta.y() * factor
                self.pair_changed.emit()

            elif self._mode == self.MODE_ROTATE:
                # Compute angle change based on mouse movement around pivot
                if self._pix_composite:
                    pw = self._pix_composite.width()
                    ph = self._pix_composite.height()
                else:
                    pw, ph = 1000, 1000
                pivot_scene = QPointF(
                    self._pair.pivot_x * pw,
                    self._pair.pivot_y * ph
                )
                prev = self._drag_start - delta - pivot_scene
                curr = scene_pos - pivot_scene
                angle_prev = math.atan2(prev.y(), prev.x())
                angle_curr = math.atan2(curr.y(), curr.x())
                delta_angle = math.degrees(angle_curr - angle_prev)
                if fine:
                    delta_angle *= 0.1
                self._pair.rotation = (self._pair.rotation + delta_angle) % 360
                self.pair_changed.emit()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.RightButton:
            self._panning = False
            self.set_mode(self._mode)
        super().mouseReleaseEvent(event)

    def fit_view(self):
        self.fitInView(self.gscene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)


class OverlayViewer(QWidget):
    back_to_matching = pyqtSignal()
    save_project = pyqtSignal(object)  # emits OverlaySet

    def __init__(self, overlay_set: OverlaySet, settings: dict, parent=None):
        super().__init__(parent)
        self.overlay_set = overlay_set
        self.settings = settings
        self.current_pair_index = 0
        self._render_worker = None
        self._worker_pool = []   # keeps workers alive until they finish naturally
        self._dirty = False

        self._build_ui()

        # Sync background state from overlay_set (matters when loading a saved project)
        white = (overlay_set.canvas_bg != 'dark')
        self.canvas.set_background(white)
        self.bg_white_btn.setChecked(white)
        self.bg_dark_btn.setChecked(not white)

        self._load_pair(0)

        # Debounce re-render on transform changes
        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(400)
        self._render_timer.timeout.connect(self._do_render)

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left: pair list
        left_panel = QWidget()
        left_panel.setFixedWidth(200)
        left_panel.setStyleSheet("background: #161616;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)

        left_layout.addWidget(QLabel("Overlay Pairs"))
        self.pair_list = QListWidget()
        self.pair_list.setStyleSheet("background: #1e1e1e; color: #ddd; border: 1px solid #444;")
        for pair in self.overlay_set.pairs:
            label = pair.page_a.sheet_number or pair.pair_id
            self.pair_list.addItem(label)
        self.pair_list.currentRowChanged.connect(self._load_pair)
        left_layout.addWidget(self.pair_list)

        back_btn = QPushButton("← Back to Matching")
        back_btn.setStyleSheet("background: #333; color: #aaa; border: none; padding: 5px;")
        back_btn.clicked.connect(self.back_to_matching)
        left_layout.addWidget(back_btn)

        root.addWidget(left_panel)

        # ── Center: canvas
        self.canvas = OverlayCanvas()
        self.canvas.pair_changed.connect(self._on_pair_changed)
        root.addWidget(self.canvas, 1)

        # ── Right: tools panel
        right_panel = QWidget()
        right_panel.setFixedWidth(260)
        right_panel.setStyleSheet("background: #161616;")
        right_scroll = QScrollArea()
        right_scroll.setWidget(right_panel)
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet("border: none;")
        right_scroll.setFixedWidth(272)
        root.addWidget(right_scroll)

        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(8)

        # View toggle
        view_group = QGroupBox("View")
        view_group.setStyleSheet(self._group_style())
        vg_layout = QVBoxLayout(view_group)
        self.view_btns = {}
        for key, label in [('composite', 'Overlay (Both)'),
                            ('a', f'Set A only'),
                            ('b', f'Set B only')]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(self._toggle_btn_style())
            btn.clicked.connect(lambda checked, k=key: self._set_view(k))
            self.view_btns[key] = btn
            vg_layout.addWidget(btn)
        self.view_btns['composite'].setChecked(True)

        # Background toggle
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333;")
        vg_layout.addWidget(sep)
        bg_row = QHBoxLayout()
        bg_row.addWidget(QLabel("Background:"))
        self.bg_white_btn = QPushButton("⬜ White")
        self.bg_white_btn.setCheckable(True)
        self.bg_white_btn.setChecked(True)
        self.bg_white_btn.setFixedHeight(26)
        self.bg_dark_btn = QPushButton("⬛ Dark")
        self.bg_dark_btn.setCheckable(True)
        self.bg_dark_btn.setChecked(False)
        for btn in [self.bg_white_btn, self.bg_dark_btn]:
            btn.setStyleSheet(self._toggle_btn_style())
        self.bg_white_btn.clicked.connect(lambda: self._set_bg('white'))
        self.bg_dark_btn.clicked.connect(lambda: self._set_bg('dark'))
        bg_row.addWidget(self.bg_white_btn)
        bg_row.addWidget(self.bg_dark_btn)
        vg_layout.addLayout(bg_row)

        right_layout.addWidget(view_group)

        # Alignment mode
        align_group = QGroupBox("Align Drawing B")
        align_group.setStyleSheet(self._group_style())
        ag_layout = QVBoxLayout(align_group)
        self.move_btn = QPushButton("↕  Move (click & drag)")
        self.move_btn.setCheckable(True)
        self.move_btn.setStyleSheet(self._toggle_btn_style())
        self.move_btn.clicked.connect(lambda: self._set_align_mode('move'))
        ag_layout.addWidget(self.move_btn)

        # Offset nudge
        nudge_row = QHBoxLayout()
        for label, dx, dy in [('←', -1, 0), ('→', 1, 0), ('↑', 0, -1), ('↓', 0, 1)]:
            btn = QPushButton(label)
            btn.setFixedSize(32, 28)
            btn.setStyleSheet("background: #2a2a5e; color: white; border: none; border-radius: 3px;")
            btn.clicked.connect(lambda _, x=dx, y=dy: self._nudge(x, y))
            nudge_row.addWidget(btn)
        ag_layout.addLayout(nudge_row)
        ag_layout.addWidget(QLabel("Shift+drag = fine movement", styleSheet="color: #666; font-size: 9px;"))

        right_layout.addWidget(align_group)

        # Rotation
        rot_group = QGroupBox("Rotation (Drawing B)")
        rot_group.setStyleSheet(self._group_style())
        rg_layout = QVBoxLayout(rot_group)

        # Quick rotation buttons
        quick_row = QHBoxLayout()
        for label, angle in [('90°', 90), ('180°', 180), ('270°', 270), ('45°', 45), ('-45°', -45)]:
            btn = QPushButton(label)
            btn.setFixedHeight(28)
            btn.setStyleSheet("background: #2a4a6b; color: white; border: none; border-radius: 3px; font-size: 10px;")
            btn.clicked.connect(lambda _, a=angle: self._rotate_quick(a))
            quick_row.addWidget(btn)
        rg_layout.addLayout(quick_row)

        # Manual rotation
        self.rotate_btn = QPushButton("↻  Free Rotate (click & drag)")
        self.rotate_btn.setCheckable(True)
        self.rotate_btn.setStyleSheet(self._toggle_btn_style())
        self.rotate_btn.clicked.connect(lambda: self._set_align_mode('rotate'))
        rg_layout.addWidget(self.rotate_btn)

        rot_val_row = QHBoxLayout()
        rot_val_row.addWidget(QLabel("Angle:"))
        self.rot_spin = QDoubleSpinBox()
        self.rot_spin.setRange(-360, 360)
        self.rot_spin.setDecimals(1)
        self.rot_spin.setSuffix("°")
        self.rot_spin.setStyleSheet("background: #2a2a2a; color: #eee; border: 1px solid #555;")
        self.rot_spin.valueChanged.connect(self._on_rot_spin)
        rot_val_row.addWidget(self.rot_spin)
        rg_layout.addLayout(rot_val_row)

        rg_layout.addWidget(QLabel("Pivot (normalized 0-1):"))
        pivot_row = QHBoxLayout()
        pivot_row.addWidget(QLabel("X:"))
        self.pivot_x_spin = QDoubleSpinBox()
        self.pivot_x_spin.setRange(0, 1)
        self.pivot_x_spin.setSingleStep(0.05)
        self.pivot_x_spin.setValue(0.5)
        self.pivot_x_spin.setDecimals(2)
        self.pivot_x_spin.setStyleSheet("background: #2a2a2a; color: #eee; border: 1px solid #555;")
        self.pivot_x_spin.valueChanged.connect(self._on_pivot_changed)
        pivot_row.addWidget(self.pivot_x_spin)
        pivot_row.addWidget(QLabel("Y:"))
        self.pivot_y_spin = QDoubleSpinBox()
        self.pivot_y_spin.setRange(0, 1)
        self.pivot_y_spin.setSingleStep(0.05)
        self.pivot_y_spin.setValue(0.5)
        self.pivot_y_spin.setDecimals(2)
        self.pivot_y_spin.setStyleSheet("background: #2a2a2a; color: #eee; border: 1px solid #555;")
        self.pivot_y_spin.valueChanged.connect(self._on_pivot_changed)
        pivot_row.addWidget(self.pivot_y_spin)
        rg_layout.addLayout(pivot_row)

        right_layout.addWidget(rot_group)

        # Scale
        scale_group = QGroupBox("Scale")
        scale_group.setStyleSheet(self._group_style())
        sg_layout = QVBoxLayout(scale_group)

        sg_layout.addWidget(QLabel("Set A scale:"))
        self.scale_a_combo = QComboBox()
        self.scale_a_combo.addItems(COMMON_SCALES)
        self.scale_a_combo.setEditable(True)
        self.scale_a_combo.setStyleSheet("background: #2a2a2a; color: #eee;")
        sg_layout.addWidget(self.scale_a_combo)

        sg_layout.addWidget(QLabel("Set B scale:"))
        self.scale_b_combo = QComboBox()
        self.scale_b_combo.addItems(COMMON_SCALES)
        self.scale_b_combo.setEditable(True)
        self.scale_b_combo.setStyleSheet("background: #2a2a2a; color: #eee;")
        sg_layout.addWidget(self.scale_b_combo)

        apply_scale_btn = QPushButton("Apply Scale")
        apply_scale_btn.setStyleSheet("background: #2a4a6b; color: white; border: none; padding: 5px; border-radius: 4px;")
        apply_scale_btn.clicked.connect(self._apply_scale)
        sg_layout.addWidget(apply_scale_btn)

        self.scale_status = QLabel("")
        self.scale_status.setStyleSheet("color: #888; font-size: 10px;")
        self.scale_status.setWordWrap(True)
        sg_layout.addWidget(self.scale_status)

        right_layout.addWidget(scale_group)

        # Reset
        reset_btn = QPushButton("Reset All Transforms")
        reset_btn.setStyleSheet("background: #5e2a2a; color: white; border: none; padding: 5px; border-radius: 4px;")
        reset_btn.clicked.connect(self._reset_transforms)
        right_layout.addWidget(reset_btn)

        fit_btn = QPushButton("Fit to Window")
        fit_btn.setStyleSheet("background: #333; color: white; border: none; padding: 5px; border-radius: 4px;")
        fit_btn.clicked.connect(self.canvas.fit_view)
        right_layout.addWidget(fit_btn)

        # Export / Save
        export_group = QGroupBox("Export / Save")
        export_group.setStyleSheet(self._group_style())
        eg_layout = QVBoxLayout(export_group)

        save_btn = QPushButton("💾  Save Project")
        save_btn.setStyleSheet("background: #1a6b35; color: white; border: none; padding: 6px; border-radius: 4px;")
        save_btn.clicked.connect(lambda: self.save_project.emit(self.overlay_set))
        eg_layout.addWidget(save_btn)

        export_png_btn = QPushButton("Export PNG")
        export_png_btn.setStyleSheet("background: #2a4a6b; color: white; border: none; padding: 5px; border-radius: 4px;")
        export_png_btn.clicked.connect(lambda: self._export('png'))
        eg_layout.addWidget(export_png_btn)

        export_pdf_btn = QPushButton("Export PDF")
        export_pdf_btn.setStyleSheet("background: #2a4a6b; color: white; border: none; padding: 5px; border-radius: 4px;")
        export_pdf_btn.clicked.connect(lambda: self._export('pdf'))
        eg_layout.addWidget(export_pdf_btn)

        right_layout.addWidget(export_group)
        right_layout.addStretch()

        # Rendering status
        self.render_status = QLabel("Ready")
        self.render_status.setStyleSheet("color: #666; font-size: 10px; padding: 4px;")
        right_layout.addWidget(self.render_status)

        # Keyboard shortcuts
        QShortcut(QKeySequence("1"), self, lambda: self._set_view('composite'))
        QShortcut(QKeySequence("2"), self, lambda: self._set_view('a'))
        QShortcut(QKeySequence("3"), self, lambda: self._set_view('b'))
        QShortcut(QKeySequence("F"), self, self.canvas.fit_view)

    def _load_pair(self, index: int):
        if index < 0 or index >= len(self.overlay_set.pairs):
            return
        self.current_pair_index = index
        self.pair_list.setCurrentRow(index)
        pair = self.overlay_set.pairs[index]

        # Update controls from pair state
        self._update_controls_from_pair(pair)
        self._do_render()

    def _update_controls_from_pair(self, pair: OverlayPair):
        self.rot_spin.blockSignals(True)
        self.rot_spin.setValue(pair.rotation)
        self.rot_spin.blockSignals(False)

        self.pivot_x_spin.blockSignals(True)
        self.pivot_x_spin.setValue(pair.pivot_x)
        self.pivot_x_spin.blockSignals(False)

        self.pivot_y_spin.blockSignals(True)
        self.pivot_y_spin.setValue(pair.pivot_y)
        self.pivot_y_spin.blockSignals(False)

        if pair.scale_a:
            idx = self.scale_a_combo.findText(pair.scale_a)
            if idx >= 0:
                self.scale_a_combo.setCurrentIndex(idx)
            else:
                self.scale_a_combo.setCurrentText(pair.scale_a)

        if pair.scale_b:
            idx = self.scale_b_combo.findText(pair.scale_b)
            if idx >= 0:
                self.scale_b_combo.setCurrentIndex(idx)
            else:
                self.scale_b_combo.setCurrentText(pair.scale_b)

    def _current_pair(self) -> OverlayPair:
        return self.overlay_set.pairs[self.current_pair_index]

    def _do_render(self):
        if not self.overlay_set.pairs:
            return
        pair = self._current_pair()
        self.render_status.setText("Rendering...")

        # Cancel any active worker (it checks self.cancelled so it will stop soon)
        if self._render_worker and self._render_worker.isRunning():
            self._render_worker.cancel()
            # Don't wait — let it finish on its own; _worker_pool keeps it alive

        # Purge finished workers from the pool
        self._worker_pool = [w for w in self._worker_pool if w.isRunning()]

        worker = RenderWorker(pair, self.overlay_set)
        worker.done.connect(self._on_render_done)
        # When finished, remove from pool
        worker.finished.connect(lambda w=worker: self._worker_pool.remove(w) if w in self._worker_pool else None)
        self._render_worker = worker
        self._worker_pool.append(worker)
        worker.start()

    def _on_render_done(self, pix_a, pix_b, pix_composite):
        # Ignore results from a worker that was cancelled
        sender = self.sender()
        if sender and getattr(sender, 'cancelled', False):
            return
        if pix_composite is None:
            self.render_status.setText("Render failed — check console")
            return
        pair = self._current_pair()
        self.canvas.load_pixmaps(pix_a, pix_b, pix_composite, pair)
        self._set_view(list(self.view_btns.keys())[
            next(i for i, (k, btn) in enumerate(self.view_btns.items()) if btn.isChecked())
        ])
        self.render_status.setText("Ready  (1=overlay  2=A only  3=B only)")

    def _set_bg(self, mode: str):
        """Switch canvas background between white and dark, flipping shared-line color accordingly."""
        self.overlay_set.canvas_bg = mode
        white = (mode == 'white')
        self.overlay_set.shared_color = '#000000' if white else '#ffffff'
        self.canvas.set_background(white)
        self.bg_white_btn.setChecked(white)
        self.bg_dark_btn.setChecked(not white)
        # Re-render so shared line color updates
        self._do_render()

    def _set_view(self, mode: str):
        for k, btn in self.view_btns.items():
            btn.setChecked(k == mode)
        self.canvas.set_view_mode(mode)

    def _set_align_mode(self, mode: str):
        if mode == 'move':
            self.canvas.set_mode(OverlayCanvas.MODE_MOVE)
            self.move_btn.setChecked(True)
            self.rotate_btn.setChecked(False)
        elif mode == 'rotate':
            self.canvas.set_mode(OverlayCanvas.MODE_ROTATE)
            self.rotate_btn.setChecked(True)
            self.move_btn.setChecked(False)
        else:
            self.canvas.set_mode(OverlayCanvas.MODE_VIEW)
            self.move_btn.setChecked(False)
            self.rotate_btn.setChecked(False)

    def _nudge(self, dx: int, dy: int):
        pair = self._current_pair()
        pair.offset_x += dx * 5
        pair.offset_y += dy * 5
        self._on_pair_changed()

    def _rotate_quick(self, angle: float):
        pair = self._current_pair()
        pair.rotation = (pair.rotation + angle) % 360
        self.rot_spin.blockSignals(True)
        self.rot_spin.setValue(pair.rotation)
        self.rot_spin.blockSignals(False)
        self._on_pair_changed()

    def _on_rot_spin(self, value: float):
        pair = self._current_pair()
        pair.rotation = value
        self._on_pair_changed()

    def _on_pivot_changed(self):
        pair = self._current_pair()
        pair.pivot_x = self.pivot_x_spin.value()
        pair.pivot_y = self.pivot_y_spin.value()
        self._on_pair_changed()

    def _apply_scale(self):
        pair = self._current_pair()
        scale_a = self.scale_a_combo.currentText()
        scale_b = self.scale_b_combo.currentText()
        factor = compute_scale_factor(scale_a, scale_b)
        pair.scale_a = scale_a
        pair.scale_b = scale_b
        pair.scale_factor = factor
        if factor != 1.0:
            self.scale_status.setText(f"Scale factor: {factor:.3f}x applied to B")
        else:
            self.scale_status.setText("Scales equal or unparseable — no change")
        self._on_pair_changed()

    def _reset_transforms(self):
        pair = self._current_pair()
        pair.offset_x = 0
        pair.offset_y = 0
        pair.rotation = 0
        pair.pivot_x = 0.5
        pair.pivot_y = 0.5
        pair.scale_factor = 1.0
        self._update_controls_from_pair(pair)
        self._on_pair_changed()

    def _on_pair_changed(self):
        # Update rot spin if changed via canvas drag
        pair = self._current_pair()
        self.rot_spin.blockSignals(True)
        self.rot_spin.setValue(pair.rotation)
        self.rot_spin.blockSignals(False)
        self._render_timer.start()

    def _export(self, fmt: str):
        pair = self._current_pair()
        default_name = f"overlay_{pair.page_a.sheet_number or 'sheet'}.{fmt}"
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {fmt.upper()}",
            os.path.join(self.settings.get('export_path', ''), default_name),
            f"{fmt.upper()} Files (*.{fmt})"
        )
        if not path:
            return

        try:
            # Re-render at export quality
            dpi = self.overlay_set.render_dpi
            img_a = R.render_page(pair.page_a.pdf_path, pair.page_a.page_index, dpi)
            img_b_raw = R.render_page(pair.page_b.pdf_path, pair.page_b.page_index, dpi)
            img_b = R.apply_transform(img_b_raw, pair.offset_x, pair.offset_y,
                                       pair.rotation, pair.pivot_x, pair.pivot_y,
                                       pair.scale_factor, img_a.size)
            composite = R.composite_overlay(img_a, img_b,
                                             self.overlay_set.color_a,
                                             self.overlay_set.color_b)

            # White background for export
            bg = Image.new("RGBA", composite.size, (255, 255, 255, 255))
            bg.paste(composite, mask=composite)
            final = bg.convert("RGB")

            if fmt == 'png':
                final.save(path)
            elif fmt == 'pdf':
                final.save(path, "PDF", resolution=dpi)

            self.render_status.setText(f"Exported to {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

    def _group_style(self):
        return """
            QGroupBox {
                border: 1px solid #333; border-radius: 5px;
                margin-top: 6px; padding: 6px; color: #bbb; font-size: 10px; font-weight: bold;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; }
        """

    def _toggle_btn_style(self):
        return """
            QPushButton {
                background: #2a2a2a; color: #ccc; border: 1px solid #444;
                border-radius: 4px; padding: 5px; text-align: left;
            }
            QPushButton:checked { background: #2a4a6b; color: white; border-color: #4a8ab8; }
            QPushButton:hover { background: #333; }
        """
