"""
PDF rendering and overlay compositing engine
"""
import fitz  # PyMuPDF
import math
import os
import sys
import numpy as np
from PIL import Image, ImageChops
from PyQt6.QtGui import (
    QImage, QPixmap, QColor, QPainter, QPen, QBrush, QPainterPath
)
from PyQt6.QtCore import Qt, QPointF, QRectF
import io


def forward_matrix(w: float, h: float, offset_x: float, offset_y: float,
                   rotation: float, pivot_x: float, pivot_y: float,
                   scale_factor: float) -> np.ndarray:
    """
    Build the 3x3 affine matrix that maps a pixel in Drawing B's own
    (natural, unscaled) coordinates to the shared canvas coordinates.

    The transform is: scale about the top-left, then rotate by `rotation`
    degrees about the (scaled) pivot point, then translate by the offset.

    This single matrix is the source of truth for BOTH the live Qt preview
    (forward map) and the final PIL composite (its inverse), so the two
    always line up exactly — no jump when a drag is released.
    """
    th = math.radians(rotation)
    cos, sin = math.cos(th), math.sin(th)
    # Pivot expressed in scaled-image coordinates.
    cx = pivot_x * w * scale_factor
    cy = pivot_y * h * scale_factor

    def T(dx, dy):
        return np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]], dtype=float)

    S = np.array([[scale_factor, 0, 0], [0, scale_factor, 0], [0, 0, 1]], dtype=float)
    # Clockwise-positive rotation in the image's y-down coordinate system.
    Rm = np.array([[cos, -sin, 0], [sin, cos, 0], [0, 0, 1]], dtype=float)

    return T(offset_x, offset_y) @ T(cx, cy) @ Rm @ T(-cx, -cy) @ S


def render_page(pdf_path: str, page_index: int, dpi: int = 150) -> Image.Image:
    """Render a PDF page to a PIL Image (RGBA)"""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    doc.close()
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return img.convert("RGBA")


def pil_to_qpixmap(img: Image.Image) -> QPixmap:
    """Convert PIL Image to QPixmap"""
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    qimg = QImage()
    qimg.loadFromData(buf.read())
    return QPixmap.fromImage(qimg)


def apply_transform(img: Image.Image, offset_x: float, offset_y: float,
                    rotation: float, pivot_x: float, pivot_y: float,
                    scale_factor: float, canvas_size: tuple) -> Image.Image:
    """
    Apply scale, rotation (around the pivot) and translation to an image,
    rendering the result onto a transparent canvas of canvas_size.

    Uses the same affine matrix as the live Qt preview (see forward_matrix)
    so the on-screen alignment and the committed composite match exactly.
    """
    w, h = img.size
    cw, ch = canvas_size

    # Fast path: identity transform — just paste at the origin.
    if (abs(scale_factor - 1.0) < 1e-6 and abs(rotation) < 1e-6
            and abs(offset_x) < 1e-6 and abs(offset_y) < 1e-6
            and (w, h) == (cw, ch)):
        return img.copy()

    M = forward_matrix(w, h, offset_x, offset_y, rotation,
                       pivot_x, pivot_y, scale_factor)
    # PIL's AFFINE transform maps output coords -> input coords, so it needs
    # the inverse of our forward (input -> output) matrix.
    inv = np.linalg.inv(M)
    coeffs = (inv[0, 0], inv[0, 1], inv[0, 2],
              inv[1, 0], inv[1, 1], inv[1, 2])
    return img.transform((cw, ch), Image.AFFINE, coeffs,
                         resample=Image.BICUBIC)


