"""
Detect PDFs currently open in other applications (Bluebeam, Acrobat, ...).

A running viewer keeps a file handle on the PDF it has open, so we can list
those handles with psutil and let the user import the real file — no dragging,
no rasterizing. psutil is an optional dependency; everything degrades gracefully
when it isn't installed.
"""
import os

try:
    import psutil
except Exception:  # pragma: no cover - optional dep
    psutil = None

# Substrings matched (case-insensitive) against process names of known PDF apps.
VIEWER_HINTS = (
    'revu', 'bluebeam', 'acrobat', 'acrord32', 'acrodist', 'foxit',
    'sumatra', 'pdfxe', 'nitro', 'drawboard', 'xchange',
)


def psutil_available() -> bool:
    return psutil is not None


def _collect_from(proc, out: list, seen: set):
    """Append any open .pdf files held by proc to out (deduped by path)."""
    try:
        files = proc.open_files()
    except Exception:
        return
    for f in files:
        path = getattr(f, 'path', None)
        if not path or not path.lower().endswith('.pdf'):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            app = proc.name()
        except Exception:
            app = '?'
        out.append({'path': path, 'app': app, 'pid': proc.pid})


def list_open_pdfs() -> list:
    """Return [{'path','app','pid'}] for PDFs open in other apps.

    Scans known PDF viewers first (fast); if that finds nothing, falls back to
    scanning every accessible process. Returns [] if psutil is unavailable.
    """
    if psutil is None:
        return []
    out, seen = [], set()
    try:
        procs = list(psutil.process_iter(['name']))
    except Exception:
        return out

    def name_of(pr):
        try:
            return (pr.info.get('name') or '').lower()
        except Exception:
            return ''

    # Phase 1: known viewers only.
    for pr in procs:
        if any(h in name_of(pr) for h in VIEWER_HINTS):
            _collect_from(pr, out, seen)

    # Phase 2: nothing from known viewers — scan everything accessible.
    if not out:
        for pr in procs:
            _collect_from(pr, out, seen)

    out.sort(key=lambda d: os.path.basename(d['path']).lower())
    return out
