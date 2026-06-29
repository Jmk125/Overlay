"""
PDF rendering and overlay compositing engine
"""
import fitz  # PyMuPDF
import math
import numpy as np
from PIL import Image, ImageChops
from PyQt6.QtGui import QImage, QPixmap, QColor
from PyQt6.QtCore import Qt
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
    - Pixels present in both -> black
    - Pixels only in A -> color_a
    - Pixels only in B -> color_b
    - Background -> transparent

    'Present' means darker than threshold (i.e., it's ink, not paper).
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

    # "Ink" mask: pixel is dark (ink present) if luminance < 255 - threshold
    def ink_mask(arr):
        luminance = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
        return luminance < (255 - threshold)

    mask_a = ink_mask(arr_a)
    mask_b = ink_mask(arr_b)

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
    result[both] = [rgb_shared[0], rgb_shared[1], rgb_shared[2], 255]

    # Only in A -> color_a
    only_a = mask_a & ~mask_b
    result[only_a] = [rgb_a[0], rgb_a[1], rgb_a[2], 255]

    # Only in B -> color_b
    only_b = mask_b & ~mask_a
    result[only_b] = [rgb_b[0], rgb_b[1], rgb_b[2], 255]

    # Background stays transparent (alpha=0 already)
    return Image.fromarray(result, "RGBA")


def render_single_colored(img: Image.Image, color: str, threshold: int = 30) -> Image.Image:
    """Render a single drawing page with ink in the specified color (for solo view)"""
    arr = np.array(img.convert("RGB")).astype(np.int16)
    luminance = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
    mask = luminance < (255 - threshold)

    def hex_to_rgb(hex_color):
        c = QColor(hex_color)
        return (c.red(), c.green(), c.blue())

    rgb = hex_to_rgb(color)
    h, w = mask.shape
    result = np.zeros((h, w, 4), dtype=np.uint8)
    result[mask] = [rgb[0], rgb[1], rgb[2], 255]
    return Image.fromarray(result, "RGBA")


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


def tesseract_available() -> bool:
    """True if pytesseract is importable and the Tesseract binary is reachable."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


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
