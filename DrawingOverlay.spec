# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Drawing Overlay Tool.

Builds a single-file, windowed Windows .exe. It automatically bundles:
  - a portable Tesseract folder if a `tesseract/` (or `Tesseract-OCR/`)
    directory is present next to this spec, and
  - `app.ico` as the executable icon and window icon, if present.

Build with:   pyinstaller DrawingOverlay.spec
Output:       dist/DrawingOverlay.exe
"""
import os

block_cipher = None

# Bundle the portable Tesseract folder (and the app icon) if they exist.
datas = []
for _tess in ('tesseract', 'Tesseract-OCR'):
    if os.path.isdir(_tess):
        datas.append((_tess, _tess))
        break
if os.path.exists('app.ico'):
    datas.append(('app.ico', '.'))

icon = 'app.ico' if os.path.exists('app.ico') else None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    # These are imported lazily / inside try-except, so list them explicitly to
    # guarantee they're bundled:
    #   psutil     -> "From open PDF" (detect sheets open in Bluebeam/Acrobat)
    #   pytesseract-> OCR sheet matching
    #   openpyxl   -> Export Notes to .xlsx
    hiddenimports=['psutil', 'pytesseract', 'openpyxl'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DrawingOverlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed app (no console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