def composite_overlay(img_a: Image.Image, img_b: Image.Image,
                      color_a: str, color_b: str,
                      threshold: int = 30,
                      shared_color: str = "#000000") -> Image.Image:
    """
    Composite two drawings:
    - Pixels present in both -> shared_color
    - Pixels only in A -> color_a
    - Pixels only in B -> color_b
    - Background -> transparent

    'Present' means darker than threshold (i.e., it's ink, not paper).
    Grey source pixels are preserved as lighter alpha so shaded regions do not
    get flattened into the same visual weight as black linework.
    """
    # Ensure same size
    size = (max(img_a.width, img_b.width), max(img_a.height, img_b.height))
    canvas_a = Image.new("RGBA", size, (255, 255, 255, 0))
    canvas_b = Image.new("RGBA", size, (255, 255, 255, 0))
    canvas_a.paste(img_a, (0, 0))
    canvas_b.paste(img_b, (0, 0))

    # Convert to numpy for fast processing
    arr_a = np.array(canvas_a.convert("RGB")).astype(np.int16)
    arr_b = np.array(canvas_b.convert("RGB")).astype(np.int16)

    # "Ink" mask: pixel is dark (ink present) if luminance < 255 - threshold.
    # Keep the darkness value as alpha so grey poche/hatches stay visibly
    # lighter than black linework instead of being flattened to solid ink.
    def luminance(arr):
        return 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]

    lum_a = luminance(arr_a)
    lum_b = luminance(arr_b)
    mask_a = lum_a < (255 - threshold)
    mask_b = lum_b < (255 - threshold)
    alpha_a = np.clip(255 - lum_a, 0, 255).astype(np.uint8)
    alpha_b = np.clip(255 - lum_b, 0, 255).astype(np.uint8)

    # Parse colors
    def hex_to_rgb(hex_color):
        c = QColor(hex_color)
        return (c.red(), c.green(), c.blue())

    rgb_a = hex_to_rgb(color_a)
    rgb_b = hex_to_rgb(color_b)
    rgb_shared = hex_to_rgb(shared_color)

    h, w = mask_a.shape
    result = np.zeros((h, w, 4), dtype=np.uint8)

    # Both present -> shared color (black on white bg, white on dark bg)
    both = mask_a & mask_b
    result[both, 0] = rgb_shared[0]
    result[both, 1] = rgb_shared[1]
    result[both, 2] = rgb_shared[2]
    result[both, 3] = np.maximum(alpha_a[both], alpha_b[both])

    # Only in A -> color_a
    only_a = mask_a & ~mask_b
    result[only_a, 0] = rgb_a[0]
    result[only_a, 1] = rgb_a[1]
    result[only_a, 2] = rgb_a[2]
    result[only_a, 3] = alpha_a[only_a]

    # Only in B -> color_b
    only_b = mask_b & ~mask_a
    result[only_b, 0] = rgb_b[0]
    result[only_b, 1] = rgb_b[1]
    result[only_b, 2] = rgb_b[2]
    result[only_b, 3] = alpha_b[only_b]

    # Background stays transparent (alpha=0 already)
    return Image.fromarray(result, "RGBA")


def render_single_colored(img: Image.Image, color: str, threshold: int = 30) -> Image.Image:
    """Render a single drawing page with ink in the specified color.

    Source darkness becomes output alpha, preserving grey fills/hatches as
    lighter drawing content instead of forcing every ink pixel to solid color.
    """
    arr = np.array(img.convert("RGB")).astype(np.int16)
    luminance = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
    mask = luminance < (255 - threshold)

    def hex_to_rgb(hex_color):
        c = QColor(hex_color)
        return (c.red(), c.green(), c.blue())

    rgb = hex_to_rgb(color)
    h, w = mask.shape
    result = np.zeros((h, w, 4), dtype=np.uint8)
    result[mask, 0] = rgb[0]
    result[mask, 1] = rgb[1]
    result[mask, 2] = rgb[2]
    result[mask, 3] = np.clip(255 - luminance[mask], 0, 255).astype(np.uint8)
    return Image.fromarray(result, "RGBA")


# ── Markup rendering (shared by the on-canvas overlay and export) ──────

