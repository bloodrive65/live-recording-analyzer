"""Read OBS's current profile + scene collection straight from disk.

Works even when OBS is closed, so the pre-flight check can run before you start.
"""
from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass, field


def obs_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "obs-studio")


def _ini(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            cp.read_file(f)
    except Exception:
        pass
    return cp


def _get(cp, sec, key, default=""):
    try:
        return cp.get(sec, key, fallback=default)
    except Exception:
        return default


@dataclass
class ObsConfig:
    found: bool = False
    profile: str = ""
    profile_dir: str = ""
    scene_collection: str = ""
    output_mode: str = ""
    rec_path: str = ""
    rec_format: str = ""
    rec_encoder: str = ""
    rec_quality: str = ""
    rec_rescale: bool = False
    rec_rescale_res: str = ""
    encoder_settings: dict = field(default_factory=dict)
    base_res: str = ""
    output_res: str = ""
    fps: float = 0.0
    fps_label: str = ""
    color_format: str = "NV12"
    color_space: str = "709"
    color_range: str = "Partial"
    sample_rate: int = 48000
    capture_devices: list = field(default_factory=list)   # dicts
    screen_captures: list = field(default_factory=list)   # dicts
    browser_sources: int = 0
    websocket: dict = field(default_factory=dict)

    @property
    def range_label(self):
        return "Full" if self.color_range.lower() == "full" else "Limited"

    @property
    def space_label(self):
        return {"709": "Rec. 709", "601": "Rec. 601", "sRGB": "sRGB", "2100PQ": "Rec. 2100 (PQ)", "2100HLG": "Rec. 2100 (HLG)"}.get(self.color_space, self.color_space)


def _fps(cp) -> tuple[float, str]:
    t = _get(cp, "Video", "FPSType", "0")
    if t == "1":
        v = float(_get(cp, "Video", "FPSInt", "30") or 30)
        return v, f"{v:g}"
    if t == "2":
        n = float(_get(cp, "Video", "FPSNum", "30") or 30)
        d = float(_get(cp, "Video", "FPSDen", "1") or 1)
        return n / d, f"{n:g}/{d:g}"
    c = _get(cp, "Video", "FPSCommon", "30") or "30"
    try:
        return float(c.split()[0]), c
    except ValueError:
        return 30.0, c


def load() -> ObsConfig:
    cfg = ObsConfig()
    base = obs_dir()
    if not os.path.isdir(base):
        return cfg
    # current profile / scene collection (user.ini in OBS 31+, global.ini before)
    prof_name = prof_dir = scene_file = ""
    for f in ("user.ini", "global.ini"):
        cp = _ini(os.path.join(base, f))
        prof_name = prof_name or _get(cp, "Basic", "Profile")
        prof_dir = prof_dir or _get(cp, "Basic", "ProfileDir")
        scene_file = scene_file or _get(cp, "Basic", "SceneCollectionFile")
    profiles = os.path.join(base, "basic", "profiles")
    if not prof_dir and os.path.isdir(profiles):
        ds = sorted(os.listdir(profiles))
        prof_dir = ds[0] if ds else ""
    pdir = os.path.join(profiles, prof_dir)
    ini = os.path.join(pdir, "basic.ini")
    if not os.path.isfile(ini):
        return cfg
    cfg.found = True
    cfg.profile, cfg.profile_dir = prof_name or prof_dir, pdir
    cp = _ini(ini)
    cfg.output_mode = _get(cp, "Output", "Mode", "Simple")
    if cfg.output_mode.lower() == "advanced":
        cfg.rec_path = _get(cp, "AdvOut", "RecFilePath")
        cfg.rec_format = _get(cp, "AdvOut", "RecFormat2") or _get(cp, "AdvOut", "RecFormat", "mkv")
        cfg.rec_encoder = _get(cp, "AdvOut", "RecEncoder", "none")
        cfg.rec_rescale = _get(cp, "AdvOut", "RecRescale", "false").lower() == "true"
        cfg.rec_rescale_res = _get(cp, "AdvOut", "RecRescaleRes")
        enc_file = "recordEncoder.json" if cfg.rec_encoder.lower() not in ("none", "") else "streamEncoder.json"
        if cfg.rec_encoder.lower() in ("none", ""):
            cfg.rec_encoder = _get(cp, "AdvOut", "Encoder", "") + " (same as stream)"
    else:
        cfg.rec_path = _get(cp, "SimpleOutput", "FilePath")
        cfg.rec_format = _get(cp, "SimpleOutput", "RecFormat2") or _get(cp, "SimpleOutput", "RecFormat", "mkv")
        cfg.rec_quality = _get(cp, "SimpleOutput", "RecQuality", "Stream")
        cfg.rec_encoder = _get(cp, "SimpleOutput", "RecEncoder") or _get(cp, "SimpleOutput", "StreamEncoder")
        enc_file = ""
    if enc_file:
        try:
            with open(os.path.join(pdir, enc_file), "r", encoding="utf-8-sig") as f:
                cfg.encoder_settings = json.load(f)
        except Exception:
            cfg.encoder_settings = {}
    bx, by = _get(cp, "Video", "BaseCX"), _get(cp, "Video", "BaseCY")
    ox, oy = _get(cp, "Video", "OutputCX"), _get(cp, "Video", "OutputCY")
    cfg.base_res = f"{bx}x{by}" if bx else ""
    cfg.output_res = f"{ox}x{oy}" if ox else ""
    cfg.fps, cfg.fps_label = _fps(cp)
    cfg.color_format = _get(cp, "Video", "ColorFormat", "NV12") or "NV12"
    cfg.color_space = _get(cp, "Video", "ColorSpace", "709") or "709"
    cfg.color_range = _get(cp, "Video", "ColorRange", "Partial") or "Partial"
    try:
        cfg.sample_rate = int(_get(cp, "Audio", "SampleRate", "48000") or 48000)
    except ValueError:
        pass

    # scene collection
    scenes_dir = os.path.join(base, "basic", "scenes")
    sc = os.path.join(scenes_dir, (scene_file or "") + ".json")
    if not os.path.isfile(sc) and os.path.isdir(scenes_dir):
        js = [os.path.join(scenes_dir, f) for f in os.listdir(scenes_dir) if f.endswith(".json")]
        sc = max(js, key=os.path.getmtime) if js else ""
    if sc and os.path.isfile(sc):
        cfg.scene_collection = os.path.splitext(os.path.basename(sc))[0]
        try:
            with open(sc, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            for s in data.get("sources", []):
                sid = s.get("id", "")
                st = s.get("settings", {}) or {}
                if sid == "dshow_input":
                    fi = st.get("frame_interval", -1)
                    fps = round(1e7 / fi, 3) if isinstance(fi, (int, float)) and fi > 0 else None
                    cfg.capture_devices.append({
                        "name": s.get("name"), "device": (st.get("video_device_id") or "").split(":")[0],
                        "res_type": st.get("res_type", 0), "resolution": st.get("resolution", ""),
                        "fps": fps, "fps_match_output": fps is None,
                        "color_space": st.get("color_space", "default"), "color_range": st.get("color_range", "default"),
                        "buffering": st.get("buffering", 2), "video_format": st.get("video_format", 0),
                        "audio_output_mode": st.get("audio_output_mode", 0),
                        "hw_decode": st.get("hw_decode", False),
                    })
                elif sid in ("game_capture", "window_capture", "monitor_capture"):
                    cfg.screen_captures.append({"name": s.get("name"), "kind": sid, "force_sdr": st.get("force_sdr", False),
                                                "method": st.get("method", st.get("capture_mode", ""))})
                elif sid == "browser_source":
                    cfg.browser_sources += 1
        except Exception:
            pass
    # websocket
    try:
        with open(os.path.join(base, "plugin_config", "obs-websocket", "config.json"), "r", encoding="utf-8") as f:
            cfg.websocket = json.load(f)
    except Exception:
        cfg.websocket = {}
    return cfg
