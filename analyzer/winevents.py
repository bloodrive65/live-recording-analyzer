"""Read relevant Windows Event Log entries for a time window."""
from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# (provider substring, event id or None) -> plain-English meaning
KNOWN = [
    ("nvlddmkm", 153, "NVIDIA driver reset the GPU (TDR) — the GPU stopped responding briefly"),
    ("nvlddmkm", 13, "NVIDIA driver graphics exception"),
    ("nvlddmkm", 14, "NVIDIA driver graphics exception"),
    ("nvlddmkm", None, "NVIDIA display driver error"),
    ("Display", 4101, "Display driver stopped responding and recovered (TDR)"),
    ("amdkmdag", None, "AMD display driver error"),
    ("WHEA-Logger", None, "Hardware error reported (CPU / PCIe / memory)"),
    ("Kernel-Power", 41, "System lost power or restarted unexpectedly"),
    ("disk", 7, "Disk has a bad block"),
    ("disk", 51, "Error during a disk paging operation"),
    ("disk", 153, "Disk I/O was retried — the drive was slow to respond"),
    ("disk", 157, "Disk was surprise-removed (USB/enclosure disconnect?)"),
    ("storahci", 129, "Storage controller reset — drive stopped responding"),
    ("stornvme", 129, "NVMe controller reset — drive stopped responding"),
    ("stornvme", None, "NVMe storage warning"),
    ("UASPStor", None, "USB storage (UASP) problem — external enclosure"),
    ("USBHUB3", None, "USB hub/port problem"),
    ("Kernel-PnP", 225, "A device could not be stopped/removed"),
    ("Ntfs", 55, "File-system corruption detected"),
    ("Ntfs", 98, "Volume needs a disk check"),
    ("Ntfs", 140, "File system failed to write data (delayed write)"),
    ("Ntfs", 50, "Delayed write failed — data may have been lost"),
    ("volmgr", None, "Volume manager warning"),
    ("Microsoft-Windows-SMBClient", None, "Network share (SMB) problem"),
    ("Application Error", 1000, "An application crashed"),
    ("Application Hang", 1002, "An application stopped responding"),
    ("Resource-Exhaustion-Detector", 2004, "Windows was low on memory"),
    ("WindowsUpdateClient", None, "Windows Update activity"),
    ("Defender", None, "Microsoft Defender activity"),
]


def _meaning(provider: str, eid: int) -> str:
    for prov, kid, text in KNOWN:
        if prov.lower() in provider.lower() and (kid is None or kid == eid):
            return text
    return ""


def query(start: datetime, end: datetime, logs=("System", "Application")) -> list[dict]:
    if sys.platform != "win32":
        return []
    out = []
    s = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    e = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    q = f"*[System[(Level=1 or Level=2 or Level=3) and TimeCreated[@SystemTime>='{s}' and @SystemTime<='{e}']]]"
    for log in logs:
        try:
            r = subprocess.run(["wevtutil", "qe", log, f"/q:{q}", "/f:RenderedXml", "/e:Events", "/c:400"],
                               capture_output=True, text=True, errors="replace", timeout=60,
                               creationflags=CREATE_NO_WINDOW)
        except Exception:
            continue
        txt = r.stdout.strip()
        if not txt:
            continue
        try:
            root = ET.fromstring(txt)
        except ET.ParseError:
            continue
        ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
        for ev in root.findall("e:Event", ns):
            sysn = ev.find("e:System", ns)
            prov = sysn.find("e:Provider", ns).get("Name", "")
            eid = int((sysn.find("e:EventID", ns).text or "0").strip() or 0)
            lvl = int((sysn.find("e:Level", ns).text or "4").strip() or 4)
            tc = sysn.find("e:TimeCreated", ns).get("SystemTime", "")
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?", tc)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if m.group(2):
                ts += timedelta(microseconds=int((m.group(2)[1:] + "000000")[:6]))
            ts = ts.astimezone().replace(tzinfo=None)
            msg_el = ev.find("e:RenderingInfo/e:Message", ns)
            msg = (msg_el.text or "").strip() if msg_el is not None else ""
            meaning = _meaning(prov, eid)
            # Skip noisy, irrelevant entries
            if not meaning and lvl == 3 and log == "Application":
                continue
            out.append({"ts": ts, "log": log, "provider": prov, "id": eid,
                        "level": {1: "critical", 2: "error", 3: "warning"}.get(lvl, "info"),
                        "meaning": meaning, "message": msg[:400]})
    out.sort(key=lambda x: x["ts"])
    return out
