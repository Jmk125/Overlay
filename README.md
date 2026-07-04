# Drawing Overlay Tool

A professional PDF drawing overlay tool built for construction workflows.
Compare drawing revisions with intelligent overlay, alignment, rotation, and version toggling.

## Features

- **Batch or single overlay** — load individual PDFs or multi-page drawing sets; for large sets the page picker opens instantly and lets you type a page range (e.g. `1-20, 45`) instead of waiting on thumbnails
- **Three matching modes** — auto-match by **OCR** (zoom/pan the page, draw a box around the sheet number), match by **PDF page order**, or pair **manually**
- **Unmatched queue** — click any unread/unmatched sheet to re-draw its OCR box (or edit its number); it re-matches automatically when numbers line up
- **Color compositing** — shared lines render black on a white canvas (white on a dark canvas); Set A only = your color; Set B only = your color
- **Version toggling** — hotkeys 1/2/3 to switch between overlay, A-only, B-only views
- **Real-time alignment** — Drawing B moves and rotates live under the cursor (GPU-accelerated layer transforms); the full color composite is recomputed only when you release
- **Fast batch navigation** — rendered pages are cached (instant when you return to one) and nearby pages are pre-rendered in the background so they're ready before you reach them
- **Activity indicator** — a slim progress bar under the canvas shows whenever the current view is rendering
- **Click & drag alignment** — move Drawing B with left-click drag; Shift for fine movement
- **Rotation** — quick 90°/45° buttons, or free-rotate with click-drag around adjustable pivot point
- **Collapsible UI** — collapse the pairs pane, the tools pane, and each tool section (View / Align / Rotation / Scale / Export) to keep the workspace clean
- **Auto-scale** — enter drawing scale (e.g. 1/4" = 1') for each set; B is auto-resized to match A
- **Markups** — draw lines, boxes and revision clouds in any color and line weight; select to move or delete individual marks; per drawing, undo/clear
- **Per-drawing notes** — jot notes for each sheet; retained when you switch drawings and saved in the project
- **Save/load project** — .overlay JSON files preserve all transform state, markups and notes
- **Export** — PNG or PDF export of current overlay (optionally with markups burned in)
- **Export notes** — File ▸ Export Notes writes an Excel/CSV sheet of notes keyed by sheet number
- **Drag & drop** — drop PDF files, image files, a dragged image/snapshot, or (where the app supports it) a page dragged straight out of Bluebeam/Acrobat onto the Set A / Set B panels
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
- openpyxl (optional — for Export Notes to .xlsx; without it, notes export as .csv)
- psutil (optional — for "From open PDF", which imports a sheet already open in Bluebeam/Acrobat)

### Install dependencies

```bash
pip install PyQt6 PyMuPDF Pillow pytesseract openpyxl psutil
```

**Tesseract OCR** (needed for auto sheet matching):
- Windows: https://github.com/UB-Mannheim/tesseract/wiki
- After install, ensure `tesseract` is on your PATH
- The matching screen shows a **live read preview** after you draw the box — if
  Tesseract isn't found it says so there, and you can still use
  *Skip OCR — Match Manually*. (OCR reads from the source PDF at 300 DPI, so it
  doesn't depend on the on-screen preview resolution — just on a tight box.)

### Run

```bash
cd drawing_overlay
python main.py
```

### Package to a single .exe (Windows)

A ready-made PyInstaller spec (`DrawingOverlay.spec`) is included. It bundles
Tesseract and the icon automatically when they're present.

**1. (Optional) Add a portable Tesseract** so OCR works with no install on the
user's machine. Tesseract isn't a single file — `tesseract.exe` needs its DLLs
and a `tessdata/` language folder — but the whole folder is **portable**:

  1. Install the [UB-Mannheim Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki)
     on **any one** Windows machine.
  2. Copy the entire install folder (`C:\Program Files\Tesseract-OCR`) into this
     project as a folder named **`tesseract/`** (so you have
     `tesseract/tesseract.exe` and `tesseract/tessdata/`).
  3. To shrink it, delete everything in `tessdata/` except `eng.traineddata`
     (and `osd.traineddata`).

**2. (Optional) Add your icon** — drop `app.ico` in the project root. It becomes
both the .exe icon and the window/taskbar icon.

**3. Build** — either run the helper script:

```bat
build.bat
```

or directly:

```bash
python -m pip install pyinstaller PyQt6 PyMuPDF Pillow numpy pytesseract openpyxl
python -m PyInstaller DrawingOverlay.spec
```

The result is **`dist/DrawingOverlay.exe`** — a single self-contained file.

> **`'pyinstaller' is not recognized`?** Its Scripts folder isn't on your PATH
> (common with the Microsoft Store build of Python). Run it as a module instead:
> `python -m PyInstaller DrawingOverlay.spec` (note the capital `PyInstaller`).
> `build.bat` already does this for you.

The app **auto-detects** the bundled `tesseract/` folder (and `app.ico`) inside
the packaged exe; no configuration needed. Users can still point to a custom
Tesseract in **Edit ▸ Preferences ▸ OCR**, which shows a green check when found.

> One-liner equivalent (without the spec). Windows uses `;` as the `--add-data`
> separator:
>
> ```bash
> python -m PyInstaller --onefile --windowed --name "DrawingOverlay" ^
>   --icon app.ico --add-data "tesseract;tesseract" --add-data "app.ico;." main.py
> ```

**Notes**
- `--onefile` unpacks the bundle (including Tesseract) to a temp folder on each
  launch, so first start is a little slower. For faster startup, change
  `DrawingOverlay.spec` to a one-folder build (a `COLLECT` step) — the
  `tesseract/` folder then sits next to the exe and isn't re-extracted.
- The build is platform-specific: build the Windows .exe on Windows.

## Project Structure

```
drawing_overlay/
├── main.py              # App entry point, main window
├── DrawingOverlay.spec  # PyInstaller build spec (bundles Tesseract + icon)
├── build.bat            # One-click Windows build helper
├── core/
│   ├── models.py        # Data models (DrawingPage, OverlayPair, OverlaySet)
│   ├── renderer.py      # PDF rendering, transform, compositing, markups, OCR
│   └── persistence.py   # Save/load .overlay projects, settings, notes export
└── ui/
    ├── landing.py       # New overlay / open project screen (drag & drop)
    ├── page_selector.py # PDF page picker with thumbnails
    ├── matching.py      # Sheet matching screen (OCR + manual queue)
    ├── settings_dialog.py # Preferences (controls, rendering, colors, OCR)
    ├── collapsible.py   # Collapsible section widget for the tools pane
    └── viewer.py        # Main overlay viewer (align, markups, notes, export)
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
