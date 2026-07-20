"""
Landing / New Overlay screen
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
    QColorDialog, QFrame, QSizePolicy, QMessageBox, QSpinBox,
    QGroupBox, QScrollArea, QDialog, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPixmap, QPainter, QImage
import os
import uuid
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


class OpenPdfPickerDialog(QDialog):
    """Lists PDFs currently open in other apps; returns the chosen path."""
    def __init__(self, items: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import from an open PDF")
        self.setMinimumWidth(520)
        self.setStyleSheet("background:#161616; color:#e0e0e0;")
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("PDFs currently open in Bluebeam / Acrobat / etc.:"))
        self.list = QListWidget()
        self.list.setStyleSheet("background:#1e1e1e; color:#eee; border:1px solid #444;")
        for it in items:
            label = f"{os.path.basename(it['path'])}    —    {it['app']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, it['path'])
            item.setToolTip(it['path'])
            self.list.addItem(item)
        if items:
            self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(self.accept)
        layout.addWidget(self.list)

        # Optional page range — fill it in to skip the page-picker window.
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Pages (optional):"))
        self.pages_edit = QLineEdit()
        self.pages_edit.setPlaceholderText("e.g.  5   or   1-20, 45   — leave blank to pick pages in the next window")
        self.pages_edit.setStyleSheet("background:#2a2a2a; color:#eee; border:1px solid #555; padding:4px;")
        self.pages_edit.returnPressed.connect(self.accept)
        pr.addWidget(self.pages_edit, 1)
        layout.addLayout(pr)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet("background:#444; color:#ddd; border:none; padding:6px 14px; border-radius:4px;")
        ok = QPushButton("Use This PDF")
        ok.clicked.connect(self.accept)
        ok.setStyleSheet("background:#1a6b35; color:white; border:none; padding:6px 14px; border-radius:4px;")
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)

    def selected_path(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def pages_text(self) -> str:
        return self.pages_edit.text().strip()


class LandingScreen(QWidget):
    start_matching = pyqtSignal(object)   # emits OverlaySet
    start_viewer = pyqtSignal(object)     # emits OverlaySet (single-drawing fast path)
    open_project = pyqtSignal(str)        # emits filepath
    create_empty_workspace = pyqtSignal()

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.pages_a: list[DrawingPage] = []
        self.pages_b: list[DrawingPage] = []
        self._build_ui()
        self.setAcceptDrops(True)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(24, 24, 24, 24)

        # Title
        title = QLabel("Drawing Overlay Tool")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff;")
        root.addWidget(title)

        subtitle = QLabel("Compare two drawing sets, or stitch many drawings together on a blank workspace.")
        subtitle.setStyleSheet("color: #aaaaaa; font-size: 12px;")
        root.addWidget(subtitle)

        # Open existing project
        open_row = QHBoxLayout()
        open_btn = QPushButton("📂  Open Existing Project (.overlay)")
        open_btn.setFixedHeight(36)
        open_btn.setStyleSheet(self._btn_style("#2a4a6b", "#3a6491"))
        open_btn.clicked.connect(self._open_project)
        open_row.addWidget(open_btn)
        empty_btn = QPushButton("🧩  Create Empty Work Space")
        empty_btn.setFixedHeight(36)
        empty_btn.setStyleSheet(self._btn_style("#4f3b6b", "#6b5191"))
        empty_btn.clicked.connect(self.create_empty_workspace)
        open_row.addWidget(empty_btn)
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
        screen_dpi_label = QLabel("Screen DPI:")
        screen_dpi_label.setToolTip("On-screen working resolution — lower is faster")
        color_row.addWidget(screen_dpi_label)
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 300)
        self.dpi_spin.setValue(self.settings.get('render_dpi', 120))
        self.dpi_spin.setFixedWidth(70)
        self.dpi_spin.setToolTip("On-screen working resolution — lower is faster")
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

        from_open_btn = QPushButton("📄 From open PDF (Bluebeam / Acrobat)")
        from_open_btn.setToolTip("Pick a PDF that's currently open in another app")
        from_open_btn.setStyleSheet(self._btn_style("#2a4a6b", "#3a6491"))
        layout.addWidget(from_open_btn)

        page_list = PageListWidget("")
        layout.addWidget(page_list)

        status = QLabel("No files loaded")
        status.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(status)

        status.setText("No files loaded — or drag & drop a PDF or image here")

        panel = {
            'widget': group,
            'side': side,
            'name_edit': name_edit,
            'page_list': page_list,
            'status': status,
        }

        load_single_btn.clicked.connect(lambda: self._load_single(side, panel))
        load_set_btn.clicked.connect(lambda: self._load_set(side, panel))
        from_open_btn.clicked.connect(lambda: self._load_from_open_app(side, panel))

        return panel

    def _load_from_open_app(self, side: str, panel: dict):
        """Pick a PDF currently open in Bluebeam/Acrobat/etc. and load it."""
        from core import openpdf
        if not openpdf.psutil_available():
            QMessageBox.information(
                self, "Feature needs psutil",
                "Detecting open PDFs needs the 'psutil' package.\n\n"
                "Install it with:\n    pip install psutil\n\n"
                "(It's included automatically in the packaged .exe build.)")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            items = openpdf.list_open_pdfs()
        finally:
            QApplication.restoreOverrideCursor()

        if not items:
            QMessageBox.information(
                self, "No open PDFs found",
                "I couldn't find a PDF open in another app.\n\n"
                "Make sure the drawing is open in Bluebeam/Acrobat, then try "
                "again. If it still doesn't appear, use “Load Single PDF”.")
            return

        dlg = OpenPdfPickerDialog(items, self)
        if dlg.exec():
            path = dlg.selected_path()
            if not (path and os.path.exists(path)):
                return
            pages_text = dlg.pages_text()
            if pages_text:
                # Skip the page picker — load exactly the requested pages.
                self._load_pages_by_range(side, panel, path, pages_text)
            else:
                self._handle_dropped(side, panel, [path])

    def _load_pages_by_range(self, side: str, panel: dict, path: str, pages_text: str):
        from ui.page_selector import parse_page_ranges
        try:
            count = R.get_page_count(path)
        except Exception:
            count = 1
        indices = parse_page_ranges(pages_text, count)
        if not indices:
            QMessageBox.information(
                self, "Page range",
                f"No valid pages in “{pages_text}”. This PDF has {count} page(s).")
            return
        pages = [DrawingPage(pdf_path=path, page_index=i, display_name=f"Page {i + 1}")
                 for i in indices]
        self._assign_pages(side, pages)
        panel['page_list'].set_pages(pages)
        panel['status'].setText(f"{len(pages)} page(s) from {os.path.basename(path)}")

    def _load_single(self, side: str, panel: dict):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Load PDF(s)", self.settings.get('last_open_dir', ''),
            "PDF Files (*.pdf)"
        )
        if not paths:
            return
        self._load_single_pages(side, panel, paths)

    def _load_single_pages(self, side: str, panel: dict, paths: list):
        """Load the first page of each given PDF (one drawing per file)."""
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
        self._load_set_from_path(side, panel, path)

    def _load_set_from_path(self, side: str, panel: dict, path: str):
        """Open the page selector for a multi-page PDF and load chosen pages."""
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

    # ── Drag & drop ───────────────────────────────────────────────
    # Windows clipboard format some apps use to drag a "virtual" file (a page
    # that isn't a file on disk yet) — exposed by Qt under this MIME name.
    WIN_FILECONTENTS = 'application/x-qt-windows-mime;value="FileContents"'
    IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.gif')

    def _pdf_urls(self, mime) -> list:
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls()
                if u.toLocalFile().lower().endswith('.pdf')]

    def _image_urls(self, mime) -> list:
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls()
                if u.toLocalFile().lower().endswith(self.IMAGE_EXTS)]

    def _extract_filecontents(self, mime):
        """Try to pull PDF bytes from any 'FileContents' drag format (how apps
        like Bluebeam drag a page out as a virtual file). Returns bytes or None."""
        for fmt in mime.formats():
            if 'filecontents' in fmt.lower():
                try:
                    data = bytes(mime.data(fmt))
                except Exception:
                    continue
                if not data:
                    continue
                idx = data.find(b'%PDF-')   # some wrappers prepend bytes
                if idx != -1:
                    return data[idx:]
                return data   # let the PDF reader try anyway
        return None

    def _has_file_descriptor(self, mime) -> bool:
        """A Windows virtual-file drag (descriptor/contents) is present."""
        for f in mime.formats():
            fl = f.lower()
            if 'filegroupdescriptor' in fl or 'filecontents' in fl:
                return True
        return False

    def _droppable(self, mime) -> bool:
        # Accept anything we might be able to use — including a virtual-file
        # drag, so the drop is allowed and we can inspect/extract it (otherwise
        # the OS shows the "no drop" cursor and dropEvent never fires).
        return bool(self._pdf_urls(mime)) or bool(self._image_urls(mime)) \
            or mime.hasImage() or self._has_file_descriptor(mime)

    def dragEnterEvent(self, event):
        if self._droppable(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._droppable(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        pos = event.position().toPoint()
        if self.panel_b['widget'].geometry().contains(pos):
            side, panel = 'b', self.panel_b
        else:
            side, panel = 'a', self.panel_a

        # 1. Real PDF file(s)
        paths = self._pdf_urls(mime)
        if paths:
            self._handle_dropped(side, panel, paths)
            event.acceptProposedAction()
            return

        # 2. A page dragged out as a virtual PDF (e.g. Bluebeam thumbnail)
        data = self._extract_filecontents(mime)
        if data:
            saved = self._save_pdf_bytes(data)
            if saved:
                self._handle_dropped(side, panel, [saved])
                event.acceptProposedAction()
                return

        # 3. Image file(s) on disk
        imgs = self._image_urls(mime)
        if imgs:
            pdfs = [p for p in (self._image_file_to_pdf(i) for i in imgs) if p]
            if pdfs:
                self._load_single_pages(side, panel, pdfs)
                event.acceptProposedAction()
                return

        # 4. Rasterized content (a selection / snapshot / page image dragged
        #    straight out of Bluebeam, Acrobat, etc.)
        if mime.hasImage():
            pdf = self._image_to_pdf(mime.imageData())
            if pdf:
                self._load_single_pages(side, panel, [pdf])
                event.acceptProposedAction()
                return

        # Nothing usable — show what the source offered so it can be supported.
        self._report_unhandled_drop(mime)
        event.ignore()

    def _handle_dropped(self, side: str, panel: dict, paths: list):
        """A single multi-page PDF opens the page selector; otherwise each
        dropped file is loaded as a single drawing (its first page)."""
        if len(paths) == 1:
            try:
                multipage = R.get_page_count(paths[0]) > 1
            except Exception:
                multipage = False
            if multipage:
                self._load_set_from_path(side, panel, paths[0])
                return
        self._load_single_pages(side, panel, paths)

    # ── Importing dragged images / virtual files ──────────────────
    def _import_dir(self) -> str:
        d = os.path.join(os.path.expanduser("~/.drawing_overlay"), "imported")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_pdf_bytes(self, data: bytes):
        try:
            pdf = os.path.join(self._import_dir(), f"drop_{uuid.uuid4().hex}.pdf")
            with open(pdf, 'wb') as f:
                f.write(data)
            if R.get_page_count(pdf) >= 1:
                return pdf
        except Exception:
            pass
        return None

    def _image_to_pdf(self, image):
        """Convert dragged QImage data into a one-page PDF we can render."""
        try:
            img = image if isinstance(image, QImage) else QImage(image)
            if img.isNull():
                return None
            base = os.path.join(self._import_dir(), f"drop_{uuid.uuid4().hex}")
            png = base + ".png"
            if not img.save(png, "PNG"):
                return None
            from PIL import Image
            pdf = base + ".pdf"
            Image.open(png).convert("RGB").save(pdf, "PDF", resolution=150.0)
            try:
                os.remove(png)
            except OSError:
                pass
            return pdf
        except Exception:
            return None

    def _image_file_to_pdf(self, path: str):
        try:
            from PIL import Image
            pdf = os.path.join(self._import_dir(), f"img_{uuid.uuid4().hex}.pdf")
            Image.open(path).convert("RGB").save(pdf, "PDF", resolution=150.0)
            return pdf
        except Exception:
            return None

    def _report_unhandled_drop(self, mime):
        lines = []
        for f in mime.formats():
            try:
                n = len(bytes(mime.data(f)))
            except Exception:
                n = -1
            lines.append(f"• {f}  ({n} bytes)")
        body = "\n".join(lines) or "(none)"
        QMessageBox.information(
            self, "Couldn't import that drop",
            "I couldn't extract a PDF or image from that drag.\n\n"
            "Formats the source app offered:\n" + body + "\n\n"
            "If there's a 'FileContents' entry showing 0 bytes, the app handed "
            "over a virtual file that Qt can't read directly — send me this list "
            "and I'll add a Windows-specific reader for it.\n\n"
            "Tip: dragging the actual PDF file (from Explorer) always works.")

    def _assign_pages(self, side: str, pages):
        if side == 'a':
            self.pages_a = pages
        else:
            self.pages_b = pages

    def _start(self):
        if not self.pages_a or not self.pages_b:
            QMessageBox.warning(self, "Missing Pages", "Please load pages for both Set A and Set B.")
            return

        bg = self.settings.get('canvas_bg', 'white')
        overlay_set = OverlaySet(
            set_a_label=self.panel_a['name_edit'].text(),
            set_b_label=self.panel_b['name_edit'].text(),
            color_a=self.color_btn_a.color(),
            color_b=self.color_btn_b.color(),
            render_dpi=self.dpi_spin.value(),
            export_dpi=self.settings.get('export_dpi', 200),
            canvas_bg=bg,
            shared_color='#000000' if bg == 'white' else '#ffffff',
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
