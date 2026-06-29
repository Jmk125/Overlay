@echo off
REM Build the Drawing Overlay Tool into a single Windows .exe.
REM
REM Before running, place (next to this file):
REM   - tesseract\       a portable Tesseract-OCR folder (optional, enables OCR)
REM   - app.ico          your application icon (optional)
REM
REM Output: dist\DrawingOverlay.exe

echo Installing/updating build dependencies...
pip install pyinstaller PyQt6 PyMuPDF Pillow numpy pytesseract openpyxl

echo Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Building...
pyinstaller DrawingOverlay.spec --noconfirm

echo.
echo Done. Your executable is at: dist\DrawingOverlay.exe
pause
