"""Empty workspace for stitching many drawings on a blank canvas."""
import os
import math
import uuid
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import (
    QBrush, QColor, QPainter, QPen, QPixmap, QCursor, QImage
)
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel, QListWidget,
    QListWidgetItem, QFileDialog, QColorDialog, QDoubleSpinBox, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsItem, QMessageBox,
    QScrollArea, QGraphicsRectItem, QComboBox, QMenu, QInputDialog
)
from PIL import Image
from core.models import (
    DrawingPage, WorkspaceDrawing, OverlaySet, COMMON_SCALES,
    compute_scale_factor,
)
from core import renderer as R
from ui.collapsible import CollapsibleSection
from ui.landing import OpenPdfPickerDialog


class WorkspacePixmapItem(QGraphicsPixmapItem):
    """Selectable workspace drawing with per-drawing transparent erase masks."""
    def __init__(self, drawing: WorkspaceDrawing, pixmap: QPixmap):
        super().__init__()
        self.drawing = drawing
        self._base_pixmap = pixmap
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setTransformOriginPoint(pixmap.width() / 2, pixmap.height() / 2)
        self.rebuild_pixmap()
        self.sync_from_model()

    def set_base_pixmap(self, pixmap: QPixmap):
        """Replace the unerased source pixmap, then reapply saved erase masks."""
        self._base_pixmap = pixmap
        self.setTransformOriginPoint(pixmap.width() / 2, pixmap.height() / 2)
        self.rebuild_pixmap()

    def rebuild_pixmap(self):
        """Bake erase masks into this drawing pixmap by clearing alpha.

        The stored base pixmap remains intact, so undoing an erase or changing
        color can rebuild the visible image from the original drawing pixels.
        """
        image = self._base_pixmap.toImage().convertToFormat(
            QImage.Format.Format_ARGB32_Premultiplied)
        mask_painter = QPainter(image)
        mask_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        mask_painter.setPen(Qt.PenStyle.NoPen)
        mask_painter.setBrush(Qt.BrushStyle.SolidPattern)
        for r in self.drawing.erase_rects:
            mask_painter.drawRect(QRectF(
                r[0] * image.width(), r[1] * image.height(),
                r[2] * image.width(), r[3] * image.height()))
        mask_painter.end()
        self.setPixmap(QPixmap.fromImage(image))

    def sync_from_model(self):
        self.setPos(self.drawing.offset_x, self.drawing.offset_y)
        self.setRotation(self.drawing.rotation)
        self.setScale(self.drawing.scale_factor)
        self.setVisible(self.drawing.visible)

    def paint(self, painter: QPainter, option, widget=None):
        painter.drawPixmap(0, 0, self.pixmap())
        if self.isSelected():
            pen = QPen(QColor('#00e0ff'))
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.boundingRect())