def cloud_path(rect: QRectF, bump: float) -> QPainterPath:
    """Build a revision-cloud path: scalloped bumps bulging outward around a
    rectangle, using quadratic curves so the direction is easy to control."""
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    bump = max(4.0, bump)
    # Clockwise edges with their outward normals.
    edges = [
        ((x0, y0), (x1, y0), (0, -1)),   # top
        ((x1, y0), (x1, y1), (1, 0)),    # right
        ((x1, y1), (x0, y1), (0, 1)),    # bottom
        ((x0, y1), (x0, y0), (-1, 0)),   # left
    ]
    path = QPainterPath()
    path.moveTo(x0, y0)
    for (sx, sy), (ex, ey), (nx, ny) in edges:
        length = math.hypot(ex - sx, ey - sy)
        nb = max(1, int(round(length / (2 * bump))))
        dx, dy = (ex - sx) / nb, (ey - sy) / nb
        for i in range(nb):
            px, py = sx + dx * i, sy + dy * i
            qx, qy = sx + dx * (i + 1), sy + dy * (i + 1)
            mx = (px + qx) / 2 + nx * bump
            my = (py + qy) / 2 + ny * bump
            path.quadTo(mx, my, qx, qy)
    path.closeSubpath()
    return path


def paint_markups(painter: QPainter, markups: list, width: int, height: int):
    """Draw a list of normalized markups onto a QPainter sized width×height."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    for m in markups:
        pts = [(p[0] * width, p[1] * height) for p in m.get('points', [])]
        if len(pts) < 2:
            continue
        pen = QPen(QColor(m.get('color', '#ff0000')))
        pen.setWidthF(max(1.0, float(m.get('width', 0.003)) * width))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mtype = m.get('type', 'line')
        if mtype == 'line':
            painter.drawLine(QPointF(*pts[0]), QPointF(*pts[1]))
        elif mtype == 'rect':
            painter.drawRect(QRectF(QPointF(*pts[0]), QPointF(*pts[1])).normalized())
        elif mtype == 'cloud':
            rect = QRectF(QPointF(*pts[0]), QPointF(*pts[1])).normalized()
            bump = max(6.0, min(rect.width(), rect.height()) * 0.12)
            painter.drawPath(cloud_path(rect, bump))


def render_markups_pil(markups: list, width: int, height: int) -> Image.Image:
    """Render markups to a transparent RGBA PIL image (for export compositing)."""
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    paint_markups(painter, markups, width, height)
    painter.end()
    # Convert QImage -> PNG bytes -> PIL RGBA
    from PyQt6.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    qbuf = QBuffer(ba)
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(qbuf, "PNG")
    buf = io.BytesIO(bytes(ba))
    buf.seek(0)
    return Image.open(buf).convert("RGBA")


def get_page_count(pdf_path: str) -> int:
    doc = fitz.open(pdf_path)
    count = len(doc)
    doc.close()
    return count


def render_thumbnail(pdf_path: str, page_index: int, max_size: int = 200) -> QPixmap:
    """Render a small thumbnail for the matching UI"""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    # Scale to fit max_size
    rect = page.rect
    scale = max_size / max(rect.width, rect.height)
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    doc.close()
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return pil_to_qpixmap(img)


def render_thumbnail_doc(doc, page_index: int, max_size: int = 180) -> Image.Image:
    """Render a thumbnail from an already-open document as a PIL image.

    Reusing one open document across many pages avoids re-opening the PDF for
    every page — a big speed-up when previewing large sets. Returns a PIL image
    (safe to build off the GUI thread; convert to QPixmap on the main thread)."""
    page = doc[page_index]
    rect = page.rect
    scale = max_size / max(rect.width, rect.height)
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _tesseract_exe_name() -> str:
    return 'tesseract.exe' if os.name == 'nt' else 'tesseract'


def _app_base_dirs() -> list:
    """Directories to search for a Tesseract bundled alongside the app."""
    bases = []
    if getattr(sys, 'frozen', False):
        # PyInstaller: data is extracted to _MEIPASS; the exe lives elsewhere.
        bases.append(getattr(sys, '_MEIPASS', ''))
        bases.append(os.path.dirname(sys.executable))
    # Project / source root (…/core/renderer.py -> project root)
    bases.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [b for b in bases if b]


def find_bundled_tesseract() -> str:
    """Return the path to a Tesseract binary shipped with the app, or ''.

    Looks for a 'tesseract' or 'Tesseract-OCR' folder next to the app (or in the
    PyInstaller bundle) — just drop the portable Tesseract folder there.
    """
    exe = _tesseract_exe_name()
    for base in _app_base_dirs():
        for sub in ('tesseract', 'Tesseract-OCR', ''):
            cand = os.path.join(base, sub, exe) if sub else os.path.join(base, exe)
            if os.path.exists(cand):
                return cand
    return ''


def configure_tesseract(explicit_path: str = None) -> bool:
    """Point pytesseract at a Tesseract binary.

    Resolution order: an explicit path (file or its folder) from settings, then
    a copy bundled next to the app, otherwise leave pytesseract's default (a
    system install on PATH). Also sets TESSDATA_PREFIX to the sibling tessdata
    folder so a bundled copy finds its language files. Returns True if a binary
    was located and configured here.
    """
    try:
        import pytesseract
    except Exception:
        return False

    path = ''
    if explicit_path:
        if os.path.isdir(explicit_path):
            cand = os.path.join(explicit_path, _tesseract_exe_name())
            path = cand if os.path.exists(cand) else ''
        elif os.path.exists(explicit_path):
            path = explicit_path
    if not path:
        path = find_bundled_tesseract()

    if path:
        pytesseract.pytesseract.tesseract_cmd = path
        tessdata = os.path.join(os.path.dirname(path), 'tessdata')
        if os.path.isdir(tessdata):
            os.environ['TESSDATA_PREFIX'] = tessdata
        return True
    return False


def tesseract_available() -> bool:
    """True if pytesseract is importable and the Tesseract binary is reachable."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# Auto-detect a bundled Tesseract as soon as the renderer is imported, so a
