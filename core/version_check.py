"""Application update checking helpers."""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin

APP_VERSION = "1.0.0"
DEFAULT_UPDATE_SERVER = "http://10.0.10.180:3090/"
APP_SLUGS = ("drawing-overlay-tool", "drawing_overlay_tool", "overlay", "drawing-overlay")
APP_NAMES = ("drawing overlay tool", "overlay tool")


@dataclass
class UpdateInfo:
    version: str
    changelog: str = ""
    download_url: str = ""
    source_url: str = ""


def _version_tuple(value: str) -> tuple:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def is_newer_version(latest: str, current: str = APP_VERSION) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def _candidate_urls(base_url: str) -> list[str]:
    base = (base_url or DEFAULT_UPDATE_SERVER).strip() or DEFAULT_UPDATE_SERVER
    if not base.endswith("/"):
        base += "/"
    candidates = [
        base,
        urljoin(base, "version.json"),
        urljoin(base, "versions.json"),
        urljoin(base, "manifest.json"),
        urljoin(base, "api/tools/drawing-overlay-tool"),
        urljoin(base, "api/tools/drawing-overlay-tool/latest"),
        urljoin(base, "api/tools/drawing-overlay-tool/versions/latest"),
        urljoin(base, "tools/drawing-overlay-tool.json"),
    ]
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(candidates))


def _get_any(data: dict, *keys, default=""):
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def _looks_like_overlay_tool(data: dict) -> bool:
    text = " ".join(str(_get_any(data, "slug", "id", "name", "title", "app", default="")).lower().split())
    return any(slug in text for slug in APP_SLUGS) or any(name in text for name in APP_NAMES)


def _extract_from_release(data: dict, source_url: str) -> UpdateInfo | None:
    version = str(_get_any(data, "version", "latest_version", "latestVersion", "tag", "tag_name", default="")).strip()
    if not version:
        return None
    changelog = _get_any(
        data,
        "changelog", "changeLog", "changes", "release_notes", "releaseNotes", "notes", "description",
        default="",
    )
    if isinstance(changelog, list):
        changelog = "\n".join(str(item) for item in changelog)
    download_url = str(_get_any(data, "download_url", "downloadUrl", "url", "installer", "asset_url", default="")).strip()
    return UpdateInfo(version=version, changelog=str(changelog or ""), download_url=download_url, source_url=source_url)


def _extract_update_info(data, source_url: str) -> UpdateInfo | None:
    if isinstance(data, dict):
        direct = _extract_from_release(data, source_url)
        if direct and (_looks_like_overlay_tool(data) or "version" in data or "latest_version" in data or "latestVersion" in data):
            return direct

        for key in ("latest", "release", "current", "app", "tool"):
            nested = data.get(key)
            if isinstance(nested, dict):
                found = _extract_update_info(nested, source_url)
                if found:
                    return found

        for key in ("tools", "apps", "utilities", "projects", "items", "versions", "releases"):
            items = data.get(key)
            if isinstance(items, list):
                found = _extract_update_info(items, source_url)
                if found:
                    return found

    if isinstance(data, list):
        matching = [item for item in data if isinstance(item, dict) and _looks_like_overlay_tool(item)]
        for item in matching or [item for item in data if isinstance(item, dict)]:
            found = _extract_update_info(item, source_url)
            if found:
                return found

    return None


def fetch_latest_update(base_url: str, timeout: float = 4.0) -> UpdateInfo | None:
    """Fetch update metadata from a small set of common manifest endpoints.

    The hosted JSON can be either a direct release object (for example
    {"version": "1.0.1", "changelog": "..."}) or a list/catalog containing a
    Drawing Overlay Tool entry.
    """
    last_error = None
    for url in _candidate_urls(base_url):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read(1_000_000).decode("utf-8", errors="replace")
            if "json" not in content_type.lower() and not raw.lstrip().startswith(("{", "[")):
                continue
            data = json.loads(raw)
            info = _extract_update_info(data, url)
            if info:
                return info
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return None
