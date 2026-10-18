"""Read OBS's current profile + scene collection straight from disk.

Works even when OBS is closed, so the OBS settings check can run before you start.
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
    rec_uses_stream_encoder: bool = False

    def snapshot(self) -> dict:
        """The settings people change to fix problems, with OBS's own wording. Used to spot changes."""
        es = self.encoder_settings or {}
        preset = str(es.get("preset2") or es.get("preset") or "").lower()
        rc = str(es.get("rate_control") or "")
        level = es.get("cqp") or es.get("cq") or es.get("crf") or es.get("qp") or ""
        snap = {
            "Video encoder": self.rec_encoder or "",
            "Preset": PRESET_LABELS.get(preset, preset.upper() if preset else ""),
            "Rate control": rc,
            "Quality level (CQ/CQP)": str(level) if level != "" else "",
            "Bitrate": f"{es['bitrate']} Kbps" if es.get("bitrate") and rc.upper() in ("CBR", "VBR") else "",
            "Multipass mode": str(es.get("multipass") or ""),
            "Look-ahead": ("on" if es.get("lookahead") else "off") if "lookahead" in es else "",
            "Profile": str(es.get("profile") or ""),
            "Output resolution": self.output_res,
            "FPS": self.fps_label or (f"{self.fps:g}" if self.fps else ""),
            "Color format": self.color_format,
            "Color space": self.color_space,
            "Color range": self.color_range,
            "Recording format": self.rec_format,
            "Recording path": self.rec_path,
        }
        return {k: v for k, v in snap.items() if v not in ("", None)}

    @property
    def advanced(self):
        return (self.output_mode or "").lower() == "advanced"

    def enc_path(self, option: str) -> str:
        """Where an encoder option lives in OBS for *this* user's output mode."""
        if self.advanced:
            tab = "Streaming" if self.rec_uses_stream_encoder else "Recording"
            return f"OBS → Settings → Output → {tab} tab → {option}"
        return (f"OBS → Settings → Output → set Output Mode (at the top) to Advanced → Recording tab → {option}. "
                f"Simple mode doesn’t show this option")

    def rec_path_setting(self) -> str:
        return ("OBS → Settings → Output → Recording tab → Recording Path" if self.advanced
                else "OBS → Settings → Output → Recording → Recording Path")

    def rec_format_setting(self) -> str:
        return ("OBS → Settings → Output → Recording tab → Recording Format" if self.advanced
                else "OBS → Settings → Output → Recording → Recording Format")

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
            cfg.rec_uses_stream_encoder = True
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
    cfg.websocket = websocket_settings(base)
    return cfg


PRESET_LABELS = {
    "p1": "P1: Fastest", "p2": "P2: Faster", "p3": "P3: Fast", "p4": "P4: Medium",
    "p5": "P5: Slow", "p6": "P6: Slower", "p7": "P7: Slowest",
}


def settings_files(base: str = "") -> list:
    """Files OBS rewrites when you click Apply/OK in Settings (current profile and scene collection)."""
    base = base or obs_dir()
    out = [os.path.join(base, "global.ini"), os.path.join(base, "user.ini")]
    try:
        prof = os.path.join(base, "basic", "profiles")
        for d in os.listdir(prof):
            for f in ("basic.ini", "recordEncoder.json", "streamEncoder.json"):
                out.append(os.path.join(prof, d, f))
    except OSError:
        pass
    return out


def settings_stamp(base: str = "") -> tuple:
    st = []
    for f in settings_files(base):
        try:
            st.append((f, os.path.getmtime(f)))
        except OSError:
            pass
    return tuple(st)


def websocket_settings(base: str = "") -> dict:
    """OBS 30+ keeps WebSocket settings in plugin_config/obs-websocket/config.json;
    OBS 28-29 kept them in global.ini under [OBSWebSocket]. Read fresh every time."""
    base = base or obs_dir()
    try:
        with open(os.path.join(base, "plugin_config", "obs-websocket", "config.json"), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
            d["_source"] = "config.json"
            return d
    except Exception:
        pass
    cp = _ini(os.path.join(base, "global.ini"))
    if cp.has_section("OBSWebSocket"):
        b = lambda k, dflt: str(_get(cp, "OBSWebSocket", k, dflt)).lower() in ("true", "1")
        try:
            port = int(_get(cp, "OBSWebSocket", "ServerPort", "4455") or 4455)
        except ValueError:
            port = 4455
        return {"server_enabled": b("ServerEnabled", "false"), "server_port": port,
                "auth_required": b("AuthRequired", "true"), "server_password": _get(cp, "OBSWebSocket", "ServerPassword", ""),
                "_source": "global.ini"}
    return {}
