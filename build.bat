@echo off
setlocal

REM Build the Drawing Overlay Tool into a single Windows .exe.
REM
REM Before running, place (next to this file):
REM   - tesseract\       a portable Tesseract-OCR folder (optional, enables OCR)
REM   - app.ico          your application icon (optional)
REM
REM Output: dist\DrawingOverlay.exe

REM --- Find a working Python launcher (Store/python.org both supported) ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo ERROR: Could not find Python. Install it from python.org and tick
  echo        "Add python.exe to PATH", then re-run this script.
  pause
  exit /b 1
)
echo Using Python: %PY%

echo.
echo Installing/updating build dependencies...
%PY% -m pip install --upgrade pyinstaller PyQt6 PyMuPDF Pillow numpy pytesseract openpyxl psutil
if errorlevel 1 (
  echo ERROR: dependency install failed.
  pause
  exit /b 1
)

echo.
echo Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo Building (this can take a minute)...
REM Run PyInstaller as a module so it works even when its Scripts folder
REM isn't on PATH (the usual cause of "'pyinstaller' is not recognized").
%PY% -m PyInstaller DrawingOverlay.spec --noconfirm
if errorlevel 1 (
  echo.
  echo BUILD FAILED. Scroll up to see the error.
  pause
  exit /b 1
)

echo.
echo Done. Your executable is at: dist\DrawingOverlay.exe
pause
