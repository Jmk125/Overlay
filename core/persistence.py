"""
Save and load overlay project state
"""
import json
import os
from core.models import OverlaySet, OverlayPair, DrawingPage, WorkspaceDrawing


def save_project(overlay_set: OverlaySet, filepath: str):
    """Save overlay project to a .overlay JSON file"""
    def page_to_dict(p: DrawingPage):
        return {
            'pdf_path': p.pdf_path,
            'page_index': p.page_index,
            'sheet_number': p.sheet_number,
            'display_name': p.display_name,
        }

    def pair_to_dict(pair: OverlayPair):
        return {
            'page_a': page_to_dict(pair.page_a),
            'page_b': page_to_dict(pair.page_b),
            'pair_id': pair.pair_id,
            'offset_x': pair.offset_x,
            'offset_y': pair.offset_y,
            'rotation': pair.rotation,
            'pivot_x': pair.pivot_x,
            'pivot_y': pair.pivot_y,
            'scale_factor': pair.scale_factor,
            'scale_a': pair.scale_a,
            'scale_b': pair.scale_b,
            'markups': pair.markups,
            'notes': pair.notes,
        }

    def workspace_to_dict(d: WorkspaceDrawing):
        return {
            'page': page_to_dict(d.page),
            'name': d.name,
            'drawing_id': d.drawing_id,
            'color': d.color,
            'offset_x': d.offset_x,
            'offset_y': d.offset_y,
            'rotation': d.rotation,
            'scale_factor': d.scale_factor,
            'scale_str': d.scale_str,
            'visible': d.visible,
            'erase_rects': d.erase_rects,
            'erase_bg': d.erase_bg,
        }

    data = {
        'version': 2,
        'set_a_label': overlay_set.set_a_label,
        'set_b_label': overlay_set.set_b_label,
        'color_a': overlay_set.color_a,
        'color_b': overlay_set.color_b,
        'shared_color': overlay_set.shared_color,
        'canvas_bg': overlay_set.canvas_bg,
        'render_dpi': overlay_set.render_dpi,
        'export_dpi': overlay_set.export_dpi,
        'workspace_mode': overlay_set.workspace_mode,
        'workspace_ref_scale': overlay_set.workspace_ref_scale,
        'workspace_drawings': [workspace_to_dict(d) for d in overlay_set.workspace_drawings],
        'pairs': [pair_to_dict(p) for p in overlay_set.pairs],
        'unmatched_a': [page_to_dict(p) for p in overlay_set.unmatched_a],
        'unmatched_b': [page_to_dict(p) for p in overlay_set.unmatched_b],
    }
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)


