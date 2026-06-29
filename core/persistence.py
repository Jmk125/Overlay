"""
Save and load overlay project state
"""
import json
import os
from core.models import OverlaySet, OverlayPair, DrawingPage


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
        }

    data = {
        'version': 1,
        'set_a_label': overlay_set.set_a_label,
        'set_b_label': overlay_set.set_b_label,
        'color_a': overlay_set.color_a,
        'color_b': overlay_set.color_b,
        'shared_color': overlay_set.shared_color,
        'canvas_bg': overlay_set.canvas_bg,
        'render_dpi': overlay_set.render_dpi,
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
        render_dpi=data.get('render_dpi', 150),
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
        )
        overlay_set.pairs.append(pair)

    overlay_set.unmatched_a = [dict_to_page(d) for d in data.get('unmatched_a', [])]
    overlay_set.unmatched_b = [dict_to_page(d) for d in data.get('unmatched_b', [])]

    return overlay_set


def load_settings(settings_path: str) -> dict:
    defaults = {
        'default_color_a': '#FF0000',
        'default_color_b': '#0000FF',
        'render_dpi': 150,
        'export_path': os.path.expanduser('~/Desktop'),
        'last_open_dir': os.path.expanduser('~'),
        'ink_threshold': 30,
        # ── Viewer controls (configurable in Preferences) ──
        'zoom_on_scroll': True,    # True = plain scroll zooms; False = require Ctrl+scroll
        'pan_button': 'right',     # which mouse button pans: 'left' | 'middle' | 'right'
        'antialiasing': True,      # smooth scaled drawings to soften low-DPI edges
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
