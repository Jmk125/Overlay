"""
Overlay Viewer - main workspace
Pan/zoom, layer toggling, alignment, rotation, export
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QSlider, QSplitter, QScrollArea, QFrame,
    QListWidget, QListWidgetItem, QDoubleSpinBox, QSpinBox,
    QFileDialog, QCheckBox, QGroupBox, QMessageBox, QSizePolicy,
    QLineEdit, QProgressBar, QColorDialog
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QThread, QPointF, QRectF, QSizeF, QTimer
)
from PyQt6.QtGui import (
    QFont, QPixmap, QWheelEvent, QMouseEvent, QPainter,
    QColor, QPen, QBrush, QKeySequence, QShortcut, QCursor, QTransform
)
from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsEllipseItem, QGraphicsItem, QApplication, QPlainTextEdit
)
import math
import os
from PIL import Image
from core.models import OverlayPair, OverlaySet, COMMON_SCALES, compute_scale_factor
from core import renderer as R
from ui.collapsible import CollapsibleSection


class RenderWorker(QThread):
    # pix_a (colored A, canvas-sized), pix_b_raw (colored B, natural size,
    # untransformed — the canvas applies B's transform live), pix_composite
    done = pyqtSignal(object, object, object)

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

            # Apply transforms to B (only needed for the flattened composite)
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
            # A layer is canvas-sized; B layer is RAW (natural size, no
            # transform) so the canvas can move/rotate it live via Qt.
            solo_a = R.render_single_colored(img_a, self.overlay_set.color_a)
            solo_b_raw = R.render_single_colored(img_b_raw, self.overlay_set.color_b)

            if self.cancelled:
                return
            pix_composite = R.pil_to_qpixmap(composite)
            pix_a = R.pil_to_qpixmap(solo_a)
            pix_b = R.pil_to_qpixmap(solo_b_raw)
            self.done.emit(pix_a, pix_b, pix_composite)
        except Exception as e:
            if not self.cancelled:
                print(f"Render error: {e}")
                self.done.emit(None, None, None)


class MarkupOverlayItem(QGraphicsItem):
    """A single scene item that paints all of a pair's markups (plus the one
    currently being drawn). Coordinates are normalized 0-1 to the canvas."""
    def __init__(self, w: float, h: float):
        super().__init__()
        self._w = float(w)
        self._h = float(h)
        self._markups = []
        self._pending = None
        self._selected = None   # index of selected markup, or None
        self.setZValue(1000)   # always above the drawings

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def set_markups(self, markups: list):
        self._markups = markups
        self.update()

    def set_pending(self, m):
        self._pending = m
        self.update()

    def set_selected(self, idx):
        self._selected = idx
        self.update()

    def paint(self, painter, option, widget=None):
        items = list(self._markups)
        if self._pending:
            items = items + [self._pending]
        R.paint_markups(painter, items, self._w, self._h)

        # Selection highlight (dashed box + corner handles).
        if self._selected is not None and 0 <= self._selected < len(self._markups):
            pts = [(p[0] * self._w, p[1] * self._h)
                   for p in self._markups[self._selected].get('points', [])]
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                pad = 8
                rect = QRectF(min(xs) - pad, min(ys) - pad,
                              (max(xs) - min(xs)) + 2 * pad,
                              (max(ys) - min(ys)) + 2 * pad)
                pen = QPen(QColor('#00e0ff'))
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor('#00e0ff'))
                for hx, hy in [(rect.left(), rect.top()), (rect.right(), rect.top()),
                               (rect.left(), rect.bottom()), (rect.right(), rect.bottom())]:
                    painter.drawRect(QRectF(hx - 3, hy - 3, 6, 6))


class OverlayCanvas(QGraphicsView):
    """
    The main canvas.
    - Right-click drag = pan
    - Ctrl+scroll = zoom
    - Left click drag (when in drag mode) = move drawing B
    - Rotation handle drag = rotate drawing B
    """
    pair_changed = pyqtSignal()    # committed change -> recompute composite
    pair_preview = pyqtSignal()    # live change during a drag -> no recompute
    markups_changed = pyqtSignal() # a markup was added / removed

    MODE_VIEW = 0
    MODE_MOVE = 1
    MODE_ROTATE = 2
    MODE_MARKUP = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.gscene = QGraphicsScene(self)
        self.setScene(self.gscene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._bg_white = True
        self._apply_bg()

        # ── Configurable controls (set via apply_view_settings) ──
        self._zoom_on_scroll = True
        self._pan_button = Qt.MouseButton.RightButton
        self._antialiasing = True

        self._mode = self.MODE_VIEW
        self._panning = False
        self._pan_start = QPointF()
        self._drag_start = QPointF()
        self._pair: OverlayPair = None
        self._b_dragging = False     # left-drag of B in align mode in progress

        # Pixmap items
        self._item_a = None
        self._item_b = None
        self._item_composite = None

        # Natural (untransformed) size of the B layer pixmap
        self._b_size = None

        # Markups
        self._markup_item = None
        self._canvas_w = 1.0
        self._canvas_h = 1.0
        self._markup_tool = 'line'        # 'select' | 'line' | 'rect' | 'cloud'
        self._markup_color = '#ff3030'
        self._markup_width = 0.003        # normalized fraction of canvas width
        self._pending_markup = None
        self._selected_markup = None      # index of selected markup
        self._select_dragging = False
        self._select_last = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # receive Delete key

        # Live layered preview (A + transformed B) — on while aligning
        self._live = False

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

    _PAN_BUTTONS = {
        'left': Qt.MouseButton.LeftButton,
        'middle': Qt.MouseButton.MiddleButton,
        'right': Qt.MouseButton.RightButton,
    }

    def apply_view_settings(self, zoom_on_scroll: bool, pan_button: str, antialiasing: bool):
        """Apply user control/render preferences to the canvas."""
        self._zoom_on_scroll = zoom_on_scroll
        self._pan_button = self._PAN_BUTTONS.get(pan_button, Qt.MouseButton.RightButton)
        self.set_antialiasing(antialiasing)

    def set_antialiasing(self, on: bool):
        self._antialiasing = on
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, on)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, on)
        mode = (Qt.TransformationMode.SmoothTransformation if on
                else Qt.TransformationMode.FastTransformation)
        for item in (self._item_a, self._item_b, self._item_composite):
            if item:
                item.setTransformationMode(mode)
        if self.gscene:
            self.gscene.update()

    def set_mode(self, mode: int):
        self._mode = mode
        if mode == self.MODE_VIEW:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        elif mode == self.MODE_MOVE:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        elif mode == self.MODE_ROTATE:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        elif mode == self.MODE_MARKUP:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        # Selecting a tool does NOT switch to the live colored preview — that
        # only happens while you actually drag (see mousePressEvent). When idle
        # the canvas stays flattened to the composite.

    def load_pixmaps(self, pix_a, pix_b, pix_composite, pair: OverlayPair,
                     reset_view: bool = False):
        self._pix_a = pix_a
        self._pix_b = pix_b
        self._pix_composite = pix_composite
        self._pair = pair
        self._b_size = (pix_b.width(), pix_b.height()) if pix_b else None
        self.gscene.clear()
        self._item_a = self.gscene.addPixmap(pix_a if pix_a else QPixmap())
        self._item_b = self.gscene.addPixmap(pix_b if pix_b else QPixmap())
        self._item_composite = self.gscene.addPixmap(pix_composite if pix_composite else QPixmap())
        mode = (Qt.TransformationMode.SmoothTransformation if self._antialiasing
                else Qt.TransformationMode.FastTransformation)
        for item in (self._item_a, self._item_b, self._item_composite):
            item.setTransformationMode(mode)
        self._apply_b_transform()

        # Markup overlay sits above everything, sized to the canvas (A page).
        cw = pix_composite.width() if pix_composite else (pix_a.width() if pix_a else 1)
        ch = pix_composite.height() if pix_composite else (pix_a.height() if pix_a else 1)
        self._canvas_w, self._canvas_h = float(cw), float(ch)
        self._markup_item = MarkupOverlayItem(self._canvas_w, self._canvas_h)
        self.gscene.addItem(self._markup_item)
        self._selected_markup = None
        self._select_dragging = False
        if pair is not None:
            self._markup_item.set_markups(pair.markups)

        self._update_visibility()
        if reset_view:
            self.fitInView(self.gscene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    # ── Markups ───────────────────────────────────────────────────
    def set_markup_tool(self, tool: str):
        self._markup_tool = tool
        if tool != 'select':
            self._select_markup(None)

    def set_markup_color(self, hex_color: str):
        self._markup_color = hex_color

    def set_markup_width(self, width_norm: float):
        self._markup_width = width_norm

    def _scene_to_norm(self, scene_pos) -> list:
        return [scene_pos.x() / self._canvas_w, scene_pos.y() / self._canvas_h]

    def _select_markup(self, idx):
        self._selected_markup = idx
        if self._markup_item:
            self._markup_item.set_selected(idx)

    @staticmethod
    def _dist_to_segment(px, py, x0, y0, x1, y1) -> float:
        dx, dy = x1 - x0, y1 - y0
        if dx == 0 and dy == 0:
            return math.hypot(px - x0, py - y0)
        t = ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))

    def _markup_hit_test(self, scene_pos):
        """Return the index of the topmost markup near scene_pos, or None."""
        if not self._pair or not self._pair.markups:
            return None
        W, H = self._canvas_w, self._canvas_h
        scale = self.transform().m11() or 1.0
        tol = 10.0 / scale   # ~10 on-screen pixels in scene units
        px, py = scene_pos.x(), scene_pos.y()
        for i in range(len(self._pair.markups) - 1, -1, -1):
            m = self._pair.markups[i]
            pts = [(p[0] * W, p[1] * H) for p in m.get('points', [])]
            if len(pts) < 2:
                continue
            (x0, y0), (x1, y1) = pts[0], pts[1]
            if m.get('type') == 'line':
                if self._dist_to_segment(px, py, x0, y0, x1, y1) <= tol:
                    return i
            else:
                xmin, xmax = min(x0, x1), max(x0, x1)
                ymin, ymax = min(y0, y1), max(y0, y1)
                if xmin - tol <= px <= xmax + tol and ymin - tol <= py <= ymax + tol:
                    return i
        return None

    def markup_delete_selected(self):
        if (self._pair and self._selected_markup is not None
                and 0 <= self._selected_markup < len(self._pair.markups)):
            del self._pair.markups[self._selected_markup]
            self._select_markup(None)
            if self._markup_item:
                self._markup_item.set_markups(self._pair.markups)
            self.markups_changed.emit()

    def markup_undo(self):
        if self._pair and self._pair.markups:
            self._pair.markups.pop()
            self._select_markup(None)
            if self._markup_item:
                self._markup_item.set_markups(self._pair.markups)
            self.markups_changed.emit()

    def markup_clear(self):
        if self._pair and self._pair.markups:
            self._pair.markups.clear()
            self._select_markup(None)
            if self._markup_item:
                self._markup_item.set_markups(self._pair.markups)
            self.markups_changed.emit()

    def keyPressEvent(self, event):
        if (self._mode == self.MODE_MARKUP and self._selected_markup is not None
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)):
            self.markup_delete_selected()
            return
        super().keyPressEvent(event)

    def _apply_b_transform(self):
        """Position/rotate/scale the B layer live using a Qt affine transform
        derived from the same matrix that drives the final composite."""
        if not self._item_b or not self._pair or not self._b_size:
            return
        w, h = self._b_size
        p = self._pair
        M = R.forward_matrix(w, h, p.offset_x, p.offset_y, p.rotation,
                             p.pivot_x, p.pivot_y, p.scale_factor)
        # Qt maps (row-vector): x' = m11*x + m21*y + dx; y' = m12*x + m22*y + dy
        t = QTransform(M[0, 0], M[1, 0],
                       M[0, 1], M[1, 1],
                       M[0, 2], M[1, 2])
        self._item_b.setTransform(t)

    def set_view_mode(self, mode: str):
        """mode: 'composite', 'a', or 'b'"""
        self._view_mode = mode
        self._update_visibility()

    def _set_live(self, on: bool):
        self._live = on
        self._update_visibility()

    def show_committed(self):
        """Called once the post-drag composite has been re-rendered: drop the
        live colored layers and show the flattened composite. No-op while a
        drag is still in progress."""
        if not self._b_dragging:
            self._set_live(False)

    def _update_visibility(self):
        if not self._item_composite:
            return
        live = self._live
        # Live: overlay A + (transformed) B layers. Otherwise: chosen view.
        self._item_composite.setVisible((not live) and self._view_mode == 'composite')
        if self._item_a:
            self._item_a.setVisible(live or self._view_mode == 'a')
        if self._item_b:
            self._item_b.setVisible(live or self._view_mode == 'b')
            # Make B translucent while aligning so overlaps are visible.
            self._item_b.setOpacity(0.6 if live else 1.0)

    def wheelEvent(self, event: QWheelEvent):
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        # Zoom when scroll-zoom is enabled, or whenever Ctrl is held.
        if self._zoom_on_scroll or ctrl:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
        else:
            super().wheelEvent(event)

    def _pan_blocked_on_left(self) -> bool:
        """If panning is bound to the left button, don't pan while a tool that
        uses the left drag (align or markup) is active."""
        return (self._pan_button == Qt.MouseButton.LeftButton
                and self._mode in (self.MODE_MOVE, self.MODE_ROTATE, self.MODE_MARKUP))

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == self._pan_button and not self._pan_blocked_on_left():
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode == self.MODE_MARKUP and self._markup_item is not None:
                scene_pt = self.mapToScene(event.position().toPoint())
                if self._markup_tool == 'select':
                    idx = self._markup_hit_test(scene_pt)
                    self._select_markup(idx)
                    if idx is not None:
                        self._select_dragging = True
                        self._select_last = scene_pt
                    return
                start = self._scene_to_norm(scene_pt)
                self._pending_markup = {
                    'type': self._markup_tool,
                    'points': [start, list(start)],
                    'color': self._markup_color,
                    'width': self._markup_width,
                }
                self._markup_item.set_pending(self._pending_markup)
                return
            if self._mode in (self.MODE_MOVE, self.MODE_ROTATE):
                self._drag_start = self.mapToScene(event.position().toPoint())
                self._b_dragging = True
                # Switch to the live colored layers only for the duration of
                # the drag; we flatten back to the composite on release.
                self._set_live(True)
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

        if self._select_dragging and self._selected_markup is not None:
            sp = self.mapToScene(event.position().toPoint())
            dx = (sp.x() - self._select_last.x()) / self._canvas_w
            dy = (sp.y() - self._select_last.y()) / self._canvas_h
            self._select_last = sp
            m = self._pair.markups[self._selected_markup]
            m['points'] = [[p[0] + dx, p[1] + dy] for p in m['points']]
            self._markup_item.set_markups(self._pair.markups)
            return

        if self._pending_markup is not None:
            cur = self._scene_to_norm(self.mapToScene(event.position().toPoint()))
            self._pending_markup['points'][1] = cur
            self._markup_item.set_pending(self._pending_markup)
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
                self._apply_b_transform()
                self.pair_preview.emit()

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
                self._apply_b_transform()
                self.pair_preview.emit()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._select_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._select_dragging = False
            self.markups_changed.emit()   # committed move
            return
        if self._pending_markup is not None and event.button() == Qt.MouseButton.LeftButton:
            p0, p1 = self._pending_markup['points']
            # Discard accidental tiny marks.
            if abs(p1[0] - p0[0]) > 0.003 or abs(p1[1] - p0[1]) > 0.003:
                if self._pair is not None:
                    self._pair.markups.append(self._pending_markup)
                    self._markup_item.set_markups(self._pair.markups)
                    self.markups_changed.emit()
            self._pending_markup = None
            if self._markup_item:
                self._markup_item.set_pending(None)
            return
        if self._panning and event.button() == self._pan_button:
            self._panning = False
            self.set_mode(self._mode)
        if event.button() == Qt.MouseButton.LeftButton and self._b_dragging:
            # Drag finished — commit, which recomputes the shared-line composite.
            self._b_dragging = False
            self.pair_changed.emit()
        super().mouseReleaseEvent(event)

    def fit_view(self):
        self.fitInView(self.gscene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)


class OverlayViewer(QWidget):
    back_to_matching = pyqtSignal()
    save_project = pyqtSignal(object)  # emits OverlaySet

    MAX_CACHE = 24   # cap on cached pairs to bound memory on large sets

    def __init__(self, overlay_set: OverlaySet, settings: dict, parent=None):
        super().__init__(parent)
        self.overlay_set = overlay_set
        self.settings = settings
        self.current_pair_index = 0
        self._render_worker = None        # foreground (current view) worker
        self._bg_worker = None            # background prefetch worker
        self._worker_pool = []   # keeps workers alive until they finish naturally
        self._cache = {}         # pair index -> {'a','b','composite','sig'}
        self._dirty = False
        self._needs_fit = True   # fit-to-window only when switching pairs
        self._markup_color = '#ff3030'   # current markup color (mirrors canvas)

        self._build_ui()

        # Apply control/render preferences (zoom, pan button, antialiasing)
        self.apply_settings()

        # Sync background state from overlay_set (matters when loading a saved
        # project). Derive the shared-line color from the background so a saved
        # project can never show black linework on a dark canvas.
        white = (overlay_set.canvas_bg != 'dark')
        overlay_set.shared_color = '#000000' if white else '#ffffff'
        self.canvas.set_background(white)

        self._load_pair(0)

        # Debounce re-render on transform changes
        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(220)   # debounce before flattening to composite
        self._render_timer.timeout.connect(self._do_render)

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left: pair list (collapsible) ─────────────────────────
        self.left_panel = QWidget()
        self.left_panel.setFixedWidth(200)
        self.left_panel.setStyleSheet("background: #161616;")
        left_layout = QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)

        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("Overlay Pairs"))
        left_header.addStretch()
        collapse_left_btn = QPushButton("‹")
        collapse_left_btn.setFixedSize(22, 22)
        collapse_left_btn.setToolTip("Collapse panel")
        collapse_left_btn.setStyleSheet(self._collapse_btn_style())
        collapse_left_btn.clicked.connect(lambda: self._set_left_collapsed(True))
        left_header.addWidget(collapse_left_btn)
        left_layout.addLayout(left_header)

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

        root.addWidget(self.left_panel)

        # Thin strip shown when the left panel is collapsed.
        self.left_bar = self._make_collapsed_bar("›", "Show pairs",
                                                  lambda: self._set_left_collapsed(False))
        root.addWidget(self.left_bar)
        self.left_bar.setVisible(False)

        # ── Center: canvas + activity bar (spans only the canvas column,
        #    so it widens when the side panes are collapsed) ──
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        self.canvas = OverlayCanvas()
        self.canvas.pair_changed.connect(self._on_pair_changed)
        self.canvas.pair_preview.connect(self._on_pair_preview)
        center_layout.addWidget(self.canvas, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)          # indeterminate "busy" animation
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setStyleSheet("""
            QProgressBar { background: #161616; border: none; }
            QProgressBar::chunk { background: #3a6491; }
        """)
        self.progress.setVisible(False)
        center_layout.addWidget(self.progress)

        root.addWidget(center, 1)

        # Thin strip shown when the right panel is collapsed.
        self.right_bar = self._make_collapsed_bar("‹", "Show tools",
                                                   lambda: self._set_right_collapsed(False))
        root.addWidget(self.right_bar)
        self.right_bar.setVisible(False)

        # ── Right: tools panel (collapsible) ──────────────────────
        right_panel = QWidget()
        right_panel.setStyleSheet("background: #161616;")
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(right_panel)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setStyleSheet("border: none;")
        self.right_scroll.setFixedWidth(272)
        root.addWidget(self.right_scroll)

        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)

        right_header = QHBoxLayout()
        tools_label = QLabel("Tools")
        tools_label.setStyleSheet("color: #ddd; font-weight: bold;")
        right_header.addWidget(tools_label)
        right_header.addStretch()
        collapse_right_btn = QPushButton("›")
        collapse_right_btn.setFixedSize(22, 22)
        collapse_right_btn.setToolTip("Collapse panel")
        collapse_right_btn.setStyleSheet(self._collapse_btn_style())
        collapse_right_btn.clicked.connect(lambda: self._set_right_collapsed(True))
        right_header.addWidget(collapse_right_btn)
        right_layout.addLayout(right_header)

        # View section (collapsed by default)
        view_section = CollapsibleSection("View", collapsed=True)
        self.view_btns = {}
        for key, label in [('composite', 'Overlay (Both)'),
                            ('a', 'Set A only'),
                            ('b', 'Set B only')]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(self._toggle_btn_style())
            btn.clicked.connect(lambda checked, k=key: self._set_view(k))
            self.view_btns[key] = btn
            view_section.addWidget(btn)
        self.view_btns['composite'].setChecked(True)
        right_layout.addWidget(view_section)

        # Align section
        align_section = CollapsibleSection("Align Drawing B", collapsed=True)
        self.move_btn = QPushButton("↕  Move (click & drag)")
        self.move_btn.setCheckable(True)
        self.move_btn.setStyleSheet(self._toggle_btn_style())
        self.move_btn.clicked.connect(lambda: self._set_align_mode('move'))
        align_section.addWidget(self.move_btn)

        nudge_row = QHBoxLayout()
        for label, dx, dy in [('←', -1, 0), ('→', 1, 0), ('↑', 0, -1), ('↓', 0, 1)]:
            btn = QPushButton(label)
            btn.setFixedSize(32, 28)
            btn.setStyleSheet("background: #2a2a5e; color: white; border: none; border-radius: 3px;")
            btn.clicked.connect(lambda _, x=dx, y=dy: self._nudge(x, y))
            nudge_row.addWidget(btn)
        align_section.addLayout(nudge_row)
        align_section.addWidget(QLabel("Shift+drag = fine movement", styleSheet="color: #666; font-size: 9px;"))
        right_layout.addWidget(align_section)

        # Rotation section
        rot_section = CollapsibleSection("Rotation (Drawing B)", collapsed=True)
        quick_row = QHBoxLayout()
        for label, angle in [('90°', 90), ('180°', 180), ('270°', 270), ('45°', 45), ('-45°', -45)]:
            btn = QPushButton(label)
            btn.setFixedHeight(28)
            btn.setStyleSheet("background: #2a4a6b; color: white; border: none; border-radius: 3px; font-size: 10px;")
            btn.clicked.connect(lambda _, a=angle: self._rotate_quick(a))
            quick_row.addWidget(btn)
        rot_section.addLayout(quick_row)

        self.rotate_btn = QPushButton("↻  Free Rotate (click & drag)")
        self.rotate_btn.setCheckable(True)
        self.rotate_btn.setStyleSheet(self._toggle_btn_style())
        self.rotate_btn.clicked.connect(lambda: self._set_align_mode('rotate'))
        rot_section.addWidget(self.rotate_btn)

        rot_val_row = QHBoxLayout()
        rot_val_row.addWidget(QLabel("Angle:"))
        self.rot_spin = QDoubleSpinBox()
        self.rot_spin.setRange(-360, 360)
        self.rot_spin.setDecimals(1)
        self.rot_spin.setSuffix("°")
        self.rot_spin.setStyleSheet("background: #2a2a2a; color: #eee; border: 1px solid #555;")
        self.rot_spin.valueChanged.connect(self._on_rot_spin)
        rot_val_row.addWidget(self.rot_spin)
        rot_section.addLayout(rot_val_row)

        rot_section.addWidget(QLabel("Pivot (normalized 0-1):"))
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
        rot_section.addLayout(pivot_row)
        right_layout.addWidget(rot_section)

        # Scale section
        scale_section = CollapsibleSection("Scale", collapsed=True)
        scale_section.addWidget(QLabel("Set A scale:"))
        self.scale_a_combo = QComboBox()
        self.scale_a_combo.addItems(COMMON_SCALES)
        self.scale_a_combo.setEditable(True)
        self.scale_a_combo.setStyleSheet("background: #2a2a2a; color: #eee;")
        scale_section.addWidget(self.scale_a_combo)

        scale_section.addWidget(QLabel("Set B scale:"))
        self.scale_b_combo = QComboBox()
        self.scale_b_combo.addItems(COMMON_SCALES)
        self.scale_b_combo.setEditable(True)
        self.scale_b_combo.setStyleSheet("background: #2a2a2a; color: #eee;")
        scale_section.addWidget(self.scale_b_combo)

        apply_scale_btn = QPushButton("Apply Scale")
        apply_scale_btn.setStyleSheet("background: #2a4a6b; color: white; border: none; padding: 5px; border-radius: 4px;")
        apply_scale_btn.clicked.connect(self._apply_scale)
        scale_section.addWidget(apply_scale_btn)

        self.scale_status = QLabel("")
        self.scale_status.setStyleSheet("color: #888; font-size: 10px;")
        self.scale_status.setWordWrap(True)
        scale_section.addWidget(self.scale_status)
        right_layout.addWidget(scale_section)

        # Markups section
        markup_section = CollapsibleSection("Markups", collapsed=True)
        self.markup_btns = {}

        select_btn = QPushButton("◈ Select / Move")
        select_btn.setCheckable(True)
        select_btn.setStyleSheet(self._toggle_btn_style())
        select_btn.clicked.connect(lambda: self._set_markup_tool('select'))
        self.markup_btns['select'] = select_btn
        markup_section.addWidget(select_btn)

        tool_row = QHBoxLayout()
        for key, label in [('line', '╱ Line'), ('rect', '▭ Box'), ('cloud', '☁ Cloud')]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet(self._toggle_btn_style())
            b.clicked.connect(lambda _, k=key: self._set_markup_tool(k))
            self.markup_btns[key] = b
            tool_row.addWidget(b)
        markup_section.addLayout(tool_row)

        cw_row = QHBoxLayout()
        cw_row.addWidget(QLabel("Color:"))
        self.markup_color_btn = QPushButton()
        self.markup_color_btn.setFixedSize(40, 22)
        self.markup_color_btn.clicked.connect(self._pick_markup_color)
        self._refresh_markup_color_btn()
        cw_row.addWidget(self.markup_color_btn)
        cw_row.addWidget(QLabel("Width:"))
        self.markup_width_spin = QSpinBox()
        self.markup_width_spin.setRange(1, 20)
        self.markup_width_spin.setValue(3)
        self.markup_width_spin.setStyleSheet("background:#2a2a2a; color:#eee; border:1px solid #555;")
        self.markup_width_spin.valueChanged.connect(
            lambda v: self.canvas.set_markup_width(v / 1000.0))
        cw_row.addWidget(self.markup_width_spin)
        cw_row.addStretch()
        markup_section.addLayout(cw_row)

        uc_row = QHBoxLayout()
        undo_btn = QPushButton("↶ Undo")
        undo_btn.setStyleSheet("background:#3a3a3a; color:white; border:none; padding:4px; border-radius:3px;")
        undo_btn.clicked.connect(self.canvas.markup_undo)
        del_btn = QPushButton("🗑 Delete")
        del_btn.setToolTip("Delete the selected markup (or press Delete)")
        del_btn.setStyleSheet("background:#3a3a3a; color:white; border:none; padding:4px; border-radius:3px;")
        del_btn.clicked.connect(self.canvas.markup_delete_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.setStyleSheet("background:#5e2a2a; color:white; border:none; padding:4px; border-radius:3px;")
        clear_btn.clicked.connect(self.canvas.markup_clear)
        uc_row.addWidget(undo_btn)
        uc_row.addWidget(del_btn)
        uc_row.addWidget(clear_btn)
        markup_section.addLayout(uc_row)

        markup_section.addWidget(QLabel(
            "Draw tool: drag on the drawing. Select: click a markup to move it; "
            "Delete key removes it. Pan with right-drag.",
            styleSheet="color:#666; font-size:9px;", wordWrap=True))
        right_layout.addWidget(markup_section)

        # Notes section (per drawing)
        notes_section = CollapsibleSection("Notes (this drawing)", collapsed=True)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Notes for this drawing — saved with the project.")
        self.notes_edit.setFixedHeight(110)
        self.notes_edit.setStyleSheet("background:#1e1e1e; color:#eee; border:1px solid #444;")
        self.notes_edit.textChanged.connect(self._on_notes_changed)
        notes_section.addWidget(self.notes_edit)
        right_layout.addWidget(notes_section)

        # Always-visible quick actions
        reset_btn = QPushButton("Reset All Transforms")
        reset_btn.setStyleSheet("background: #5e2a2a; color: white; border: none; padding: 5px; border-radius: 4px;")
        reset_btn.clicked.connect(self._reset_transforms)
        right_layout.addWidget(reset_btn)

        fit_btn = QPushButton("Fit to Window")
        fit_btn.setStyleSheet("background: #333; color: white; border: none; padding: 5px; border-radius: 4px;")
        fit_btn.clicked.connect(self.canvas.fit_view)
        right_layout.addWidget(fit_btn)

        # Export / Save section
        export_section = CollapsibleSection("Export / Save", collapsed=True)
        save_btn = QPushButton("💾  Save Project")
        save_btn.setStyleSheet("background: #1a6b35; color: white; border: none; padding: 6px; border-radius: 4px;")
        save_btn.clicked.connect(lambda: self.save_project.emit(self.overlay_set))
        export_section.addWidget(save_btn)

        self.include_markups_chk = QCheckBox("Include markups in export")
        self.include_markups_chk.setChecked(True)
        self.include_markups_chk.setStyleSheet("color:#ccc;")
        export_section.addWidget(self.include_markups_chk)

        export_png_btn = QPushButton("Export PNG")
        export_png_btn.setStyleSheet("background: #2a4a6b; color: white; border: none; padding: 5px; border-radius: 4px;")
        export_png_btn.clicked.connect(lambda: self._export('png'))
        export_section.addWidget(export_png_btn)

        export_pdf_btn = QPushButton("Export PDF")
        export_pdf_btn.setStyleSheet("background: #2a4a6b; color: white; border: none; padding: 5px; border-radius: 4px;")
        export_pdf_btn.clicked.connect(lambda: self._export('pdf'))
        export_section.addWidget(export_pdf_btn)
        right_layout.addWidget(export_section)

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

    def _make_collapsed_bar(self, arrow: str, tooltip: str, on_click) -> QWidget:
        """A thin vertical strip with a single button to re-expand a panel."""
        bar = QWidget()
        bar.setFixedWidth(20)
        bar.setStyleSheet("background: #161616;")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 8, 0, 8)
        btn = QPushButton(arrow)
        btn.setFixedSize(18, 40)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(self._collapse_btn_style())
        btn.clicked.connect(on_click)
        lay.addWidget(btn)
        lay.addStretch()
        return bar

    def _set_left_collapsed(self, collapsed: bool):
        self.left_panel.setVisible(not collapsed)
        self.left_bar.setVisible(collapsed)

    def _set_right_collapsed(self, collapsed: bool):
        self.right_scroll.setVisible(not collapsed)
        self.right_bar.setVisible(collapsed)

    def apply_settings(self):
        """Push the current control/render preferences onto the canvas.
        Safe to call again after settings change in Preferences."""
        s = self.settings
        self.canvas.apply_view_settings(
            zoom_on_scroll=s.get('zoom_on_scroll', True),
            pan_button=s.get('pan_button', 'right'),
            antialiasing=s.get('antialiasing', True),
        )

    def apply_render_dpi(self, screen_dpi: int, export_dpi: int):
        """Apply DPI changes from Preferences to the open project. A changed
        screen DPI invalidates the cache and re-renders the current pair."""
        self.overlay_set.export_dpi = export_dpi
        if self.overlay_set.render_dpi != screen_dpi:
            self.overlay_set.render_dpi = screen_dpi
            self._invalidate_cache()
            self._do_render()

    def _load_pair(self, index: int):
        if index < 0 or index >= len(self.overlay_set.pairs):
            return
        self.current_pair_index = index
        self.pair_list.setCurrentRow(index)
        pair = self.overlay_set.pairs[index]

        # Update controls from pair state
        self._update_controls_from_pair(pair)
        self._needs_fit = True   # new pair -> fit to window once

        cached = self._cache.get(index)
        if cached and cached['sig'] == self._pair_sig(pair):
            # Instant: reuse the already-rendered pixmaps for this pair.
            self.canvas.load_pixmaps(cached['a'], cached['b'], cached['composite'],
                                     pair, reset_view=True)
            self._needs_fit = False
            self._restore_view()
            self.canvas.show_committed()
            self._set_progress(False)
            self.render_status.setText("Ready  (1=overlay  2=A only  3=B only)")
            self._schedule_prefetch()
        else:
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

        # Load this drawing's notes into the editor.
        self.notes_edit.blockSignals(True)
        self.notes_edit.setPlainText(pair.notes or "")
        self.notes_edit.blockSignals(False)

    def _current_pair(self) -> OverlayPair:
        return self.overlay_set.pairs[self.current_pair_index]

    # ── Rendering, caching & background prefetch ──────────────────
    def _pair_sig(self, pair: OverlayPair) -> tuple:
        """A signature of everything the rendered composite depends on. Two
        identical signatures => the cached pixmaps are still valid."""
        s = self.overlay_set
        return (
            round(pair.offset_x, 2), round(pair.offset_y, 2),
            round(pair.rotation, 3),
            round(pair.pivot_x, 4), round(pair.pivot_y, 4),
            round(pair.scale_factor, 5),
            s.color_a, s.color_b, s.shared_color, s.render_dpi,
        )

    def _restore_view(self):
        """Re-apply the currently selected view (composite / A / B)."""
        for k, btn in self.view_btns.items():
            if btn.isChecked():
                self.canvas.set_view_mode(k)
                return
        self.canvas.set_view_mode('composite')

    def _set_progress(self, busy: bool):
        self.progress.setVisible(busy)

    def _set_status_idle(self):
        """Show the cache progress if prefetch is still working, else Ready."""
        n = len(self.overlay_set.pairs)
        cached = self._cached_count()
        if cached < n:
            self.render_status.setText(f"Ready — caching pages in background ({cached}/{n})")
        else:
            self.render_status.setText("Ready  (1=overlay  2=A only  3=B only)")

    def _cached_count(self) -> int:
        return sum(1 for i, p in enumerate(self.overlay_set.pairs)
                   if (self._cache.get(i) or {}).get('sig') == self._pair_sig(p))

    def _do_render(self):
        """Foreground render of the current pair (the user is waiting on it)."""
        if not self.overlay_set.pairs:
            return
        idx = self.current_pair_index
        pair = self._current_pair()
        self._set_progress(True)
        self.render_status.setText("Rendering…")

        # Cancel the previous foreground worker, and pause background prefetch
        # so the CPU goes to what the user is looking at.
        if self._render_worker and self._render_worker.isRunning():
            self._render_worker.cancel()
        self._cancel_bg()

        self._worker_pool = [w for w in self._worker_pool if w.isRunning()]

        worker = RenderWorker(pair, self.overlay_set)
        worker.index = idx
        worker.sig = self._pair_sig(pair)
        worker.done.connect(lambda a, b, c, w=worker: self._on_render_done(w, a, b, c))
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._render_worker = worker
        self._worker_pool.append(worker)
        worker.start()

    def _on_worker_finished(self, worker):
        """A render thread fully ended; reclaim it and resume prefetch (now
        that the CPU is free)."""
        if worker in self._worker_pool:
            self._worker_pool.remove(worker)
        self._schedule_prefetch()

    def _on_render_done(self, worker, pix_a, pix_b, pix_composite):
        if getattr(worker, 'cancelled', False):
            return
        if pix_composite is None:
            self._set_progress(False)
            self.render_status.setText("Render failed — check console")
            return
        # Don't rebuild the scene mid-drag (it would disrupt the live item).
        # A fresh render is triggered on release, so dropping this is safe.
        if self.canvas._b_dragging:
            return

        self._store_cache(worker.index, pix_a, pix_b, pix_composite, worker.sig)

        # Only paint it if this result is still the pair on screen.
        if worker.index == self.current_pair_index:
            pair = self._current_pair()
            self.canvas.load_pixmaps(pix_a, pix_b, pix_composite, pair,
                                     reset_view=self._needs_fit)
            self._needs_fit = False
            self._restore_view()
            # Flatten: drop the live colored layers now the composite is fresh.
            self.canvas.show_committed()
            self._set_progress(False)

        self._set_status_idle()
        self._schedule_prefetch()

    def _store_cache(self, index: int, pix_a, pix_b, pix_composite, sig):
        self._cache[index] = {'a': pix_a, 'b': pix_b,
                              'composite': pix_composite, 'sig': sig}
        self._evict_cache()

    def _evict_cache(self):
        """Keep at most MAX_CACHE entries, dropping those farthest from the
        current pair first (never the current one)."""
        if len(self._cache) <= self.MAX_CACHE:
            return
        cur = self.current_pair_index
        # Sort cached indices by distance from current, keep the nearest.
        victims = sorted(self._cache.keys(), key=lambda i: -abs(i - cur))
        for idx in victims:
            if len(self._cache) <= self.MAX_CACHE:
                break
            if idx != cur:
                self._cache.pop(idx, None)

    def _invalidate_cache(self, index: int = None):
        """Drop one pair's cache (index given) or the whole cache (None)."""
        if index is None:
            self._cache.clear()
        else:
            self._cache.pop(index, None)

    def _prefetch_order(self) -> list:
        """Indices to prefetch, nearest to the current pair first."""
        cur = self.current_pair_index
        n = len(self.overlay_set.pairs)
        order = []
        for d in range(1, n):
            for idx in (cur + d, cur - d):
                if 0 <= idx < n:
                    order.append(idx)
        return order

    def _schedule_prefetch(self):
        """Kick off one background render of the nearest uncached pair, if the
        foreground is idle and we aren't already prefetching."""
        if self._render_worker and self._render_worker.isRunning():
            return
        if self._bg_worker and self._bg_worker.isRunning():
            return
        if len(self._cache) >= self.MAX_CACHE:
            return  # cache full — don't thrash
        for idx in self._prefetch_order():
            pair = self.overlay_set.pairs[idx]
            c = self._cache.get(idx)
            if not (c and c['sig'] == self._pair_sig(pair)):
                self._start_bg_render(idx)
                return
        self._set_status_idle()

    def _start_bg_render(self, index: int):
        pair = self.overlay_set.pairs[index]
        self._worker_pool = [w for w in self._worker_pool if w.isRunning()]
        worker = RenderWorker(pair, self.overlay_set)
        worker.index = index
        worker.sig = self._pair_sig(pair)
        worker.done.connect(lambda a, b, c, w=worker: self._on_bg_done(w, a, b, c))
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        self._bg_worker = worker
        self._worker_pool.append(worker)
        worker.start()
        self._set_status_idle()

    def _on_bg_done(self, worker, pix_a, pix_b, pix_composite):
        if getattr(worker, 'cancelled', False):
            return
        if pix_composite is not None:
            self._store_cache(worker.index, pix_a, pix_b, pix_composite, worker.sig)
        self._set_status_idle()
        # Chain to the next uncached pair.
        self._schedule_prefetch()

    def _cancel_bg(self):
        if self._bg_worker and self._bg_worker.isRunning():
            self._bg_worker.cancel()
        self._bg_worker = None

    def _shutdown(self):
        """Cancel and join all render threads before this screen is destroyed."""
        self._cancel_bg()
        for w in list(self._worker_pool):
            try:
                w.cancel()
            except Exception:
                pass
        for w in list(self._worker_pool):
            try:
                if w.isRunning():
                    w.wait(1500)
            except Exception:
                pass

    def set_background_mode(self, mode: str):
        """Switch canvas background between 'white' and 'dark', flipping the
        shared-line color accordingly. Driven from Preferences."""
        if mode not in ('white', 'dark'):
            return
        self.overlay_set.canvas_bg = mode
        white = (mode == 'white')
        self.overlay_set.shared_color = '#000000' if white else '#ffffff'
        self.canvas.set_background(white)
        # Shared-line color affects every pair — invalidate the whole cache.
        self._invalidate_cache()
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
        # Leaving markup mode — clear the markup tool buttons.
        for b in self.markup_btns.values():
            b.setChecked(False)

    def _set_markup_tool(self, tool: str):
        self.canvas.set_markup_tool(tool)
        self.canvas.set_mode(OverlayCanvas.MODE_MARKUP)
        for k, b in self.markup_btns.items():
            b.setChecked(k == tool)
        # Markup mode is exclusive with the align tools.
        self.move_btn.setChecked(False)
        self.rotate_btn.setChecked(False)

    def _pick_markup_color(self):
        c = QColorDialog.getColor(QColor(self._markup_color), self, "Markup Color")
        if c.isValid():
            self._markup_color = c.name()
            self.canvas.set_markup_color(self._markup_color)
            self._refresh_markup_color_btn()

    def _refresh_markup_color_btn(self):
        self.markup_color_btn.setStyleSheet(
            f"background:{self._markup_color}; border:1px solid #777; border-radius:3px;")

    def _on_notes_changed(self):
        if 0 <= self.current_pair_index < len(self.overlay_set.pairs):
            self._current_pair().notes = self.notes_edit.toPlainText()

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

    def _on_pair_preview(self):
        """Live drag update: refresh the rotation readout only (the canvas
        has already moved the B layer). No re-render — that's the whole point."""
        pair = self._current_pair()
        self.rot_spin.blockSignals(True)
        self.rot_spin.setValue(pair.rotation)
        self.rot_spin.blockSignals(False)

    def _on_pair_changed(self):
        # Committed change (drag release, nudge, spinbox, scale, reset).
        # The current pair's cached render is now stale.
        self._invalidate_cache(self.current_pair_index)
        # Update the live B layer immediately, then debounce the heavier
        # shared-line composite recompute.
        pair = self._current_pair()
        self.rot_spin.blockSignals(True)
        self.rot_spin.setValue(pair.rotation)
        self.rot_spin.blockSignals(False)
        self.canvas._apply_b_transform()
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
            # Re-render at export quality (independent of the on-screen DPI)
            dpi = getattr(self.overlay_set, 'export_dpi', None) or self.overlay_set.render_dpi
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

            # Optionally burn in the user's markups at export resolution.
            if self.include_markups_chk.isChecked() and pair.markups:
                W, H = final.size
                mk = R.render_markups_pil(pair.markups, W, H)
                final = final.convert("RGBA")
                final.alpha_composite(mk)
                final = final.convert("RGB")

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

    def _collapse_btn_style(self):
        return """
            QPushButton {
                background: #2a2a2a; color: #aaa; border: 1px solid #444;
                border-radius: 3px; font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background: #3a3a3a; color: #fff; }
        """