class WorkspaceCanvas(QGraphicsView):
    selection_changed = pyqtSignal(object)
    drawing_changed = pyqtSignal()
    mode_changed = pyqtSignal(str)

    MODE_VIEW = 'view'
    MODE_MOVE = 'move'
    MODE_ROTATE = 'rotate'
    MODE_ERASE = 'erase'

    # Movement (in view pixels) beyond which a press is a drag, not a click.
    CLICK_SLOP = 4

    def __init__(self):
        super().__init__()
        self.gscene = QGraphicsScene(self)
        self.setScene(self.gscene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._mode = self.MODE_VIEW
        self._bg = 'white'
        self._items_by_id = {}
        self._drag_start = None
        self._rotate_start = None
        self._erase_start = None
        self._erase_preview = None
        self._manip_item = None       # item currently being moved/rotated
        self._press_view = None       # press position (view coords) for click test
        self._press_scene = None      # press position (scene coords) for selection
        self._panning = False
        self._pan_start = QPointF()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.gscene.selectionChanged.connect(self._on_scene_selection)
        self.set_canvas_background('white')

    # ── Background ────────────────────────────────────────────────
    def set_canvas_background(self, bg: str):
        self._bg = bg
        color = '#ffffff' if bg != 'dark' else '#0d0d0d'
        self.setStyleSheet(f'background:{color}; border:none;')
        self.gscene.setBackgroundBrush(QBrush(QColor(color)))
        for item in self._items_by_id.values():
            item.drawing.erase_bg = 'white' if bg != 'dark' else 'dark'
            item.update()

    def background(self) -> str:
        return self._bg

    def set_mode(self, mode: str):
        self._mode = mode
        self._clear_erase_preview()
        if mode == self.MODE_MOVE:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        elif mode == self.MODE_ROTATE:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        elif mode == self.MODE_ERASE:
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        else:
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self.mode_changed.emit(mode)

    # ── Drawing management ────────────────────────────────────────
    def add_drawing(self, drawing: WorkspaceDrawing, pixmap: QPixmap, fit: bool = True):
        drawing.erase_bg = 'white' if self._bg != 'dark' else 'dark'
        item = WorkspacePixmapItem(drawing, pixmap)
        item.setZValue(self._next_z())
        self.gscene.addItem(item)
        self._items_by_id[drawing.drawing_id] = item
        self.gscene.clearSelection()
        item.setSelected(True)
        if fit:
            self.fit_view()

    def remove_drawing(self, drawing_id: str):
        item = self._items_by_id.pop(drawing_id, None)
        if item is not None:
            if item is self._manip_item:
                self._manip_item = None
            self.gscene.removeItem(item)
            self.drawing_changed.emit()

    def set_drawing_visible(self, drawing_id: str, visible: bool):
        item = self._items_by_id.get(drawing_id)
        if item is not None:
            item.drawing.visible = visible
            item.setVisible(visible)

    def _next_z(self) -> float:
        zs = [it.zValue() for it in self._items_by_id.values()]
        return (max(zs) + 1.0) if zs else 0.0

    def bring_to_front(self, drawing_id: str):
        item = self._items_by_id.get(drawing_id)
        if item is not None:
            item.setZValue(self._next_z())
            self.drawing_changed.emit()

    def send_to_back(self, drawing_id: str):
        item = self._items_by_id.get(drawing_id)
        if item is not None:
            zs = [it.zValue() for it in self._items_by_id.values()]
            item.setZValue((min(zs) - 1.0) if zs else 0.0)
            self.drawing_changed.emit()

    def select_drawing(self, drawing_id: str, center: bool = True):
        self.gscene.clearSelection()
        item = self._items_by_id.get(drawing_id)
        if item:
            item.setSelected(True)
            if center:
                self.centerOn(item)

    def selected_item(self):
        items = [i for i in self.gscene.selectedItems() if isinstance(i, WorkspacePixmapItem)]
        return items[-1] if items else None

    def _items_at(self, scene_pos):
        """Visible drawings whose extent covers a scene point, topmost first.

        Uses each drawing's bounding box (not its ink mask) so clicking anywhere
        on a sheet selects it — a full drawing is mostly white space, and asking
        the user to click exactly on a line would be far too fiddly. Overlap is
        therefore genuine overlap of the drawing rectangles.
        """
        hits = [i for i in self._items_by_id.values()
                if i.isVisible() and i.sceneBoundingRect().contains(scene_pos)]
        hits.sort(key=lambda it: it.zValue(), reverse=True)
        return hits

    def _on_scene_selection(self):
        item = self.selected_item()
        self.selection_changed.emit(item.drawing if item else None)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.set_mode(self.MODE_VIEW)
            self._drag_start = None
            self._rotate_start = None
            self._erase_start = None
            self._manip_item = None
            self._panning = False
            self._clear_erase_preview()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            item = self.selected_item()
            if item is not None:
                self.remove_drawing(item.drawing.drawing_id)
                event.accept()
                return
        super().keyPressEvent(event)

    def _clear_erase_preview(self):
        if self._erase_preview is not None:
            scene = self._erase_preview.scene()
            if scene is not None:
                scene.removeItem(self._erase_preview)
            self._erase_preview = None

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    # ── Mouse handling ────────────────────────────────────────────
    # Selection is decoupled from the move/rotate/erase tools so the controls
    # no longer fight each other:
    #   • A plain left click SELECTS (with an overlap chooser when several
    #     drawings sit under the cursor).
    #   • A left drag manipulates the *already selected* drawing (move / rotate
    #     / erase). So the flow is: click to pick a drawing, then drag it.
    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MouseButton.RightButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_view = event.position()
            scene_pos = self.mapToScene(event.position().toPoint())
            self._press_scene = scene_pos
            selected = self.selected_item()
            topmost = self._items_at(scene_pos)
            top = topmost[0] if topmost else None

            if self._mode == self.MODE_ERASE and selected is not None:
                self._begin_erase(selected, scene_pos)
                return
            # Begin a move/rotate drag only when pressing on the drawing that is
            # already selected — otherwise the press is treated as a selection
            # click (resolved on release).
            if self._mode == self.MODE_MOVE and selected is not None and top is selected:
                self._manip_item = selected
                self._drag_start = scene_pos
                return
            if self._mode == self.MODE_ROTATE and selected is not None and top is selected:
                self._manip_item = selected
                self._rotate_start = scene_pos
                return
            # Deferred selection — handled on release.
            return
        super().mousePressEvent(event)

    def _begin_erase(self, item, scene_pos):
        self._erase_start = item.mapFromScene(scene_pos)
        self._manip_item = item
        self._clear_erase_preview()
        self._erase_preview = QGraphicsRectItem(item)
        pen = QPen(QColor('#00e0ff'))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self._erase_preview.setPen(pen)
        self._erase_preview.setBrush(QBrush(QColor(0, 224, 255, 35)))
        self._erase_preview.setZValue(9999)
        self._erase_preview.setRect(QRectF(self._erase_start, self._erase_start))

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - delta.y()))
            return
        item = self._manip_item
        if item and self._drag_start is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            delta = scene_pos - self._drag_start
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                delta = QPointF(delta.x() * 0.1, delta.y() * 0.1)
            self._drag_start = scene_pos
            item.setPos(item.pos() + delta)
            item.drawing.offset_x = item.pos().x()
            item.drawing.offset_y = item.pos().y()
            self.drawing_changed.emit()
            return
        if item and self._rotate_start is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            center = item.mapToScene(item.boundingRect().center())
            prev = self._rotate_start - center
            cur = scene_pos - center
            delta = math.degrees(math.atan2(cur.y(), cur.x()) - math.atan2(prev.y(), prev.x()))
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                delta *= 0.1
            item.drawing.rotation = (item.drawing.rotation + delta) % 360
            item.setRotation(item.drawing.rotation)
            self._rotate_start = scene_pos
            self.drawing_changed.emit()
            return
        if item and self._erase_start is not None and self._erase_preview is not None:
            end = item.mapFromScene(self.mapToScene(event.position().toPoint()))
            x0, x1 = sorted([self._erase_start.x(), end.x()])
            y0, y1 = sorted([self._erase_start.y(), end.y()])
            self._erase_preview.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning and event.button() == Qt.MouseButton.RightButton:
            self._panning = False
            self.set_mode(self._mode)
            return
        if self._erase_start is not None and event.button() == Qt.MouseButton.LeftButton:
            item = self._manip_item
            end = item.mapFromScene(self.mapToScene(event.position().toPoint())) if item else QPointF()
            if item:
                x0, x1 = sorted([self._erase_start.x(), end.x()])
                y0, y1 = sorted([self._erase_start.y(), end.y()])
                if x1 - x0 > 4 and y1 - y0 > 4:
                    w, h = item.pixmap().width(), item.pixmap().height()
                    item.drawing.erase_rects.append([x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h])
                    item.rebuild_pixmap()
                    self.drawing_changed.emit()
            self._erase_start = None
            self._manip_item = None
            self._clear_erase_preview()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            manip = self._drag_start is not None or self._rotate_start is not None
            self._drag_start = None
            self._rotate_start = None
            self._manip_item = None
            if not manip:
                moved = (self._press_view is not None and
                         (event.position() - self._press_view).manhattanLength() > self.CLICK_SLOP)
                if not moved and self._press_scene is not None:
                    self._select_at(self._press_scene)
            return
        super().mouseReleaseEvent(event)

    def _select_at(self, scene_pos):
        """Resolve a selection click: pick the single drawing under the cursor,
        show a chooser when several overlap, or clear selection on empty space."""
        items = self._items_at(scene_pos)
        if not items:
            self.gscene.clearSelection()
            return
        if len(items) == 1:
            self.select_drawing(items[0].drawing.drawing_id, center=False)
            return
        # Overlapping drawings — let the user pick by name.
        menu = QMenu(self)
        title = menu.addAction('Select drawing:')
        title.setEnabled(False)
        menu.addSeparator()
        for it in items:
            act = menu.addAction(it.drawing.name)
            act.setData(it.drawing.drawing_id)
        chosen = menu.exec(QCursor.pos())
        if chosen is not None and chosen.data():
            self.select_drawing(chosen.data(), center=False)

    # ── Export ────────────────────────────────────────────────────
    def render_visible(self, scale: float = 1.0, margin: int = 12) -> QImage:
        """Render every visible drawing to a QImage on an opaque background.

        The selection outline and any erase preview are hidden first, so the
        export shows only the drawings themselves.
        """
        prev_selection = list(self.gscene.selectedItems())
        self.gscene.clearSelection()
        self._clear_erase_preview()

        items = [it for it in self._items_by_id.values() if it.isVisible()]
        rect = QRectF()
        for it in items:
            rect = it.sceneBoundingRect() if rect.isNull() else rect.united(it.sceneBoundingRect())
        if rect.isNull():
            rect = self.gscene.itemsBoundingRect()
        m = margin / max(scale, 0.01)
        rect = rect.adjusted(-m, -m, m, m)

        w = max(1, int(round(rect.width() * scale)))
        h = max(1, int(round(rect.height() * scale)))
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        bg = QColor('#ffffff' if self._bg != 'dark' else '#0d0d0d')
        img.fill(bg)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.gscene.render(painter, QRectF(0, 0, w, h), rect)
        painter.end()

        for it in prev_selection:
            it.setSelected(True)
        return img

    def fit_view(self):
        rect = self.gscene.itemsBoundingRect()
        if not rect.isNull():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)


