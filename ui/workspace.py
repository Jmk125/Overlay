"""Empty workspace for stitching many drawings on a blank canvas."""
import os
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QPointF
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel, QListWidget,
    QListWidgetItem, QFileDialog, QColorDialog, QDoubleSpinBox, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem, QGraphicsItem, QMessageBox, QComboBox
)
from core.models import DrawingPage, WorkspaceDrawing, OverlaySet
from core import renderer as R
from ui.landing import OpenPdfPickerDialog


class WorkspacePixmapItem(QGraphicsPixmapItem):
    """Pixmap item that applies drawing tint plus per-drawing erase rectangles."""
    def __init__(self, drawing: WorkspaceDrawing, pixmap: QPixmap):
        super().__init__(pixmap)
        self.drawing = drawing
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setTransformOriginPoint(pixmap.width() / 2, pixmap.height() / 2)
        self._sync_from_model()

    def _sync_from_model(self):
        self.setPos(self.drawing.offset_x, self.drawing.offset_y)
        self.setRotation(self.drawing.rotation)
        self.setScale(self.drawing.scale_factor)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.drawing.offset_x = self.pos().x()
            self.drawing.offset_y = self.pos().y()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            pass
        return super().itemChange(change, value)

    def paint(self, painter: QPainter, option, widget=None):
        painter.drawPixmap(0, 0, self.pixmap())
        bg = QColor('#ffffff') if self.drawing.erase_bg == 'white' else QColor('#0d0d0d')
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        for r in self.drawing.erase_rects:
            painter.drawRect(QRectF(r[0] * self.pixmap().width(), r[1] * self.pixmap().height(),
                                    r[2] * self.pixmap().width(), r[3] * self.pixmap().height()))
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

    MODE_MOVE = 'move'
    MODE_ERASE = 'erase'

    def __init__(self):
        super().__init__()
        self.gscene = QGraphicsScene(self)
        self.setScene(self.gscene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._mode = self.MODE_MOVE
        self._bg = 'white'
        self._items_by_id = {}
        self._erase_start = None
        self._erase_item = None
        self.gscene.selectionChanged.connect(self._on_scene_selection)
        self.set_canvas_background('white')

    def set_canvas_background(self, bg: str):
        self._bg = bg
        color = '#ffffff' if bg != 'dark' else '#0d0d0d'
        self.setStyleSheet(f'background:{color}; border:none;')
        self.gscene.setBackgroundBrush(QBrush(QColor(color)))
        for item in self._items_by_id.values():
            item.drawing.erase_bg = 'white' if bg != 'dark' else 'dark'
            item.update()

    def set_mode(self, mode: str):
        self._mode = mode
        self.setDragMode(QGraphicsView.DragMode.NoDrag if mode == self.MODE_ERASE else QGraphicsView.DragMode.RubberBandDrag)

    def add_drawing(self, drawing: WorkspaceDrawing, pixmap: QPixmap):
        drawing.erase_bg = 'white' if self._bg != 'dark' else 'dark'
        item = WorkspacePixmapItem(drawing, pixmap)
        item.setZValue(len(self._items_by_id))
        self.gscene.addItem(item)
        self._items_by_id[drawing.drawing_id] = item
        self.gscene.clearSelection()
        item.setSelected(True)
        self.fitInView(self.gscene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def select_drawing(self, drawing_id: str):
        self.gscene.clearSelection()
        item = self._items_by_id.get(drawing_id)
        if item:
            item.setSelected(True)
            self.centerOn(item)

    def selected_item(self):
        items = [i for i in self.gscene.selectedItems() if isinstance(i, WorkspacePixmapItem)]
        return items[-1] if items else None

    def _on_scene_selection(self):
        self.selection_changed.emit(self.selected_item().drawing if self.selected_item() else None)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if self._mode == self.MODE_ERASE and event.button() == Qt.MouseButton.LeftButton and self.selected_item():
            self._erase_start = self.selected_item().mapFromScene(self.mapToScene(event.position().toPoint()))
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._erase_start is not None and event.button() == Qt.MouseButton.LeftButton:
            item = self.selected_item()
            end = item.mapFromScene(self.mapToScene(event.position().toPoint())) if item else QPointF()
            if item:
                x0, x1 = sorted([self._erase_start.x(), end.x()])
                y0, y1 = sorted([self._erase_start.y(), end.y()])
                if x1 - x0 > 4 and y1 - y0 > 4:
                    w, h = item.pixmap().width(), item.pixmap().height()
                    item.drawing.erase_rects.append([x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h])
                    item.update()
                    self.drawing_changed.emit()
            self._erase_start = None
            return
        super().mouseReleaseEvent(event)
        self.drawing_changed.emit()


class EmptyWorkspace(QWidget):
    save_project = pyqtSignal(object)

    def __init__(self, overlay_set: OverlaySet, settings: dict, parent=None):
        super().__init__(parent)
        self.overlay_set = overlay_set
        self.settings = settings
        self._build_ui()
        self.canvas.set_canvas_background(overlay_set.canvas_bg)
        for d in overlay_set.workspace_drawings:
            self._render_and_add(d, fit=False)

    def _build_ui(self):
        root = QHBoxLayout(self)
        self.list = QListWidget()
        self.list.setFixedWidth(220)
        self.list.currentItemChanged.connect(self._select_from_list)
        root.addWidget(self.list)
        self.canvas = WorkspaceCanvas()
        self.canvas.selection_changed.connect(self._sync_selection)
        root.addWidget(self.canvas, 1)
        tools = QVBoxLayout()
        for label, cb in [('Import PDF/Image File', self._import_file), ('Import from Open PDF', self._import_open_pdf)]:
            b = QPushButton(label); b.clicked.connect(cb); tools.addWidget(b)
        self.color_btn = QPushButton('Drawing Color'); self.color_btn.clicked.connect(self._pick_color); tools.addWidget(self.color_btn)
        self.mode = QComboBox(); self.mode.addItems(['Move / Select', 'Erase Rectangle']); self.mode.currentIndexChanged.connect(lambda i: self.canvas.set_mode('erase' if i else 'move')); tools.addWidget(self.mode)
        self.rot = QDoubleSpinBox(); self.rot.setRange(-360, 360); self.rot.setSuffix('°'); self.rot.valueChanged.connect(self._set_rotation); tools.addWidget(QLabel('Rotation:')); tools.addWidget(self.rot)
        self.scale = QDoubleSpinBox(); self.scale.setRange(0.01, 20); self.scale.setSingleStep(0.05); self.scale.setValue(1); self.scale.valueChanged.connect(self._set_scale); tools.addWidget(QLabel('Scale:')); tools.addWidget(self.scale)
        reset_erase = QPushButton('Undo Last Erase'); reset_erase.clicked.connect(self._undo_erase); tools.addWidget(reset_erase)
        fit = QPushButton('Fit Workspace'); fit.clicked.connect(lambda: self.canvas.fitInView(self.canvas.gscene.itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)); tools.addWidget(fit)
        save = QPushButton('💾 Save Project'); save.clicked.connect(lambda: self.save_project.emit(self.overlay_set)); tools.addWidget(save)
        tools.addWidget(QLabel('Tip: select a drawing in the list/canvas, move it by dragging, then erase unwanted notes with Erase Rectangle.'))
        tools.addStretch()
        panel = QWidget(); panel.setLayout(tools); panel.setFixedWidth(260); root.addWidget(panel)

    def _add_to_list(self, d):
        item = QListWidgetItem(d.name); item.setData(Qt.ItemDataRole.UserRole, d.drawing_id); self.list.addItem(item)

    def _render_and_add(self, d, fit=True):
        img = R.render_page(d.page.pdf_path, d.page.page_index, self.overlay_set.render_dpi)
        colored = R.render_single_colored(img, d.color)
        self.canvas.add_drawing(d, R.pil_to_qpixmap(colored))
        self._add_to_list(d)

    def _import_file(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Import drawings', self.settings.get('last_open_dir', ''), 'Drawings (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)')
        for p in paths: self._add_path(p)

    def _add_path(self, path):
        if not path.lower().endswith('.pdf'):
            from ui.landing import LandingScreen
            path = LandingScreen(self.settings)._image_file_to_pdf(path)
        page = DrawingPage(path, 0, display_name=os.path.basename(path))
        d = WorkspaceDrawing(page=page, name=os.path.splitext(os.path.basename(path))[0])
        self.overlay_set.workspace_drawings.append(d)
        self._render_and_add(d)

    def _import_open_pdf(self):
        from core import openpdf
        items = openpdf.list_open_pdfs() if openpdf.psutil_available() else []
        if not items:
            QMessageBox.information(self, 'No open PDFs found', 'No open PDFs were found. Use Import PDF/Image File instead.')
            return
        dlg = OpenPdfPickerDialog(items, self)
        if dlg.exec() and dlg.selected_path(): self._add_path(dlg.selected_path())

    def _selected_drawing(self):
        item = self.canvas.selected_item()
        return item.drawing if item else None

    def _sync_selection(self, d):
        if not d: return
        self.rot.blockSignals(True); self.rot.setValue(d.rotation); self.rot.blockSignals(False)
        self.scale.blockSignals(True); self.scale.setValue(d.scale_factor); self.scale.blockSignals(False)
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == d.drawing_id:
                self.list.setCurrentRow(i); break

    def _select_from_list(self, item, old=None):
        if item: self.canvas.select_drawing(item.data(Qt.ItemDataRole.UserRole))

    def _pick_color(self):
        d = self._selected_drawing()
        if not d: return
        c = QColorDialog.getColor(QColor(d.color), self, 'Pick Drawing Color')
        if c.isValid():
            d.color = c.name()
            img = R.render_page(d.page.pdf_path, d.page.page_index, self.overlay_set.render_dpi)
            self.canvas.selected_item().setPixmap(R.pil_to_qpixmap(R.render_single_colored(img, d.color)))
            self.canvas.selected_item().update()

    def _set_rotation(self, value):
        item = self.canvas.selected_item()
        if item: item.drawing.rotation = value; item.setRotation(value)

    def _set_scale(self, value):
        item = self.canvas.selected_item()
        if item: item.drawing.scale_factor = value; item.setScale(value)

    def _undo_erase(self):
        item = self.canvas.selected_item()
        if item and item.drawing.erase_rects:
            item.drawing.erase_rects.pop(); item.update()
