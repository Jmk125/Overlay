"""
PDF rendering and overlay compositing engine
"""
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageChops
from PyQt6.QtGui import QImage, QPixmap, QColor
from PyQt6.QtCore import Qt
import io


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
    Apply rotation (around pivot), scale, and translation to an image.
    Returns a new image composited onto a transparent canvas of canvas_size.
    """
    w, h = img.size
    cw, ch = canvas_size

    # Scale first
    if abs(scale_factor - 1.0) > 0.001:
        new_w = int(w * scale_factor)
        new_h = int(h * scale_factor)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        w, h = img.size

    # Rotate around pivot point (pivot is normalized relative to image size)
    if abs(rotation) > 0.001:
        pivot_px = (pivot_x * w, pivot_y * h)
        img = img.rotate(-rotation, expand=True, center=pivot_px, resample=Image.BICUBIC)
        w, h = img.size

    # Composite onto canvas
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    paste_x = int(offset_x)
    paste_y = int(offset_y)
    canvas.paste(img, (paste_x, paste_y), img)
    return canvas


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


def ocr_region(pdf_path: str, page_index: int, rect_norm: tuple, dpi: int = 200) -> str:
    """
    OCR a normalized region (x1,y1,x2,y2 as 0-1 fractions of page) and return text.
    """
    try:
        import pytesseract
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
        # Upscale for better OCR
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        text = pytesseract.image_to_string(img, config='--psm 7').strip()
        return text
    except Exception as e:
        return ""