# portable copy dropped next to the app works with no configuration.
configure_tesseract()


def render_region_pixmap(pdf_path: str, page_index: int, rect_norm: tuple,
                         dpi: int = 300) -> QPixmap:
    """Render just the boxed region of a page to a QPixmap (for OCR preview)."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    pw = page.rect.width
    ph = page.rect.height
    x1, y1, x2, y2 = rect_norm
    clip = fitz.Rect(x1 * pw, y1 * ph, x2 * pw, y2 * ph)
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    doc.close()
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return pil_to_qpixmap(img)


def ocr_region(pdf_path: str, page_index: int, rect_norm: tuple, dpi: int = 300) -> str:
    """
    OCR a normalized region (x1,y1,x2,y2 as 0-1 fractions of page) and return text.

    The region is rendered at high DPI, converted to high-contrast grayscale and
    restricted to the characters that appear in sheet numbers. We try a
    single-line pass first, then fall back to a block pass — this noticeably
    reduces "unread" results on small title blocks.
    """
    try:
        import pytesseract
        from PIL import ImageOps
        doc = fitz.open(pdf_path)
        page = doc[page_index]
        pw = page.rect.width
        ph = page.rect.height
        x1, y1, x2, y2 = rect_norm
        clip = fitz.Rect(x1 * pw, y1 * ph, x2 * pw, y2 * ph)
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        doc.close()
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        # Preprocess: grayscale + autocontrast, and upscale tiny crops.
        g = ImageOps.autocontrast(ImageOps.grayscale(img))
        if max(g.size) < 500:
            scale = 500.0 / max(g.size)
            g = g.resize((max(1, int(g.width * scale)),
                          max(1, int(g.height * scale))), Image.LANCZOS)

        whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-/"
        base = f"-c tessedit_char_whitelist={whitelist}"
        # psm 7 = single text line; psm 6 = uniform block (fallback).
        text = pytesseract.image_to_string(g, config=f"--psm 7 {base}").strip()
        if not text:
            text = pytesseract.image_to_string(g, config=f"--psm 6 {base}").strip()
        return text
    except Exception:
        return ""