def load_project(filepath: str) -> OverlaySet:
    """Load overlay project from a .overlay JSON file"""
    with open(filepath, 'r') as f:
        data = json.load(f)

    def dict_to_page(d):
        return DrawingPage(
            pdf_path=d['pdf_path'],
            page_index=d['page_index'],
            sheet_number=d.get('sheet_number', ''),
            display_name=d.get('display_name', ''),
        )

    overlay_set = OverlaySet(
        set_a_label=data.get('set_a_label', 'Set A'),
        set_b_label=data.get('set_b_label', 'Set B'),
        color_a=data.get('color_a', '#FF0000'),
        color_b=data.get('color_b', '#0000FF'),
        shared_color=data.get('shared_color', '#000000'),
        canvas_bg=data.get('canvas_bg', 'white'),
        render_dpi=data.get('render_dpi', 120),
        export_dpi=data.get('export_dpi', 200),
        workspace_mode=data.get('workspace_mode', False),
        workspace_ref_scale=data.get('workspace_ref_scale', ''),
    )

    for pd in data.get('pairs', []):
        pair = OverlayPair(
            page_a=dict_to_page(pd['page_a']),
            page_b=dict_to_page(pd['page_b']),
            pair_id=pd.get('pair_id', ''),
            offset_x=pd.get('offset_x', 0.0),
            offset_y=pd.get('offset_y', 0.0),
            rotation=pd.get('rotation', 0.0),
            pivot_x=pd.get('pivot_x', 0.5),
            pivot_y=pd.get('pivot_y', 0.5),
            scale_factor=pd.get('scale_factor', 1.0),
            scale_a=pd.get('scale_a', ''),
            scale_b=pd.get('scale_b', ''),
            markups=pd.get('markups', []) or [],
            notes=pd.get('notes', '') or '',
        )
        overlay_set.pairs.append(pair)

    for wd in data.get('workspace_drawings', []):
        overlay_set.workspace_drawings.append(WorkspaceDrawing(
            page=dict_to_page(wd['page']),
            name=wd.get('name', ''),
            drawing_id=wd.get('drawing_id', ''),
            color=wd.get('color', '#000000'),
            offset_x=wd.get('offset_x', 0.0),
            offset_y=wd.get('offset_y', 0.0),
            rotation=wd.get('rotation', 0.0),
            scale_factor=wd.get('scale_factor', 1.0),
            scale_str=wd.get('scale_str', ''),
            visible=wd.get('visible', True),
            erase_rects=wd.get('erase_rects', []) or [],
            erase_bg=wd.get('erase_bg', 'white'),
        ))

    overlay_set.unmatched_a = [dict_to_page(d) for d in data.get('unmatched_a', [])]
    overlay_set.unmatched_b = [dict_to_page(d) for d in data.get('unmatched_b', [])]

    return overlay_set


def export_notes(overlay_set, filepath: str):
    """Write per-drawing notes to a spreadsheet.

    Each row is (drawing identifier, notes). The identifier is the sheet number
    (OCR'd or set by page-order matching) when present, else the pair id. Writes
    .xlsx via openpyxl when available, otherwise falls back to .csv.
    Returns the path actually written.
    """
    rows = []
    for i, pair in enumerate(overlay_set.pairs):
        ident = (pair.page_a.sheet_number or pair.page_b.sheet_number
                 or pair.pair_id or f"Pair {i + 1}")
        rows.append((ident, pair.notes or ""))

    ext = os.path.splitext(filepath)[1].lower()
    if ext != '.csv':
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "Notes"
            ws.append(["Drawing", "Notes"])
            for ident, notes in rows:
                ws.append([ident, notes])
            ws.column_dimensions['A'].width = 24
            ws.column_dimensions['B'].width = 80
            if not filepath.lower().endswith('.xlsx'):
                filepath += '.xlsx'
            wb.save(filepath)
            return filepath
        except ImportError:
            # openpyxl missing — write CSV instead (Excel opens it fine).
            filepath = os.path.splitext(filepath)[0] + '.csv'

    import csv
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(["Drawing", "Notes"])
        for ident, notes in rows:
            writer.writerow([ident, notes])
    return filepath


def load_settings(settings_path: str) -> dict:
    defaults = {
        'default_color_a': '#FF0000',
        'default_color_b': '#0000FF',
        'render_dpi': 120,          # on-screen working DPI (lower = faster)
        'export_dpi': 200,          # DPI used when exporting PNG/PDF
        'export_path': os.path.expanduser('~/Desktop'),
        'last_open_dir': os.path.expanduser('~'),
        'ink_threshold': 30,
        # ── Viewer controls (configurable in Preferences) ──
        'zoom_on_scroll': True,    # True = plain scroll zooms; False = require Ctrl+scroll
        'pan_button': 'right',     # which mouse button pans: 'left' | 'middle' | 'right'
        'antialiasing': True,      # smooth scaled drawings to soften low-DPI edges
        'canvas_bg': 'white',      # default canvas background: 'white' | 'dark'
        'tesseract_path': '',      # optional path to tesseract.exe or its folder
        'update_server_url': 'http://10.0.10.180:3090/',
        'check_for_updates': True,
    }
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r') as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
    return defaults


def save_settings(settings: dict, settings_path: str):
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, 'w') as f:
        json.dump(settings, f, indent=2)
