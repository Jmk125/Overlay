"""
Core data models for Drawing Overlay Tool
"""
from dataclasses import dataclass, field
import uuid
from typing import Optional
from PyQt6.QtGui import QColor


@dataclass
class DrawingPage:
    """Represents a single page from a PDF set"""
    pdf_path: str
    page_index: int          # 0-based index into PDF
    sheet_number: str = ""   # OCR'd or manually entered
    display_name: str = ""   # friendly label

    def __post_init__(self):
        if not self.display_name:
            self.display_name = f"Page {self.page_index + 1}"


@dataclass
class OverlayPair:
    """A matched pair of pages to overlay"""
    page_a: DrawingPage
    page_b: DrawingPage
    pair_id: str = ""

    # Transform for page B relative to page A (page A is the anchor)
    offset_x: float = 0.0       # pixels at base render DPI
    offset_y: float = 0.0
    rotation: float = 0.0       # degrees
    pivot_x: float = 0.5        # normalized 0-1 (relative to page B bounds)
    pivot_y: float = 0.5
    scale_factor: float = 1.0   # derived from scale strings

    # Scale strings e.g. "1/8\" = 1'"
    scale_a: str = ""
    scale_b: str = ""

    # User annotations on this pair.
    # Each markup: {'type': 'line'|'rect'|'cloud', 'points': [[nx,ny],...],
    #               'color': '#RRGGBB', 'width': float}  (coords normalized 0-1)
    markups: list = field(default_factory=list)
    notes: str = ""

    def __post_init__(self):
        if not self.pair_id:
            self.pair_id = f"{self.page_a.sheet_number}_{self.page_b.sheet_number}"


@dataclass
class WorkspaceDrawing:
    """One independently adjustable drawing on an empty workspace canvas."""
    page: DrawingPage
    name: str = ""
    drawing_id: str = ""
    color: str = "#000000"
    offset_x: float = 0.0
    offset_y: float = 0.0
    rotation: float = 0.0
    scale_factor: float = 1.0
    erase_rects: list = field(default_factory=list)
    erase_bg: str = "white"

    def __post_init__(self):
        if not self.drawing_id:
            self.drawing_id = uuid.uuid4().hex
        if not self.name:
            self.name = self.page.display_name


@dataclass
class OverlaySet:
    """The full overlay project state"""
    set_a_label: str = "Set A"
    set_b_label: str = "Set B"
    color_a: str = "#FF0000"    # hex color for set A unique content
    color_b: str = "#0000FF"    # hex color for set B unique content
    shared_color: str = "#000000"  # color for lines present in both (black on white, white on dark)
    canvas_bg: str = "white"    # 'white' or 'dark'
    pairs: list = field(default_factory=list)
    unmatched_a: list = field(default_factory=list)
    unmatched_b: list = field(default_factory=list)
    render_dpi: int = 120      # on-screen working resolution (lower = faster)
    export_dpi: int = 200      # resolution used when exporting PNG/PDF
    workspace_mode: bool = False
    workspace_drawings: list = field(default_factory=list)


# Common architectural/engineering scales
COMMON_SCALES = [
    '1" = 1\'',
    '1/2" = 1\'',
    '1/4" = 1\'',
    '1/8" = 1\'',
    '3/32" = 1\'',
    '1/16" = 1\'',
    '3/16" = 1\'',
    '3/8" = 1\'',
    '3/4" = 1\'',
    '1:10',
    '1:20',
    '1:50',
    '1:100',
    '1:200',
    'NTS',
    'Custom...',
]


def parse_scale_to_ratio(scale_str: str) -> Optional[float]:
    """
    Convert a scale string to a pixels-per-foot ratio multiplier.
    Returns None if scale is NTS or unparseable.
    E.g. '1/4" = 1\'' -> 0.25 (inches per foot, used for relative comparison)
    """
    if not scale_str or scale_str == 'NTS' or scale_str == 'Custom...':
        return None
    try:
        # Handle "X" = Y'" format
        if '=' in scale_str and "'" in scale_str:
            left = scale_str.split('=')[0].strip().replace('"', '')
            # Evaluate fraction like 1/4
            if '/' in left:
                num, den = left.split('/')
                return float(num) / float(den)
            else:
                return float(left)
        # Handle ratio format 1:100
        if ':' in scale_str:
            parts = scale_str.split(':')
            return float(parts[0]) / float(parts[1])
    except Exception:
        pass
    return None


def compute_scale_factor(scale_a: str, scale_b: str) -> float:
    """
    Compute the scale factor to apply to drawing B so it matches drawing A's real-world size.
    Returns 1.0 if either scale is unparseable.
    """
    ratio_a = parse_scale_to_ratio(scale_a)
    ratio_b = parse_scale_to_ratio(scale_b)
    if ratio_a and ratio_b and ratio_b != 0:
        return ratio_a / ratio_b
    return 1.0
