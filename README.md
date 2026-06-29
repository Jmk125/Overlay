# Drawing Overlay Tool

A professional PDF drawing overlay tool built for construction workflows.
Compare drawing revisions with intelligent overlay, alignment, rotation, and version toggling.

## Features

- **Batch or single overlay** — load individual PDFs or multi-page drawing sets
- **OCR sheet matching** — draw a box around the title block; app auto-matches by sheet number
- **Unmatched queue** — manually pair any sheets that couldn't be auto-matched
- **Color compositing** — shared lines = black; Set A only = your color; Set B only = your color
- **Version toggling** — hotkeys 1/2/3 to switch between overlay, A-only, B-only views
- **Click & drag alignment** — move Drawing B with left-click drag; Shift for fine movement
- **Rotation** — quick 90°/45° buttons, or free-rotate with click-drag around adjustable pivot point
- **Auto-scale** — enter drawing scale (e.g. 1/4" = 1') for each set; B is auto-resized to match A
- **Save/load project** — .overlay JSON files preserve all transform state
- **Export** — PNG or PDF export of current overlay

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1` | Show overlay (both drawings) |
| `2` | Show Set A only |
| `3` | Show Set B only |
| `F` | Fit to window |
| `Ctrl+Scroll` | Zoom in/out |
| `Right-click drag` | Pan canvas |
| `Shift+drag` | Fine movement or rotation |
| `Ctrl+N` | New overlay |
| `Ctrl+O` | Open project |
| `Ctrl+S` | Save project |

## Setup

### Requirements
- Python 3.10+
- PyQt6
- PyMuPDF (fitz)
- Pillow
- pytesseract + Tesseract OCR (for sheet number auto-matching)

### Install dependencies

```bash
pip install PyQt6 PyMuPDF Pillow pytesseract
```

**Tesseract OCR** (needed for auto sheet matching):
- Windows: https://github.com/UB-Mannheim/tesseract/wiki
- After install, ensure `tesseract` is on your PATH

### Run

```bash
cd drawing_overlay
python main.py
```

### Package to .exe (Windows)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "DrawingOverlay" main.py
```

The .exe will be in the `dist/` folder.

## Project Structure

```
drawing_overlay/
├── main.py              # App entry point, main window
├── core/
│   ├── models.py        # Data models (DrawingPage, OverlayPair, OverlaySet)
│   ├── renderer.py      # PDF rendering, transform, compositing
│   └── persistence.py   # Save/load .overlay projects and settings
└── ui/
    ├── landing.py       # New overlay / open project screen
    ├── page_selector.py # PDF page picker with thumbnails
    ├── matching.py      # Sheet matching screen (OCR + manual queue)
    └── viewer.py        # Main overlay viewer with all tools
```

## Settings

Settings are saved to `~/.drawing_overlay/settings.json` and include:
- Default colors for Set A and Set B
- Render DPI
- Default export path
- Last open directory
- Ink detection threshold
