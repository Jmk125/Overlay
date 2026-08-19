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
    QColor, QPen, QBrush, QKeySequence, QShortcut, QCursor, QTransform,
    QPolygonF
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


class MaskedOverlayItem(QGraphicsItem):
    """Paints content clipped to the union of mask polygons, plus dashed
    outlines of each mask and the one currently being drawn.

    Normally the clipped content is the composite pixmap (full A+B overlay
    shows through the masked "windows"). In cutout mode it's the OTHER
    drawing instead — a punched-hole reveal of just that drawing, with no
    blending — positioned with its own transform since the "other" drawing
    may be B (which is not canvas-aligned like the composite/A layer are)."""
    def __init__(self, w: float, h: float):
        super().__init__()
        self._w = float(w)
        self._h = float(h)
        self._composite_pixmap = None
        self._masks = []
        self._pending_points = []
        self._show_outlines = False
        self._show_content = False
        self._cutout = False
        self._other_pixmap = None
        self._other_transform = QTransform()
        self._bg_color = QColor('#ffffff')
        self._edit_index = None
        self._edit_selected_vertex = None
        self._recolor_cache = {}   # color hex -> recolored other_pixmap
        self.setZValue(900)   # above the base layers, below markups (1000)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def set_composite_pixmap(self, pixmap):
        self._composite_pixmap = pixmap
        self.update()

    def set_masks(self, masks: list):
        self._masks = masks
        self._recolor_cache = {}   # a mask's color override may have changed
        self.update()

    def set_pending_points(self, points: list):
        self._pending_points = points
        self.update()

    def set_show_outlines(self, on: bool):
        self._show_outlines = on
        self.update()

    def set_show_content(self, on: bool):
        self._show_content = on
        self.update()

    def set_cutout(self, on: bool):
        self._cutout = on
        self.update()

    def set_other_pixmap(self, pixmap):
        self._other_pixmap = pixmap
        self._recolor_cache = {}   # base image changed — cached tints are stale
        self.update()

    def _recolored_other(self, color_hex: str):
        """A tinted copy of the "other" drawing's pixmap: same ink/alpha
        pattern, RGB replaced with `color_hex`. Cached per color since it's
        recomputed on every repaint otherwise."""
        if not self._other_pixmap:
            return None
        cached = self._recolor_cache.get(color_hex)
        if cached is not None:
            return cached
        result = QPixmap(self._other_pixmap.size())
        result.fill(Qt.GlobalColor.transparent)
        p = QPainter(result)
        p.drawPixmap(0, 0, self._other_pixmap)
        # SourceIn keeps the destination's alpha (the ink pattern) and takes
        # the newly-painted color for RGB — a cheap way to retint an
        # alpha-mask image without re-rendering from the source PDF.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(result.rect(), QColor(color_hex))
        p.end()
        self._recolor_cache[color_hex] = result
        return result

    def set_other_transform(self, transform: QTransform):
        self._other_transform = transform
        self.update()

    def set_bg_color(self, color: QColor):
        self._bg_color = color
        self.update()

    def set_edit_index(self, index):
        self._edit_index = index
        self.update()

    def set_edit_selected_vertex(self, index):
        self._edit_selected_vertex = index
        self.update()

    def paint(self, painter, option, widget=None):
        # The filled mask content (overlay or cutout reveal) only belongs to
        # the actual "Masked Overlay" view — never while just drawing/editing
        # shapes over a different view (see set_show_outlines below, which
        # covers that case independently).
        if self._show_content:
            if self._cutout:
                # Each mask can override its own reveal color, so clip and
                # draw them one at a time (rather than one shared union-clip
                # draw) — a plain mask uses the pair's default other_pixmap,
                # a colored one uses its cached tint.
                for m in self._masks:
                    if not m.get('visible', True):
                        continue
                    sub_path = R.mask_clip_qpath([m], self._w, self._h)
                    if sub_path.isEmpty():
                        continue
                    color = m.get('color')
                    pixmap = self._recolored_other(color) if color else self._other_pixmap
                    if not pixmap:
                        continue
                    painter.save()
                    painter.setClipPath(sub_path)
                    # The base layer is painted underneath this item, so a
                    # blank (no-ink) patch of the "other" drawing would let
                    # it bleed through here otherwise. Paint the canvas
                    # background first to fully occlude the base within the
                    # hole — a real cutout reveals the whole other sheet,
                    # not just its ink over a see-through gap.
                    painter.fillRect(self.boundingRect(), self._bg_color)
                    painter.setTransform(self._other_transform, True)
                    painter.drawPixmap(0, 0, pixmap)
                    painter.restore()
            elif self._composite_pixmap:
                path = R.mask_clip_qpath(self._masks, self._w, self._h)
                if not path.isEmpty():
                    painter.save()
                    painter.setClipPath(path)   # clip stays fixed in canvas coords
                    painter.drawPixmap(0, 0, self._composite_pixmap)
                    painter.restore()

        # The mask being reshaped in Edit mode always shows its handles,
        # regardless of whether outline previews are otherwise on.
        if self._edit_index is not None and 0 <= self._edit_index < len(self._masks):
            pts = self._masks[self._edit_index].get('points', [])
            spts = [QPointF(p[0] * self._w, p[1] * self._h) for p in pts]
            if len(spts) >= 2:
                epen = QPen(QColor('#00e0ff'))
                epen.setWidthF(2)
                epen.setCosmetic(True)
                painter.setPen(epen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPolygon(QPolygonF(spts))
                painter.setPen(Qt.PenStyle.NoPen)
                for i, p in enumerate(spts):
                    painter.setBrush(QColor('#ffdd00') if i == self._edit_selected_vertex
                                     else QColor('#00e0ff'))
                    painter.drawEllipse(p, 5, 5)

        if not self._show_outlines:
            return
        pen = QPen(QColor('#ffb300'))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for m in self._masks:
            if not m.get('visible', True):
                continue
            pts = m.get('points', [])
            if len(pts) >= 2:
                poly = QPolygonF([QPointF(p[0] * self._w, p[1] * self._h) for p in pts])
                painter.drawPolygon(poly)
        if self._pending_points:
            poly_pts = [QPointF(p[0] * self._w, p[1] * self._h) for p in self._pending_points]
            if len(poly_pts) >= 2:
                painter.drawPolyline(QPolygonF(poly_pts))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#ffb300'))
            for p in poly_pts:
                painter.drawEllipse(p, 4, 4)


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
    masks_changed = pyqtSignal()   # a mask was added / removed
    mode_changed = pyqtSignal(int) # the interaction mode changed (incl. via keyboard)

    MODE_VIEW = 0
    MODE_MOVE = 1
    MODE_ROTATE = 2
    MODE_MARKUP = 3
    MODE_MASK = 4
    MODE_MASK_EDIT = 5

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
        self._markup_tool = 'line'        # 'select' | 'line' | 'polyline' | 'rect' | 'cloud'
        self._markup_color = '#ff3030'
        self._markup_width = 0.003        # normalized fraction of canvas width
        self._pending_markup = None
        self._selected_markup = None      # index of selected markup
        self._select_dragging = False
        self._select_last = None
        self._polyline_points = []        # in-progress polyline markup, normalized 0-1

        # Masks (windowed overlay)
        self._mask_item = None
        self._mask_points = []            # in-progress polygon, normalized 0-1
        self._mask_edit_index = None      # index of the mask being reshaped
        self._mask_edit_selected = None   # index of its selected vertex
        self._mask_edit_drag = None       # ('vertex', idx) or ('shape', last_scene_pos)

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
        if getattr(self, '_mask_item', None):
            self._mask_item.set_bg_color(QColor(color))

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
        if (self._mode == self.MODE_MARKUP and mode != self.MODE_MARKUP
                and self._markup_tool == 'polyline'):
            self._cancel_polyline_markup()
        if self._mode == self.MODE_MASK and mode != self.MODE_MASK:
            self.mask_cancel_pending()
        if self._mode == self.MODE_MASK_EDIT and mode != self.MODE_MASK_EDIT:
            self._mask_edit_index = None
            self._mask_edit_selected = None
            self._mask_edit_drag = None
            if self._mask_item:
                self._mask_item.set_edit_index(None)
                self._mask_item.set_edit_selected_vertex(None)
        self._mode = mode
        if mode == self.MODE_VIEW:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        elif mode == self.MODE_MOVE:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        elif mode == self.MODE_ROTATE:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        elif mode == self.MODE_MARKUP:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        elif mode == self.MODE_MASK:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        elif mode == self.MODE_MASK_EDIT:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        # Selecting a tool does NOT switch to the live colored preview — that
        # only happens while you actually drag (see mousePressEvent). When idle
        # the canvas stays flattened to the composite.
        self._update_visibility()
        self.mode_changed.emit(mode)

    def start_mask_edit(self, index: int):
        """Enter Edit mode for one mask: drag its vertices to reshape it, or
        drag its body to move the whole shape."""
        if not self._pair or not (0 <= index < len(self._pair.masks)):
            return
        self._mask_edit_index = index
        self.set_mode(self.MODE_MASK_EDIT)
        if self._mask_item:
            self._mask_item.set_edit_index(index)

    def stop_mask_edit(self):
        self.set_mode(self.MODE_VIEW)

    def load_pixmaps(self, pix_a, pix_b, pix_composite, pair: OverlayPair,
                     reset_view: bool = False):
        self._pix_a = pix_a
        self._pix_b = pix_b
        self._pix_composite = pix_composite
        self._pair = pair
        self._b_size = (pix_b.width(), pix_b.height()) if pix_b else None
        self.gscene.clear()
        # gscene.clear() just deleted the underlying C++ objects for these —
        # drop the stale Python references so nothing touches them before
        # they're recreated below (_apply_b_transform syncs the mask layer).
        self._mask_item = None
        self._markup_item = None
        # A fresh pair (or re-render) invalidates any in-progress mask edit.
        self._mask_edit_index = None
        self._mask_edit_selected = None
        self._mask_edit_drag = None
        self._item_a = self.gscene.addPixmap(pix_a if pix_a else QPixmap())
        self._item_b = self.gscene.addPixmap(pix_b if pix_b else QPixmap())
        self._item_composite = self.gscene.addPixmap(pix_composite if pix_composite else QPixmap())
        mode = (Qt.TransformationMode.SmoothTransformation if self._antialiasing
                else Qt.TransformationMode.FastTransformation)
        for item in (self._item_a, self._item_b, self._item_composite):
            item.setTransformationMode(mode)
        self._apply_b_transform()

        # Masked-overlay layer: composite clipped to the mask polygon(s), sits
        # above the base layers, sized to the canvas (A page).
        cw = pix_composite.width() if pix_composite else (pix_a.width() if pix_a else 1)
        ch = pix_composite.height() if pix_composite else (pix_a.height() if pix_a else 1)
        self._canvas_w, self._canvas_h = float(cw), float(ch)
        self._mask_item = MaskedOverlayItem(self._canvas_w, self._canvas_h)
        self.gscene.addItem(self._mask_item)
        self._mask_item.set_composite_pixmap(pix_composite)
        self._mask_item.set_bg_color(QColor('#ffffff' if self._bg_white else '#0d0d0d'))
        self._mask_points = []
        if pair is not None:
            self._mask_item.set_masks(pair.masks)
        self._sync_mask_cutout_layer()

        # Markup overlay sits above everything.
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
        if self._markup_tool == 'polyline' and tool != 'polyline':
            self._cancel_polyline_markup()
        self._markup_tool = tool
        if tool != 'select':
            self._select_markup(None)

    def _finish_polyline_markup(self):
        """Close the in-progress polyline (needs >= 2 points) into a new
        markup, then stop — a fresh click starts a separate one."""
        if self._pair is not None and len(self._polyline_points) >= 2:
            markup = {
                'type': 'polyline',
                'points': [list(p) for p in self._polyline_points],
                'color': self._markup_color,
                'width': self._markup_width,
            }
            self._pair.markups.append(markup)
            if self._markup_item:
                self._markup_item.set_markups(self._pair.markups)
            self.markups_changed.emit()
        self._cancel_polyline_markup()

    def _cancel_polyline_markup(self):
        self._polyline_points = []
        self._pending_markup = None
        if self._markup_item:
            self._markup_item.set_pending(None)

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
            mtype = m.get('type')
            if mtype in ('line', 'polyline'):
                # Check every segment, not just the first — a polyline can
                # have more than two points.
                if any(self._dist_to_segment(px, py, x0, y0, x1, y1) <= tol
                      for (x0, y0), (x1, y1) in zip(pts, pts[1:])):
                    return i
            else:
                (x0, y0), (x1, y1) = pts[0], pts[1]
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
        if self._mode == self.MODE_MARKUP and self._markup_tool == 'polyline':
            if event.key() == Qt.Key.Key_Escape:
                self._cancel_polyline_markup()
                return
            if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self._polyline_points:
                self._polyline_points.pop()
                if self._pending_markup:
                    self._pending_markup['points'] = [list(p) for p in self._polyline_points]
                if self._markup_item:
                    self._markup_item.set_pending(self._pending_markup if self._polyline_points else None)
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_polyline_markup()
                return
        if self._mode == self.MODE_MASK:
            if event.key() == Qt.Key.Key_Escape:
                self.mask_cancel_pending()
                return
            if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self._mask_points:
                self._mask_points.pop()
                if self._mask_item:
                    self._mask_item.set_pending_points(self._mask_points)
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_mask_polygon()
                return
        if self._mode == self.MODE_MASK_EDIT:
            if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.stop_mask_edit()
                return
            if (event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                    and self._mask_edit_selected is not None):
                self._mask_edit_delete_selected_vertex()
                return
        super().keyPressEvent(event)

    def _b_qtransform(self) -> QTransform:
        """The Qt affine transform that positions/rotates/scales the B layer,
        derived from the same matrix that drives the final composite."""
        if not self._pair or not self._b_size:
            return QTransform()
        w, h = self._b_size
        p = self._pair
        M = R.forward_matrix(w, h, p.offset_x, p.offset_y, p.rotation,
                             p.pivot_x, p.pivot_y, p.scale_factor)
        # Qt maps (row-vector): x' = m11*x + m21*y + dx; y' = m12*x + m22*y + dy
        return QTransform(M[0, 0], M[1, 0],
                          M[0, 1], M[1, 1],
                          M[0, 2], M[1, 2])

    def _apply_b_transform(self):
        if not self._item_b:
            return
        self._item_b.setTransform(self._b_qtransform())
        self._sync_mask_cutout_layer()

    def _sync_mask_cutout_layer(self):
        """Keep the mask-cutout preview's "other drawing" pixmap/transform in
        sync with the current pair (mask_base flip, cutout toggle, or a live
        move/rotate of B)."""
        if not self._mask_item or not self._pair:
            return
        self._mask_item.set_cutout(getattr(self._pair, 'mask_cutout', False))
        if self._pair.mask_base == 'a':
            self._mask_item.set_other_pixmap(self._pix_b)
            self._mask_item.set_other_transform(self._b_qtransform())
        else:
            self._mask_item.set_other_pixmap(self._pix_a)
            self._mask_item.set_other_transform(QTransform())

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
        # In the "mask" view, only the chosen base drawing (A or B) shows
        # outside the mask(s); the composite shows through inside them.
        mask_mode = (not live) and self._view_mode == 'mask'
        mask_base = (self._pair.mask_base if self._pair else 'a') if mask_mode else None

        # Live: overlay A + (transformed) B layers. Otherwise: chosen view.
        self._item_composite.setVisible((not live) and self._view_mode == 'composite')
        if self._item_a:
            self._item_a.setVisible(live or self._view_mode == 'a' or mask_base == 'a')
        if self._item_b:
            self._item_b.setVisible(live or self._view_mode == 'b' or mask_base == 'b')
            # Make B translucent while aligning so overlaps are visible.
            self._item_b.setOpacity(0.6 if live else 1.0)
        if self._mask_item:
            # The filled mask CONTENT only ever shows in the "Masked Overlay"
            # view — never bleeds into Overlay/A-only/B-only. But the drawing
            # aids (dots, pending line, existing outlines, edit handles) stay
            # visible while actively drawing/editing regardless of which view
            # is selected, so you can draw or reshape a mask while referencing
            # a specific drawing.
            drawing = self._mode in (self.MODE_MASK, self.MODE_MASK_EDIT)
            self._mask_item.setVisible(mask_mode or drawing)
            self._mask_item.set_show_content(mask_mode)
            self._mask_item.set_show_outlines(drawing)

    def refresh_mask_view(self):
        """Re-evaluate visibility and the cutout layer after `pair.mask_base`
        or `pair.mask_cutout` changes."""
        self._update_visibility()
        self._sync_mask_cutout_layer()

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
        uses the left drag (align, markup or mask) is active."""
        return (self._pan_button == Qt.MouseButton.LeftButton
                and self._mode in (self.MODE_MOVE, self.MODE_ROTATE, self.MODE_MARKUP,
                                   self.MODE_MASK, self.MODE_MASK_EDIT))

    def _mask_edit_hit_vertex(self, scene_pos):
        """Index of the vertex of the mask being edited near scene_pos, or None."""
        if self._mask_edit_index is None or not self._pair:
            return None
        if not (0 <= self._mask_edit_index < len(self._pair.masks)):
            return None
        pts = self._pair.masks[self._mask_edit_index].get('points', [])
        W, H = self._canvas_w, self._canvas_h
        scale = self.transform().m11() or 1.0
        tol = 10.0 / scale
        px, py = scene_pos.x(), scene_pos.y()
        for i in range(len(pts) - 1, -1, -1):
            x, y = pts[i][0] * W, pts[i][1] * H
            if math.hypot(px - x, py - y) <= tol:
                return i
        return None

    def _mask_edit_hit_edge(self, scene_pos):
        """Index to insert a new vertex at if scene_pos is near one of the
        editing mask's edges, else None. The new point goes between the edge's
        two endpoints — i.e. right before the returned index."""
        if self._mask_edit_index is None or not self._pair:
            return None
        if not (0 <= self._mask_edit_index < len(self._pair.masks)):
            return None
        pts = self._pair.masks[self._mask_edit_index].get('points', [])
        n = len(pts)
        if n < 2:
            return None
        W, H = self._canvas_w, self._canvas_h
        scale = self.transform().m11() or 1.0
        tol = 10.0 / scale
        px, py = scene_pos.x(), scene_pos.y()
        for i in range(n):
            x0, y0 = pts[i][0] * W, pts[i][1] * H
            x1, y1 = pts[(i + 1) % n][0] * W, pts[(i + 1) % n][1] * H
            if self._dist_to_segment(px, py, x0, y0, x1, y1) <= tol:
                return i + 1
        return None

    def _mask_edit_insert_vertex(self, insert_idx: int, scene_pos):
        pts = self._pair.masks[self._mask_edit_index]['points']
        pts.insert(insert_idx, self._scene_to_norm(scene_pos))
        self._mask_edit_select_vertex(insert_idx)
        if self._mask_item:
            self._mask_item.set_masks(self._pair.masks)

    def _mask_edit_point_inside(self, scene_pos) -> bool:
        """True if scene_pos falls inside the mask currently being edited."""
        if self._mask_edit_index is None or not self._pair:
            return False
        if not (0 <= self._mask_edit_index < len(self._pair.masks)):
            return False
        m = dict(self._pair.masks[self._mask_edit_index])
        m['visible'] = True   # editing a hidden mask should still hit-test
        path = R.mask_clip_qpath([m], self._canvas_w, self._canvas_h)
        return path.contains(scene_pos)

    def _mask_edit_select_vertex(self, idx):
        self._mask_edit_selected = idx
        if self._mask_item:
            self._mask_item.set_edit_selected_vertex(idx)

    def _mask_edit_delete_selected_vertex(self):
        if self._mask_edit_index is None or not self._pair:
            return
        pts = self._pair.masks[self._mask_edit_index].get('points', [])
        idx = self._mask_edit_selected
        if idx is None or not (0 <= idx < len(pts)) or len(pts) <= 3:
            return
        del pts[idx]
        self._mask_edit_select_vertex(None)
        if self._mask_item:
            self._mask_item.set_masks(self._pair.masks)

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
                if self._markup_tool == 'polyline':
                    self._polyline_points.append(self._scene_to_norm(scene_pt))
                    self._pending_markup = {
                        'type': 'polyline',
                        'points': [list(p) for p in self._polyline_points],
                        'color': self._markup_color,
                        'width': self._markup_width,
                    }
                    self._markup_item.set_pending(self._pending_markup)
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
            if self._mode == self.MODE_MASK and self._mask_item is not None:
                scene_pt = self.mapToScene(event.position().toPoint())
                self._mask_points.append(self._scene_to_norm(scene_pt))
                self._mask_item.set_pending_points(self._mask_points)
                return
            if self._mode == self.MODE_MASK_EDIT:
                scene_pt = self.mapToScene(event.position().toPoint())
                vidx = self._mask_edit_hit_vertex(scene_pt)
                if vidx is not None:
                    self._mask_edit_drag = ('vertex', vidx)
                    self._mask_edit_select_vertex(vidx)
                elif self._mask_edit_point_inside(scene_pt):
                    self._mask_edit_drag = ('shape', scene_pt)
                    self._mask_edit_select_vertex(None)
                else:
                    self._mask_edit_drag = None
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

        if self._pending_markup is not None and self._markup_tool != 'polyline':
            cur = self._scene_to_norm(self.mapToScene(event.position().toPoint()))
            self._pending_markup['points'][1] = cur
            self._markup_item.set_pending(self._pending_markup)
            return

        if self._mask_edit_drag is not None and self._pair is not None:
            scene_pt = self.mapToScene(event.position().toPoint())
            kind, payload = self._mask_edit_drag
            pts = self._pair.masks[self._mask_edit_index]['points']
            if kind == 'vertex':
                pts[payload] = self._scene_to_norm(scene_pt)
            else:   # 'shape' — payload is the last scene position
                dx = (scene_pt.x() - payload.x()) / self._canvas_w
                dy = (scene_pt.y() - payload.y()) / self._canvas_h
                for p in pts:
                    p[0] += dx
                    p[1] += dy
                self._mask_edit_drag = ('shape', scene_pt)
            if self._mask_item:
                self._mask_item.set_masks(self._pair.masks)
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
        if self._mask_edit_drag is not None and event.button() == Qt.MouseButton.LeftButton:
            self._mask_edit_drag = None
            return
        if self._select_dragging and event.button() == Qt.MouseButton.LeftButton:
            self._select_dragging = False
            self.markups_changed.emit()   # committed move
            return
        if (self._pending_markup is not None and self._markup_tool != 'polyline'
                and event.button() == Qt.MouseButton.LeftButton):
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

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self._mode == self.MODE_MASK and event.button() == Qt.MouseButton.LeftButton:
            self._finish_mask_polygon()
            return
        if self._mode == self.MODE_MASK_EDIT and event.button() == Qt.MouseButton.LeftButton:
            scene_pt = self.mapToScene(event.position().toPoint())
            insert_idx = self._mask_edit_hit_edge(scene_pt)
            if insert_idx is not None:
                self._mask_edit_insert_vertex(insert_idx, scene_pt)
            return
        if (self._mode == self.MODE_MARKUP and self._markup_tool == 'polyline'
                and event.button() == Qt.MouseButton.LeftButton):
            self._finish_polyline_markup()
            return
        super().mouseDoubleClickEvent(event)

    # ── Masks ─────────────────────────────────────────────────────
    def _finish_mask_polygon(self):
        """Close the in-progress polygon (needs >= 3 points) into a new mask,
        then stop drawing — a fresh click on Draw Mask starts a separate one."""
        if self._pair is not None and len(self._mask_points) >= 3:
            self._pair.masks.append({
                'points': [list(p) for p in self._mask_points],
                'visible': True,
                'name': f'Mask {len(self._pair.masks) + 1}',
            })
            if self._mask_item:
                self._mask_item.set_masks(self._pair.masks)
            self.masks_changed.emit()
        self.set_mode(self.MODE_VIEW)

    def mask_cancel_pending(self):
        self._mask_points = []
        if self._mask_item:
            self._mask_item.set_pending_points([])

    def mask_remove_at(self, index: int):
        if not (self._pair and 0 <= index < len(self._pair.masks)):
            return
        if self._mask_edit_index == index:
            self.set_mode(self.MODE_VIEW)   # was being edited — stop first
        elif self._mask_edit_index is not None and index < self._mask_edit_index:
            self._mask_edit_index -= 1      # keep pointing at the same mask
        del self._pair.masks[index]
        if self._mask_item:
            self._mask_item.set_masks(self._pair.masks)
        self.masks_changed.emit()

    def mask_duplicate(self, index: int):
        """Insert a copy of one mask right after it, nudged slightly so the
        two don't sit exactly on top of each other."""
        if not (self._pair and 0 <= index < len(self._pair.masks)):
            return
        src = self._pair.masks[index]
        nudge = 0.02
        copy = {
            'points': [[min(1.0, max(0.0, p[0] + nudge)),
                       min(1.0, max(0.0, p[1] + nudge))] for p in src.get('points', [])],
            'visible': src.get('visible', True),
            'color': src.get('color'),
            'name': f"{src.get('name') or f'Mask {index + 1}'} copy",
        }
        self._pair.masks.insert(index + 1, copy)
        if self._mask_edit_index is not None and index < self._mask_edit_index:
            self._mask_edit_index += 1   # keep pointing at the same mask
        if self._mask_item:
            self._mask_item.set_masks(self._pair.masks)
        self.masks_changed.emit()

    def mask_clear(self):
        if self._pair and self._pair.masks:
            self._pair.masks.clear()
            self.set_mode(self.MODE_VIEW)   # drop any in-progress draw/edit
            if self._mask_item:
                self._mask_item.set_masks(self._pair.masks)
            self.masks_changed.emit()

    def masks_updated(self):
        """Refresh the mask overlay after directly mutating pair.masks in
        place (a visibility toggle or rename from the Masks list)."""
        if self._mask_item and self._pair:
            self._mask_item.set_masks(self._pair.masks)

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
        self.canvas.masks_changed.connect(self._on_canvas_masks_changed)
        self.canvas.mode_changed.connect(self._on_canvas_mode_changed)
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

        # ── Right: tools panel (collapsible, drag-resizable) ──────
        right_panel = QWidget()
        right_panel.setStyleSheet("background: #161616;")
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(right_panel)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setStyleSheet("border: none;")
        self.right_scroll.setMinimumWidth(220)
        self.right_scroll.setMaximumWidth(600)

        # A splitter between the canvas and the tools pane lets the user drag
        # the boundary to widen it (text was getting clipped at a fixed width).
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(6)
        self.main_splitter.setStyleSheet(
            "QSplitter::handle { background: #222; } "
            "QSplitter::handle:hover { background: #3a5d82; }")
        self.main_splitter.addWidget(center)
        self.main_splitter.addWidget(self.right_scroll)
        self.main_splitter.setStretchFactor(0, 1)   # canvas absorbs extra space
        self.main_splitter.setStretchFactor(1, 0)
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setCollapsible(1, False)
        self.main_splitter.setSizes([800, 300])
        root.addWidget(self.main_splitter, 1)

        # Thin strip shown when the right panel is collapsed.
        self.right_bar = self._make_collapsed_bar("‹", "Show tools",
                                                   lambda: self._set_right_collapsed(False))
        root.addWidget(self.right_bar)
        self.right_bar.setVisible(False)

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
                            ('b', 'Set B only'),
                            ('mask', 'Masked Overlay')]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(self._toggle_btn_style())
            btn.clicked.connect(lambda checked, k=key: self._set_view(k))
            self.view_btns[key] = btn
            view_section.addWidget(btn)
        self.view_btns['composite'].setChecked(True)
        right_layout.addWidget(view_section)

        # Masks section — only relevant to (and only shown during) the
        # "Masked Overlay" view, so it stays out of the way otherwise.
        self.mask_section = CollapsibleSection("Mask (Windowed Overlay)", collapsed=False)
        self.mask_section.addWidget(QLabel(
            "Base shows outside the mask; inside it shows either the full "
            "overlay or (with Cutout) just the other drawing.",
            styleSheet="color:#666; font-size:9px;", wordWrap=True))

        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Base:"))
        self.mask_base_btns = {}
        for key, label in [('a', 'Set A'), ('b', 'Set B')]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet(self._toggle_btn_style())
            b.clicked.connect(lambda _, k=key: self._set_mask_base(k))
            self.mask_base_btns[key] = b
            base_row.addWidget(b)
        self.mask_base_btns['a'].setChecked(True)
        self.mask_section.addLayout(base_row)

        self.mask_cutout_chk = QCheckBox(
            "Cutout: reveal only the other drawing inside the mask (no overlay)")
        self.mask_cutout_chk.setStyleSheet("color:#ccc;")
        self.mask_cutout_chk.toggled.connect(self._on_mask_cutout_toggled)
        self.mask_section.addWidget(self.mask_cutout_chk)

        self.draw_mask_btn = QPushButton("✛ Draw Mask (click points, double-click/Enter to close)")
        self.draw_mask_btn.setCheckable(True)
        self.draw_mask_btn.setStyleSheet(self._toggle_btn_style())
        self.draw_mask_btn.clicked.connect(self._toggle_mask_draw)
        self.mask_section.addWidget(self.draw_mask_btn)

        self.mask_section.addWidget(QLabel(
            "Masks (check to show/hide, type to rename, ✎ to reshape):",
            wordWrap=True, styleSheet="color:#888; font-size:9px;"))
        self.mask_list = QListWidget()
        self.mask_list.setStyleSheet("background: #1e1e1e; color: #ddd; border: 1px solid #444;")
        self.mask_list.setFixedHeight(130)
        self.mask_section.addWidget(self.mask_list)

        clear_masks_btn = QPushButton("Clear All Masks")
        clear_masks_btn.setStyleSheet("background:#5e2a2a; color:white; border:none; padding:4px; border-radius:3px;")
        clear_masks_btn.clicked.connect(self.canvas.mask_clear)
        self.mask_section.addWidget(clear_masks_btn)

        self.mask_section.addWidget(QLabel(
            "Draw: click to place points; double-click or Enter closes the shape "
            "and stops (click Draw Mask again for a separate one); Esc cancels; "
            "Backspace removes the last point. Edit (✎): drag a point to reshape, "
            "double-click a line to add a point there, drag inside to move the "
            "whole mask, Delete removes the selected point, Esc/Enter finishes.",
            styleSheet="color:#666; font-size:9px;", wordWrap=True))
        right_layout.addWidget(self.mask_section)
        self.mask_section.setVisible(False)   # only relevant in the mask view

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
        for key, label in [('line', '╱ Line'), ('polyline', '〰 Polyline'),
                           ('rect', '▭ Box'), ('cloud', '☁ Cloud')]:
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
            "Line/Box/Cloud: drag on the drawing. Polyline: click to add each "
            "point, double-click or Enter to finish (doesn't need to close); "
            "Esc cancels, Backspace undoes the last point. Select: click a "
            "markup to move it; Delete removes it. Pan with right-drag.",
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
        QShortcut(QKeySequence("4"), self, lambda: self._set_view('mask'))
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

        # Sync the mask base selector, cutout toggle and masks list to this pair.
        self.mask_base_btns['a'].setChecked(pair.mask_base != 'b')
        self.mask_base_btns['b'].setChecked(pair.mask_base == 'b')
        self.mask_cutout_chk.blockSignals(True)
        self.mask_cutout_chk.setChecked(pair.mask_cutout)
        self.mask_cutout_chk.blockSignals(False)
        self._refresh_mask_list()

    # ── Masks list (show/hide, rename, edit-shape and delete each mask) ──
    def _refresh_mask_list(self):
        pair = self._current_pair()
        editing = self.canvas._mask_edit_index
        self.mask_list.clear()
        for i, m in enumerate(pair.masks):
            item = QListWidgetItem()
            self.mask_list.addItem(item)

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(4)

            chk = QCheckBox()
            chk.setChecked(m.get('visible', True))
            chk.toggled.connect(lambda checked, idx=i: self._on_mask_visible_toggled(idx, checked))
            row_layout.addWidget(chk)

            name_edit = QLineEdit(m.get('name') or f'Mask {i + 1}')
            name_edit.setStyleSheet("background:#2a2a2a; color:#ddd; border:1px solid #444; padding:2px;")
            name_edit.editingFinished.connect(
                lambda idx=i, w=name_edit: self._on_mask_renamed(idx, w.text()))
            row_layout.addWidget(name_edit, 1)

            default_color = (self.overlay_set.color_b if pair.mask_base == 'a'
                             else self.overlay_set.color_a)
            color_btn = QPushButton()
            color_btn.setFixedSize(20, 22)
            color_btn.setToolTip(
                "Cutout reveal color for this mask (only visible with Cutout on). "
                "Right-click to reset to the default color.")
            color_btn.setStyleSheet(
                f"background:{m.get('color') or default_color}; border:1px solid #777; border-radius:3px;")
            color_btn.clicked.connect(lambda _, idx=i: self._pick_mask_color(idx))
            color_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            color_btn.customContextMenuRequested.connect(
                lambda _, idx=i: self._reset_mask_color(idx))
            row_layout.addWidget(color_btn)

            edit_btn = QPushButton("✎")
            edit_btn.setToolTip("Edit shape")
            edit_btn.setFixedSize(24, 22)
            edit_btn.setCheckable(True)
            edit_btn.setChecked(editing == i)
            edit_btn.setStyleSheet(self._toggle_btn_style())
            edit_btn.clicked.connect(lambda _, idx=i: self._toggle_mask_edit(idx))
            row_layout.addWidget(edit_btn)

            dup_btn = QPushButton("⧉")
            dup_btn.setToolTip("Duplicate this mask")
            dup_btn.setFixedSize(24, 22)
            dup_btn.setStyleSheet("background:#3a3a3a; color:white; border:none; border-radius:3px;")
            dup_btn.clicked.connect(lambda _, idx=i: self.canvas.mask_duplicate(idx))
            row_layout.addWidget(dup_btn)

            del_btn = QPushButton("🗑")
            del_btn.setToolTip("Delete this mask")
            del_btn.setFixedSize(24, 22)
            del_btn.setStyleSheet("background:#3a3a3a; color:white; border:none; border-radius:3px;")
            del_btn.clicked.connect(lambda _, idx=i: self.canvas.mask_remove_at(idx))
            row_layout.addWidget(del_btn)

            item.setSizeHint(row.sizeHint())
            self.mask_list.setItemWidget(item, row)

    def _on_mask_visible_toggled(self, index: int, checked: bool):
        pair = self._current_pair()
        if 0 <= index < len(pair.masks):
            pair.masks[index]['visible'] = checked
            self.canvas.masks_updated()

    def _on_mask_renamed(self, index: int, text: str):
        pair = self._current_pair()
        text = text.strip()
        if 0 <= index < len(pair.masks) and text:
            pair.masks[index]['name'] = text

    def _pick_mask_color(self, index: int):
        pair = self._current_pair()
        if not (0 <= index < len(pair.masks)):
            return
        default_color = (self.overlay_set.color_b if pair.mask_base == 'a'
                         else self.overlay_set.color_a)
        current = pair.masks[index].get('color') or default_color
        c = QColorDialog.getColor(QColor(current), self, "Mask Cutout Color")
        if c.isValid():
            pair.masks[index]['color'] = c.name()
            self.canvas.masks_updated()
            self._refresh_mask_list()

    def _reset_mask_color(self, index: int):
        pair = self._current_pair()
        if 0 <= index < len(pair.masks) and pair.masks[index].get('color'):
            pair.masks[index]['color'] = None
            self.canvas.masks_updated()
            self._refresh_mask_list()

    def _toggle_mask_edit(self, index: int):
        if self.canvas._mask_edit_index == index:
            self.canvas.stop_mask_edit()
        else:
            self.canvas.start_mask_edit(index)
        # mode_changed (emitted by set_mode above) already resyncs the UI.

    def _on_canvas_masks_changed(self):
        self._sync_mask_ui()

    def _on_canvas_mode_changed(self, mode: int):
        self._sync_mask_ui()

    def _sync_mask_ui(self):
        """Single place that keeps the Masks panel in sync with the canvas:
        the Draw Mask toggle, the panel's own visibility (stays up while
        actively drawing/editing even off the Masked Overlay view), and the
        per-mask list (names/order/edit-highlight)."""
        self.draw_mask_btn.setChecked(self.canvas._mode == OverlayCanvas.MODE_MASK)
        self._sync_mask_section_visibility()
        self._refresh_mask_list()

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
        self._sync_mask_section_visibility()

    def _sync_mask_section_visibility(self):
        """Keep the Masks panel reachable while actively drawing/editing a
        mask even if you switch to another view to reference a drawing —
        only hide it once nothing is active and you're not in the Masked
        Overlay view."""
        drawing = self.canvas._mode in (OverlayCanvas.MODE_MASK, OverlayCanvas.MODE_MASK_EDIT)
        self.mask_section.setVisible(self._current_view_mode() == 'mask' or drawing)

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
        # Leaving markup/mask mode — clear their tool buttons.
        # (canvas.set_mode above emits mode_changed, which resyncs the Masks
        # panel: Draw Mask toggle, panel visibility, edit-highlight.)
        for b in self.markup_btns.values():
            b.setChecked(False)

    def _set_markup_tool(self, tool: str):
        self.canvas.set_markup_tool(tool)
        self.canvas.set_mode(OverlayCanvas.MODE_MARKUP)
        for k, b in self.markup_btns.items():
            b.setChecked(k == tool)
        # Markup mode is exclusive with the align tool.
        self.move_btn.setChecked(False)
        self.rotate_btn.setChecked(False)

    def _toggle_mask_draw(self):
        if self.draw_mask_btn.isChecked():
            self.canvas.set_mode(OverlayCanvas.MODE_MASK)
            # Mask-drawing is exclusive with the align and markup tools.
            self.move_btn.setChecked(False)
            self.rotate_btn.setChecked(False)
            for b in self.markup_btns.values():
                b.setChecked(False)
        else:
            self.canvas.set_mode(OverlayCanvas.MODE_VIEW)

    def _set_mask_base(self, which: str):
        pair = self._current_pair()
        pair.mask_base = which
        self.mask_base_btns['a'].setChecked(which == 'a')
        self.mask_base_btns['b'].setChecked(which == 'b')
        self.canvas.refresh_mask_view()

    def _on_mask_cutout_toggled(self, checked: bool):
        self._current_pair().mask_cutout = checked
        self.canvas.refresh_mask_view()

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

    def _current_view_mode(self) -> str:
        for k, btn in self.view_btns.items():
            if btn.isChecked():
                return k
        return 'composite'

    def _export(self, fmt: str):
        pair = self._current_pair()
        view_mode = self._current_view_mode()
        name_part = {
            'a': self.overlay_set.set_a_label,
            'b': self.overlay_set.set_b_label,
            'mask': 'masked_overlay',
        }.get(view_mode, 'overlay')
        default_name = f"{name_part}_{pair.page_a.sheet_number or 'sheet'}.{fmt}".replace(' ', '_')
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {fmt.upper()}",
            os.path.join(self.settings.get('export_path', ''), default_name),
            f"{fmt.upper()} Files (*.{fmt})"
        )
        if not path:
            return

        try:
            # Re-render at export quality (independent of the on-screen DPI).
            # Export exactly what's currently shown: solo A/B for those views,
            # the full overlay only for the composite/mask views.
            dpi = getattr(self.overlay_set, 'export_dpi', None) or self.overlay_set.render_dpi
            img_a = R.render_page(pair.page_a.pdf_path, pair.page_a.page_index, dpi)
            img_b = None
            if view_mode != 'a':
                img_b_raw = R.render_page(pair.page_b.pdf_path, pair.page_b.page_index, dpi)
                img_b = R.apply_transform(img_b_raw, pair.offset_x, pair.offset_y,
                                           pair.rotation, pair.pivot_x, pair.pivot_y,
                                           pair.scale_factor, img_a.size)

            if view_mode == 'a':
                content = R.render_single_colored(img_a, self.overlay_set.color_a)
            elif view_mode == 'b':
                content = R.render_single_colored(img_b, self.overlay_set.color_b)
            elif view_mode == 'mask':
                base_color = (self.overlay_set.color_a if pair.mask_base == 'a'
                              else self.overlay_set.color_b)
                base_src = img_a if pair.mask_base == 'a' else img_b
                base_solo = R.render_single_colored(base_src, base_color)
                if pair.mask_cutout:
                    # Cutout: the hole reveals the OTHER drawing alone, not
                    # the two blended together. Each mask can override the
                    # color it's revealed in; render each distinct color
                    # once (not per mask) and composite mask-by-mask.
                    default_other_color = (self.overlay_set.color_b if pair.mask_base == 'a'
                                           else self.overlay_set.color_a)
                    other_src = img_b if pair.mask_base == 'a' else img_a
                    colors_needed = {None: default_other_color}
                    for m in pair.masks:
                        if m.get('visible', True) and m.get('color'):
                            colors_needed[m['color']] = m['color']
                    other_by_color = {key: R.render_single_colored(other_src, color)
                                      for key, color in colors_needed.items()}
                    content = R.composite_masked_cutout(base_solo, other_by_color, pair.masks,
                                                         img_a.width, img_a.height)
                else:
                    inside = R.composite_overlay(img_a, img_b,
                                                  self.overlay_set.color_a,
                                                  self.overlay_set.color_b,
                                                  shared_color=self.overlay_set.shared_color)
                    content = R.composite_masked(inside, base_solo, pair.masks,
                                                  img_a.width, img_a.height)
            else:
                content = R.composite_overlay(img_a, img_b,
                                               self.overlay_set.color_a,
                                               self.overlay_set.color_b,
                                               shared_color=self.overlay_set.shared_color)

            # White background for export
            bg = Image.new("RGBA", content.size, (255, 255, 255, 255))
            bg.paste(content, mask=content)
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
