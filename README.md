# Drawing Overlay Tool

A professional PDF drawing overlay tool built for construction workflows.
Compare drawing revisions with intelligent overlay, alignment, rotation, and version toggling.

## Features

- **Batch or single overlay** — load individual PDFs or multi-page drawing sets
- **OCR sheet matching** — zoom/pan the sample page and draw a box around the title block; app auto-matches by sheet number
- **Unmatched queue** — manually pair any sheets that couldn't be auto-matched
- **Color compositing** — shared lines render black on a white canvas (white on a dark canvas); Set A only = your color; Set B only = your color
- **Version toggling** — hotkeys 1/2/3 to switch between overlay, A-only, B-only views
- **Real-time alignment** — Drawing B moves and rotates live under the cursor (GPU-accelerated layer transforms); the full color composite is recomputed only when you release
- **Fast batch navigation** — rendered pages are cached (instant when you return to one) and nearby pages are pre-rendered in the background so they're ready before you reach them
- **Activity indicator** — a slim progress bar under the canvas shows whenever the current view is rendering
- **Click & drag alignment** — move Drawing B with left-click drag; Shift for fine movement
- **Rotation** — quick 90°/45° buttons, or free-rotate with click-drag around adjustable pivot point
- **Collapsible UI** — collapse the pairs pane, the tools pane, and each tool section (View / Align / Rotation / Scale / Export) to keep the workspace clean
- **Auto-scale** — enter drawing scale (e.g. 1/4" = 1') for each set; B is auto-resized to match A
- **Save/load project** — .overlay JSON files preserve all transform state
- **Export** — PNG or PDF export of current overlay
- **Drag & drop** — drop PDF files straight onto the Set A / Set B panels
- **Customizable controls** — set zoom (scroll vs. Ctrl+scroll), pan button (left/middle/right), and edge antialiasing in Preferences

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1` | Show overlay (both drawings) |
| `2` | Show Set A only |
| `3` | Show Set B only |
| `F` | Fit to window |
| `Scroll` | Zoom in/out *(configurable: scroll or Ctrl+scroll)* |
| `Right-click drag` | Pan canvas *(configurable: left/middle/right)* |
| `Shift+drag` | Fine movement or rotation |
| `Ctrl+N` | New overlay |
| `Ctrl+O` | Open project |
| `Ctrl+S` | Save project |
| `Ctrl+,` | Preferences |

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
    ├── landing.py       # New overlay / open project screen (drag & drop)
    ├── page_selector.py # PDF page picker with thumbnails
    ├── matching.py      # Sheet matching screen (OCR + manual queue)
    ├── settings_dialog.py # Preferences (controls, rendering, colors)
    ├── collapsible.py   # Collapsible section widget for the tools pane
    └── viewer.py        # Main overlay viewer with all tools
```

## Settings

Open **Edit ▸ Preferences** (`Ctrl+,`) to customize controls and rendering.
Settings are saved to `~/.drawing_overlay/settings.json` and include:
- Default colors for Set A and Set B
- Render DPI
- Zoom behavior (scroll to zoom, or require Ctrl+scroll)
- Pan mouse button (left / middle / right)
- Edge antialiasing (smooths jagged lines when zooming low-DPI renders)
- Canvas background (white or dark)
- Default export path
- Last open directory
- Ink detection threshold
