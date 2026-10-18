"""Checks GitHub Releases for a newer version and downloads the installer."""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request

REPO = "bloodrive65/live-recording-analyzer"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"


def _ver(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:4]) + (0,) * (4 - len(nums[:4]))


def newer(remote: str, local: str) -> bool:
    return _ver(remote) > _ver(local)


def check(current: str, timeout: float = 10) -> dict:
    """Returns {"newer": bool, "version", "notes", "url", "size", "page"}. Raises on network errors."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"LiveRecordingAnalyzer/{current}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rel = json.loads(r.read().decode("utf-8"))
    tag = rel.get("tag_name") or ""
    asset = None
    for a in rel.get("assets") or []:
        n = a.get("name", "").lower()
        if n.endswith(".exe") and "setup" in n:
            asset = a
            break
    return {"newer": newer(tag, current) and asset is not None, "version": tag.lstrip("vV"),
            "notes": (rel.get("body") or "").strip(), "url": asset.get("browser_download_url") if asset else None,
            "size": asset.get("size", 0) if asset else 0, "name": asset.get("name") if asset else "",
            "page": rel.get("html_url") or RELEASES_PAGE}


def download(url: str, name: str, progress=None, cancel=None, timeout: float = 30) -> str:
    dest_dir = os.path.join(tempfile.gettempdir(), "LiveRecordingAnalyzer-update")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(name) or "setup.exe")
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "LiveRecordingAnalyzer-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if cancel and cancel():
                raise RuntimeError("Cancelled.")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress:
                progress(got, total)
    if total and os.path.getsize(tmp) != total:
        raise RuntimeError("The download was incomplete. Please try again.")
    with open(tmp, "rb") as f:
        if f.read(2) != b"MZ":
            raise RuntimeError("The downloaded file isn’t a Windows installer.")
    os.replace(tmp, dest)
    return dest