class EmptyWorkspace(QWidget):
    save_project = pyqtSignal(object)

    def __init__(self, overlay_set: OverlaySet, settings: dict, parent=None):
        super().__init__(parent)
        self.overlay_set = overlay_set
        self.settings = settings
        self._button_styles = []
        self._syncing_list = False
        self._build_ui()
        self.canvas.set_canvas_background(overlay_set.canvas_bg)
        for d in overlay_set.workspace_drawings:
            self._render_and_add(d, fit=False)
        self.canvas.fit_view()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.left_panel = QWidget()
        self.left_panel.setFixedWidth(220)
        self.left_panel.setStyleSheet('background:#161616;')
        left_layout = QVBoxLayout(self.left_panel)
        header = QHBoxLayout()
        header.addWidget(QLabel('Workspace Drawings'))
        header.addStretch()
        collapse_left = QPushButton('‹')
        collapse_left.setFixedSize(22, 22)
        collapse_left.setStyleSheet(self._collapse_btn_style())
        collapse_left.clicked.connect(lambda: self._set_left_collapsed(True))
        header.addWidget(collapse_left)
        left_layout.addLayout(header)
        left_layout.addWidget(QLabel('Check to show/hide · double-click to rename',
                                     wordWrap=True, styleSheet='color:#777; font-size:9px;'))
        self.list = QListWidget()
        self.list.setStyleSheet('background:#1e1e1e; color:#ddd; border:1px solid #444;')
        self.list.currentItemChanged.connect(self._select_from_list)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_context_menu)
        left_layout.addWidget(self.list)

        list_btns = QHBoxLayout()
        show_all = QPushButton('Show all')
        show_all.setStyleSheet(self._small_action_style())
        show_all.clicked.connect(lambda: self._set_all_visible(True))
        hide_all = QPushButton('Hide all')
        hide_all.setStyleSheet(self._small_action_style())
        hide_all.clicked.connect(lambda: self._set_all_visible(False))
        remove_btn = QPushButton('🗑 Remove')
        remove_btn.setStyleSheet(self._small_action_style())
        remove_btn.clicked.connect(self._remove_selected)
        list_btns.addWidget(show_all)
        list_btns.addWidget(hide_all)
        list_btns.addWidget(remove_btn)
        left_layout.addLayout(list_btns)
        root.addWidget(self.left_panel)
        self.left_bar = self._make_collapsed_bar('›', 'Show drawings', lambda: self._set_left_collapsed(False))
        root.addWidget(self.left_bar)
        self.left_bar.setVisible(False)

        self.canvas = WorkspaceCanvas()
        self.canvas.selection_changed.connect(self._sync_selection)
        self.canvas.mode_changed.connect(self._sync_mode_buttons)
        self.canvas.drawing_changed.connect(self._on_drawing_changed)
        root.addWidget(self.canvas, 1)

        self.right_bar = self._make_collapsed_bar('‹', 'Show tools', lambda: self._set_right_collapsed(False))
        root.addWidget(self.right_bar)
        self.right_bar.setVisible(False)

        right_panel = QWidget()
        right_panel.setStyleSheet('background:#161616;')
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(right_panel)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setStyleSheet('border:none;')
        self.right_scroll.setFixedWidth(282)
        root.addWidget(self.right_scroll)
        right_layout = QVBoxLayout(right_panel)

        right_header = QHBoxLayout()
        right_header.addWidget(QLabel('Workspace Tools'))
        right_header.addStretch()
        collapse_right = QPushButton('›')
        collapse_right.setFixedSize(22, 22)
        collapse_right.setStyleSheet(self._collapse_btn_style())
        collapse_right.clicked.connect(lambda: self._set_right_collapsed(True))
        right_header.addWidget(collapse_right)
        right_layout.addLayout(right_header)

        import_section = CollapsibleSection('Import Drawings', collapsed=False)
        file_btn = QPushButton('Import PDF/Image File')
        file_btn.setStyleSheet(self._action_btn_style())
        file_btn.clicked.connect(self._import_file)
        import_section.addWidget(file_btn)
        open_btn = QPushButton('Import from Open PDF')
        open_btn.setStyleSheet(self._action_btn_style())
        open_btn.clicked.connect(self._import_open_pdf)
        import_section.addWidget(open_btn)
        right_layout.addWidget(import_section)

        align_section = CollapsibleSection('Adjust Selected Drawing', collapsed=False)
        align_section.addWidget(QLabel('Click a drawing to select it, then drag to move.',
                                       wordWrap=True, styleSheet='color:#777; font-size:9px;'))
        self.move_btn = QPushButton('↕  Move (click & drag)')
        self.move_btn.setCheckable(True)
        self.move_btn.setStyleSheet(self._toggle_btn_style())
        self.move_btn.clicked.connect(lambda: self._set_mode('move'))
        align_section.addWidget(self.move_btn)
        nudge_row = QHBoxLayout()
        for label, dx, dy in [('←', -1, 0), ('→', 1, 0), ('↑', 0, -1), ('↓', 0, 1)]:
            b = QPushButton(label)
            b.setFixedSize(32, 28)
            b.setStyleSheet(self._small_btn_style())
            b.clicked.connect(lambda _, x=dx, y=dy: self._nudge(x, y))
            nudge_row.addWidget(b)
        align_section.addLayout(nudge_row)
        self.rotate_btn = QPushButton('↻  Free Rotate (click & drag)')
        self.rotate_btn.setCheckable(True)
        self.rotate_btn.setStyleSheet(self._toggle_btn_style())
        self.rotate_btn.clicked.connect(lambda: self._set_mode('rotate'))
        align_section.addWidget(self.rotate_btn)
        rot_row = QHBoxLayout()
        rot_row.addWidget(QLabel('Angle:'))
        self.rot = QDoubleSpinBox()
        self.rot.setRange(-360, 360)
        self.rot.setDecimals(1)
        self.rot.setSuffix('°')
        self.rot.valueChanged.connect(self._set_rotation)
        rot_row.addWidget(self.rot)
        align_section.addLayout(rot_row)
        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel('Scale:'))
        self.scale = QDoubleSpinBox()
        self.scale.setRange(0.01, 20)
        self.scale.setDecimals(3)
        self.scale.setSingleStep(0.05)
        self.scale.setValue(1)
        self.scale.valueChanged.connect(self._set_scale)
        scale_row.addWidget(self.scale)
        align_section.addLayout(scale_row)
        z_row = QHBoxLayout()
        front_btn = QPushButton('Bring to Front')
        front_btn.setStyleSheet(self._small_action_style())
        front_btn.clicked.connect(self._bring_to_front)
        back_btn = QPushButton('Send to Back')
        back_btn.setStyleSheet(self._small_action_style())
        back_btn.clicked.connect(self._send_to_back)
        z_row.addWidget(front_btn)
        z_row.addWidget(back_btn)
        align_section.addLayout(z_row)
        right_layout.addWidget(align_section)

        # ── Match drawings to a common real-world scale ──
        scale_section = CollapsibleSection('Scale to Drawing Scale', collapsed=True)
        scale_section.addWidget(QLabel(
            'Set a workspace reference scale, then each drawing can be resized '
            'so its own scale matches — like the overlay auto-scale.',
            wordWrap=True, styleSheet='color:#777; font-size:9px;'))
        ref_row = QHBoxLayout()
        ref_row.addWidget(QLabel('Reference:'))
        self.ref_scale_combo = QComboBox()
        self.ref_scale_combo.setEditable(True)
        self.ref_scale_combo.addItem('')
        self.ref_scale_combo.addItems(COMMON_SCALES)
        self.ref_scale_combo.setStyleSheet('background:#2a2a2a; color:#eee;')
        if self.overlay_set.workspace_ref_scale:
            self.ref_scale_combo.setCurrentText(self.overlay_set.workspace_ref_scale)
        self.ref_scale_combo.currentTextChanged.connect(self._on_ref_scale_changed)
        ref_row.addWidget(self.ref_scale_combo)
        scale_section.addLayout(ref_row)
        dr_row = QHBoxLayout()
        dr_row.addWidget(QLabel('This drawing:'))
        self.draw_scale_combo = QComboBox()
        self.draw_scale_combo.setEditable(True)
        self.draw_scale_combo.addItem('')
        self.draw_scale_combo.addItems(COMMON_SCALES)
        self.draw_scale_combo.setStyleSheet('background:#2a2a2a; color:#eee;')
        self.draw_scale_combo.currentTextChanged.connect(self._on_draw_scale_changed)
        dr_row.addWidget(self.draw_scale_combo)
        scale_section.addLayout(dr_row)
        match_sel = QPushButton('Match Selected to Reference')
        match_sel.setStyleSheet(self._action_btn_style())
        match_sel.clicked.connect(lambda: self._match_scale(all_drawings=False))
        scale_section.addWidget(match_sel)
        match_all = QPushButton('Match ALL to Reference')
        match_all.setStyleSheet(self._action_btn_style())
        match_all.clicked.connect(lambda: self._match_scale(all_drawings=True))
        scale_section.addWidget(match_all)
        self.scale_status = QLabel('')
        self.scale_status.setWordWrap(True)
        self.scale_status.setStyleSheet('color:#888; font-size:9px;')
        scale_section.addWidget(self.scale_status)
        right_layout.addWidget(scale_section)

        edit_section = CollapsibleSection('Color / Erase', collapsed=False)
        self.color_btn = QPushButton('Drawing Color')
        self.color_btn.setStyleSheet(self._action_btn_style())
        self.color_btn.clicked.connect(self._pick_color)
        edit_section.addWidget(self.color_btn)
        self.erase_btn = QPushButton('Erase Rectangle (click & drag)')
        self.erase_btn.setCheckable(True)
        self.erase_btn.setStyleSheet(self._toggle_btn_style())
        self.erase_btn.clicked.connect(lambda: self._set_mode('erase'))
        edit_section.addWidget(self.erase_btn)
        undo_erase = QPushButton('Undo Last Erase')
        undo_erase.setStyleSheet(self._action_btn_style())
        undo_erase.clicked.connect(self._undo_erase)
        edit_section.addWidget(undo_erase)
        edit_section.addWidget(QLabel('Select a drawing first. Shift+drag gives fine movement/rotation.', wordWrap=True, styleSheet='color:#777; font-size:9px;'))
        right_layout.addWidget(edit_section)

        export_section = CollapsibleSection('Save', collapsed=True)
        save = QPushButton('💾 Save Project')
        save.setStyleSheet(self._save_btn_style())
        save.clicked.connect(lambda: self.save_project.emit(self.overlay_set))
        export_section.addWidget(save)
        export_pdf = QPushButton('Export Workspace PDF')
        export_pdf.setStyleSheet(self._action_btn_style())
        export_pdf.clicked.connect(lambda: self._export('pdf'))
        export_section.addWidget(export_pdf)
        export_png = QPushButton('Export Workspace PNG')
        export_png.setStyleSheet(self._action_btn_style())
        export_png.clicked.connect(lambda: self._export('png'))
        export_section.addWidget(export_png)
        export_section.addWidget(QLabel('Exports the visible drawings only (hidden ones are skipped).',
                                        wordWrap=True, styleSheet='color:#777; font-size:9px;'))
        right_layout.addWidget(export_section)

        fit = QPushButton('Fit Workspace')
        fit.setStyleSheet(self._action_btn_style())
        fit.clicked.connect(self.canvas.fit_view)
        right_layout.addWidget(fit)
        right_layout.addStretch()
        self._set_mode('move')

    def _make_collapsed_bar(self, arrow: str, tooltip: str, on_click) -> QWidget:
        bar = QWidget()
        bar.setFixedWidth(20)
        bar.setStyleSheet('background:#161616;')
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

    def _set_mode(self, mode: str):
        self.canvas.set_mode(mode)

    def _sync_mode_buttons(self, mode: str):
        self.move_btn.setChecked(mode == 'move')
        self.rotate_btn.setChecked(mode == 'rotate')
        self.erase_btn.setChecked(mode == 'erase')

    # ── Drawings list ─────────────────────────────────────────────
    def _add_to_list(self, d):
        item = QListWidgetItem(d.name)
        item.setData(Qt.ItemDataRole.UserRole, d.drawing_id)
        item.setFlags(item.flags() |
                      Qt.ItemFlag.ItemIsUserCheckable |
                      Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(Qt.CheckState.Checked if d.visible else Qt.CheckState.Unchecked)
        self._syncing_list = True
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self._syncing_list = False

    def _list_item_for(self, drawing_id):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == drawing_id:
                return it
        return None

    def _on_item_changed(self, item):
        """Handle a rename (text edit) or a show/hide checkbox toggle."""
        if self._syncing_list or item is None:
            return
        drawing_id = item.data(Qt.ItemDataRole.UserRole)
        d = self._drawing_by_id(drawing_id)
        if d is None:
            return
        # Visibility
        visible = item.checkState() == Qt.CheckState.Checked
        if visible != d.visible:
            self.canvas.set_drawing_visible(drawing_id, visible)
        # Rename (ignore empty)
        text = item.text().strip()
        if text and text != d.name:
            d.name = text

    def _drawing_by_id(self, drawing_id):
        for d in self.overlay_set.workspace_drawings:
            if d.drawing_id == drawing_id:
                return d
        return None

    def _select_from_list(self, item, old=None):
        if item and not self._syncing_list:
            self.canvas.select_drawing(item.data(Qt.ItemDataRole.UserRole))

    def _list_context_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        drawing_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        rename_act = menu.addAction('Rename…')
        toggle_act = menu.addAction('Hide' if item.checkState() == Qt.CheckState.Checked else 'Show')
        menu.addSeparator()
        front_act = menu.addAction('Bring to Front')
        back_act = menu.addAction('Send to Back')
        menu.addSeparator()
        remove_act = menu.addAction('Remove')
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen is rename_act:
            self.list.editItem(item)
        elif chosen is toggle_act:
            new_state = (Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked
                         else Qt.CheckState.Checked)
            item.setCheckState(new_state)
        elif chosen is front_act:
            self.canvas.bring_to_front(drawing_id)
        elif chosen is back_act:
            self.canvas.send_to_back(drawing_id)
        elif chosen is remove_act:
            self._remove_drawing(drawing_id)

    def _set_all_visible(self, visible: bool):
        state = Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)

    def _remove_selected(self):
        d = self._selected_drawing()
        if d is not None:
            self._remove_drawing(d.drawing_id)

    def _remove_drawing(self, drawing_id):
        d = self._drawing_by_id(drawing_id)
        if d is None:
            return
        if QMessageBox.question(self, 'Remove Drawing',
                                f'Remove "{d.name}" from the workspace?') != QMessageBox.StandardButton.Yes:
            return
        self.canvas.remove_drawing(drawing_id)
        self.overlay_set.workspace_drawings = [
            w for w in self.overlay_set.workspace_drawings if w.drawing_id != drawing_id]
        it = self._list_item_for(drawing_id)
        if it is not None:
            self._syncing_list = True
            self.list.takeItem(self.list.row(it))
            self._syncing_list = False

    def _bring_to_front(self):
        d = self._selected_drawing()
        if d is not None:
            self.canvas.bring_to_front(d.drawing_id)

    def _send_to_back(self):
        d = self._selected_drawing()
        if d is not None:
            self.canvas.send_to_back(d.drawing_id)

    # ── Import ────────────────────────────────────────────────────
    def _render_and_add(self, d, fit=True):
        img = R.render_page(d.page.pdf_path, d.page.page_index, self.overlay_set.render_dpi)
        colored = R.render_single_colored(img, d.color)
        self.canvas.add_drawing(d, R.pil_to_qpixmap(colored), fit=fit)
        self._add_to_list(d)

    def _import_file(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Import drawings', self.settings.get('last_open_dir', ''),
            'Drawings (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)')
        for path in paths:
            self._import_path_with_page_picker(path)

    def _import_path_with_page_picker(self, path: str, pages_text: str = ''):
        if path.lower().endswith('.pdf'):
            try:
                count = R.get_page_count(path)
            except Exception:
                count = 1
            if pages_text:
                from ui.page_selector import parse_page_ranges
                indices = parse_page_ranges(pages_text, count)
            elif count > 1:
                from ui.page_selector import PageSelectorDialog
                dlg = PageSelectorDialog(path, count, self)
                if not dlg.exec():
                    return
                indices = dlg.selected_indices()
            else:
                indices = [0]
            for index in indices:
                self._add_pdf_page(path, index)
            return
        pdf = self._image_file_to_pdf(path)
        if pdf:
            self._add_pdf_page(pdf, 0, os.path.splitext(os.path.basename(path))[0])

    def _add_pdf_page(self, path: str, page_index: int, name: str = ''):
        base = os.path.splitext(os.path.basename(path))[0]
        label = name or (base if page_index == 0 else f'{base} — Page {page_index + 1}')
        page = DrawingPage(path, page_index, display_name=label)
        d = WorkspaceDrawing(page=page, name=label)
        self.overlay_set.workspace_drawings.append(d)
        self._render_and_add(d)

    def _image_file_to_pdf(self, path: str):
        try:
            pdf_dir = os.path.join(os.path.expanduser('~/.drawing_overlay'), 'imported')
            os.makedirs(pdf_dir, exist_ok=True)
            pdf = os.path.join(pdf_dir, f'workspace_img_{uuid.uuid4().hex}.pdf')
            Image.open(path).convert('RGB').save(pdf, 'PDF', resolution=150.0)
            return pdf
        except Exception as exc:
            QMessageBox.warning(self, 'Import Error', f'Could not import image:\n{exc}')
            return None

    def _import_open_pdf(self):
        from core import openpdf
        if not openpdf.psutil_available():
            QMessageBox.information(self, 'Feature needs psutil', "Detecting open PDFs needs the 'psutil' package.")
            return
        items = openpdf.list_open_pdfs()
        if not items:
            QMessageBox.information(self, 'No open PDFs found', 'No open PDFs were found. Use Import PDF/Image File instead.')
            return
        dlg = OpenPdfPickerDialog(items, self)
        if dlg.exec() and dlg.selected_path():
            self._import_path_with_page_picker(dlg.selected_path(), dlg.pages_text())

    # ── Selection sync ────────────────────────────────────────────
    def _selected_drawing(self):
        item = self.canvas.selected_item()
        return item.drawing if item else None

    def _sync_selection(self, d):
        if not d:
            return
        self.rot.blockSignals(True)
        self.rot.setValue(d.rotation)
        self.rot.blockSignals(False)
        self.scale.blockSignals(True)
        self.scale.setValue(d.scale_factor)
        self.scale.blockSignals(False)
        self.draw_scale_combo.blockSignals(True)
        self.draw_scale_combo.setCurrentText(d.scale_str or '')
        self.draw_scale_combo.blockSignals(False)
        it = self._list_item_for(d.drawing_id)
        if it is not None:
            self._syncing_list = True
            self.list.setCurrentItem(it)
            self._syncing_list = False

    def _on_drawing_changed(self):
        """A live move/rotate updated the model — refresh the angle/scale read-outs."""
        d = self._selected_drawing()
        if not d:
            return
        self.rot.blockSignals(True)
        self.rot.setValue(d.rotation)
        self.rot.blockSignals(False)

    # ── Adjustments ───────────────────────────────────────────────
    def _pick_color(self):
        d = self._selected_drawing()
        if not d:
            return
        c = QColorDialog.getColor(QColor(d.color), self, 'Pick Drawing Color')
        if c.isValid():
            d.color = c.name()
            img = R.render_page(d.page.pdf_path, d.page.page_index, self.overlay_set.render_dpi)
            item = self.canvas.selected_item()
            item.set_base_pixmap(R.pil_to_qpixmap(R.render_single_colored(img, d.color)))

    def _set_rotation(self, value):
        item = self.canvas.selected_item()
        if item:
            item.drawing.rotation = value
            item.setRotation(value)

    def _set_scale(self, value):
        item = self.canvas.selected_item()
        if item:
            item.drawing.scale_factor = value
            item.setScale(value)

    def _nudge(self, dx: int, dy: int):
        item = self.canvas.selected_item()
        if item:
            item.setPos(item.pos() + QPointF(dx, dy))
            item.drawing.offset_x = item.pos().x()
            item.drawing.offset_y = item.pos().y()

    def _undo_erase(self):
        item = self.canvas.selected_item()
        if item and item.drawing.erase_rects:
            item.drawing.erase_rects.pop()
            item.rebuild_pixmap()

    # ── Scale matching ────────────────────────────────────────────
    def _on_ref_scale_changed(self, text):
        self.overlay_set.workspace_ref_scale = text.strip()

    def _on_draw_scale_changed(self, text):
        d = self._selected_drawing()
        if d is not None:
            d.scale_str = text.strip()

    def _match_scale(self, all_drawings: bool):
        ref = self.overlay_set.workspace_ref_scale or self.ref_scale_combo.currentText().strip()
        if not ref:
            self.scale_status.setText('Set a reference scale first.')
            return
        if all_drawings:
            targets = list(self.overlay_set.workspace_drawings)
        else:
            d = self._selected_drawing()
            if d is None:
                self.scale_status.setText('Select a drawing first.')
                return
            targets = [d]

        applied = 0
        skipped = 0
        for d in targets:
            if not d.scale_str:
                skipped += 1
                continue
            factor = compute_scale_factor(ref, d.scale_str)
            d.scale_factor = factor
            item = self.canvas._items_by_id.get(d.drawing_id)
            if item is not None:
                item.setScale(factor)
            applied += 1

        # Refresh the manual scale spin for the current selection.
        cur = self._selected_drawing()
        if cur is not None:
            self.scale.blockSignals(True)
            self.scale.setValue(cur.scale_factor)
            self.scale.blockSignals(False)

        msg = f'Scaled {applied} drawing(s) to {ref}.'
        if skipped:
            msg += f' {skipped} skipped (no drawing scale set).'
        self.scale_status.setText(msg)

    # ── Export ────────────────────────────────────────────────────
    def _export(self, fmt: str):
        if not any(d.visible for d in self.overlay_set.workspace_drawings):
            QMessageBox.information(self, 'Nothing to export',
                                    'There are no visible drawings to export.')
            return
        default_name = f'workspace.{fmt}'
        path, _ = QFileDialog.getSaveFileName(
            self, f'Export {fmt.upper()}',
            os.path.join(self.settings.get('export_path', ''), default_name),
            f'{fmt.upper()} Files (*.{fmt})')
        if not path:
            return
        if not path.lower().endswith('.' + fmt):
            path += '.' + fmt
        try:
            render_dpi = self.overlay_set.render_dpi or 120
            export_dpi = getattr(self.overlay_set, 'export_dpi', None) or render_dpi
            # Supersample so the export is sharper than the on-screen raster.
            scale = max(1.0, export_dpi / render_dpi)
            qimg = self.canvas.render_visible(scale=scale)
            pil = R.qimage_to_pil(qimg).convert('RGB')
            if fmt == 'pdf':
                pil.save(path, 'PDF', resolution=float(export_dpi))
            else:
                pil.save(path)
            QMessageBox.information(self, 'Exported', f'Workspace exported to:\n{path}')
        except Exception as exc:
            QMessageBox.critical(self, 'Export Error', str(exc))

    @staticmethod
    def _collapse_btn_style():
        return 'background:#333; color:#ddd; border:none; border-radius:3px; font-weight:bold;'

    @staticmethod
    def _action_btn_style():
        return 'background:#2a4a6b; color:white; border:none; padding:6px; border-radius:4px;'

    @staticmethod
    def _small_action_style():
        return 'background:#333; color:#ddd; border:none; padding:4px; border-radius:3px; font-size:10px;'

    @staticmethod
    def _small_btn_style():
        return 'background:#2a2a5e; color:white; border:none; border-radius:3px;'

    @staticmethod
    def _save_btn_style():
        return 'background:#1a6b35; color:white; border:none; padding:6px; border-radius:4px;'

    @staticmethod
    def _toggle_btn_style():
        return """
            QPushButton { background:#2a2a2a; color:#ddd; border:1px solid #444; padding:6px; border-radius:4px; text-align:left; }
            QPushButton:hover { background:#333; }
            QPushButton:checked { background:#1a6b35; color:white; border:1px solid #27a350; }
        """
