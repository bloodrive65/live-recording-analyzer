"""Checks for the guided setup. Everything is read from this PC: nothing is assumed.

Each check returns a dict:
  key, title, state ("ok" | "attention" | "unknown"), summary, steps (list of str),
  action (optional: (label, action_name))
"""
from __future__ import annotations

import os
import shutil
import sys

from . import obs_config, obs_log, obsws
from .analyze import drive_info

OK, ATTN, UNKNOWN = "ok", "attention", "unknown"


def _c(key, title, state, summary, steps=None, action=None):
    return {"key": key, "title": title, "state": state, "summary": summary, "steps": steps or [], "action": action}


def _latest_log():
    try:
        logs = obs_log.latest_logs(1)
        return obs_log.parse(logs[0]) if logs else None
    except Exception:
        return None


def obs_exe() -> str:
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            p = os.path.join(base, "obs-studio", "bin", "64bit", "obs64.exe")
            if os.path.isfile(p):
                return p
    if sys.platform == "win32":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\OBS Studio") as k:
                        d = winreg.QueryValueEx(k, "")[0]
                        p = os.path.join(d, "bin", "64bit", "obs64.exe")
                        if os.path.isfile(p):
                            return p
                except OSError:
                    pass
        except Exception:
            pass
    return ""


def check_obs(log) -> dict:
    exe = obs_exe()
    has_cfg = os.path.isdir(obs_config.obs_dir())
    running = obsws.obs_running()
    import re
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", log.obs_version if log else "")
    ver = m.group(1) if m else ""
    if not exe and not has_cfg:
        return _c("obs", "OBS Studio", ATTN, "OBS Studio wasn’t found on this PC.",
                  ["Download OBS Studio from obsproject.com and install it (the normal installer, not the portable version).",
                   "Open it once so it creates its settings, then run this setup again."],
                  ("Open obsproject.com", "url:https://obsproject.com/download"))
    parts = []
    if ver:
        parts.append(f"OBS {ver}")
    parts.append("running now" if running else "not running right now")
    major = 0
    try:
        major = int(ver.split(".")[0]) if ver else 0
    except ValueError:
        pass
    if major and major < 28:
        return _c("obs", "OBS Studio", ATTN, ", ".join(parts) + ". This version is too old for live tracking.",
                  ["Update OBS Studio to version 28 or newer (Help → Check For Updates, or download it from obsproject.com)."])
    steps = [] if running else ["Open OBS before you start watching — the app connects to it automatically."]
    return _c("obs", "OBS Studio", OK, "Installed · " + ", ".join(parts) + ".", steps)


def check_websocket(port: int, password: str) -> dict:
    st = obsws.connection_status(port, password)
    s = st["state"]
    if s == "connected":
        return _c("ws", "Connection to OBS", OK, st["label"] + ".", [])
    if s == "obs_closed":
        cfg = st["cfg"]
        if cfg["found"] and cfg["enabled"]:
            return _c("ws", "Connection to OBS", OK, "WebSocket is turned on in OBS. It will connect when OBS is open.", [])
        if cfg["found"]:
            return _c("ws", "Connection to OBS", ATTN, "WebSocket is turned off in OBS.",
                      ["Open OBS.", "Tools → WebSocket Server Settings → tick “Enable WebSocket server” → OK.",
                       "Come back and click Check again."])
        return _c("ws", "Connection to OBS", UNKNOWN, "OBS isn’t running, so the connection can’t be tested yet.",
                  ["Open OBS, then click Check again."])
    steps = [st["detail"], "Then click Check again."]
    return _c("ws", "Connection to OBS", ATTN, st["label"] + ".", steps)


def _gb_per_hour(cfg) -> float:
    try:
        w = int((cfg.output_res or cfg.base_res).split("x")[0])
    except Exception:
        w = 1920
    fps = cfg.fps or 60
    base = 60 if w >= 3800 else (30 if w >= 2500 else 15)
    return base * (fps / 60)


def check_drive(cfg) -> dict:
    if not cfg.found:
        return _c("drive", "Recording drive", UNKNOWN, "OBS settings weren’t found, so the recording folder is unknown.")
    path = cfg.rec_path
    if not path:
        return _c("drive", "Recording drive", ATTN, "No recording folder is set in OBS.", [cfg.rec_path_setting() + " → pick a folder on an internal SSD."])
    if not os.path.isdir(path):
        return _c("drive", "Recording drive", ATTN, f"The recording folder {path} doesn’t exist. Is the drive connected?",
                  ["Connect the drive, or change " + cfg.rec_path_setting() + "."])
    d = drive_info(path)
    try:
        free = shutil.disk_usage(path).free / 1073741824
    except Exception:
        free = None
    desc = " ".join(x for x in (d.get("letter"), d.get("name"), f"({d.get('bus')} {d.get('media')})".replace(" )", ")") if d.get("bus") else "") if x)
    hours = free / _gb_per_hour(cfg) if free else None
    summary = f"{desc or path}" + (f" · {free:,.0f} GB free (roughly {hours:.0f} hours at your settings)" if free is not None else "")
    steps, state = [], OK
    if d.get("type") == "network":
        state = ATTN
        steps.append("Network drives pause unpredictably. Record to a local SSD and move files afterwards: " + cfg.rec_path_setting() + ".")
    elif (d.get("bus") or "").upper() == "USB":
        steps.append("USB drives usually work. If you see drive stalls: Control Panel → Power Options → Change plan settings → "
                     "Change advanced power settings → Hard disk → Turn off hard disk after → Never, and USB settings → USB selective suspend → Disabled.")
    elif (d.get("media") or "").upper() == "HDD":
        steps.append("Hard disks are fine for normal bitrates; avoid copying files to the same disk while recording.")
    if free is not None and hours is not None and hours < 2:
        state = ATTN
        steps.insert(0, "Free up space or choose a bigger drive before long sessions.")
    return _c("drive", "Recording drive", state, summary, steps)


HW_ENC = ("nvenc", "amf", "amd", "qsv", "quicksync", "jim", "ffmpeg_nvenc", "obs_nvenc")


def check_encoder(cfg, log) -> dict:
    gpus = [a["name"] for a in (log.adapters if log else [])]
    gpu_txt = ", ".join(gpus) if gpus else "unknown graphics card"
    if not cfg.found:
        return _c("enc", "Graphics card and encoder", UNKNOWN, f"Graphics: {gpu_txt}. OBS settings weren’t found.")
    from .live import encoder_label
    enc = cfg.rec_encoder or ""
    label = encoder_label(enc) or enc or "unknown"
    hw_gpu = any(k in g.lower() for g in gpus for k in ("nvidia", "geforce", "rtx", "gtx", "radeon", "amd", "intel arc", "intel(r) arc", "intel(r) uhd", "intel(r) iris"))
    if "x264" in enc.lower() and hw_gpu:
        return _c("enc", "Graphics card and encoder", ATTN, f"Graphics: {gpu_txt}. Recording encoder: {label} (uses the CPU).",
                  [cfg.enc_path("Video Encoder") + " → pick your graphics card’s hardware encoder (NVIDIA NVENC, AMD AMF or Intel QuickSync). "
                   "It frees up the CPU for your game."])
    return _c("enc", "Graphics card and encoder", OK, f"Graphics: {gpu_txt}. Recording encoder: {label}.")


def check_hdr(cfg, log) -> dict:
    if not log or not log.displays:
        return _c("hdr", "Windows HDR", UNKNOWN, "Open OBS once and run this again — the HDR state is read from the OBS log.")
    hdr = [d for d in log.displays if "G2084" in d.get("space", "") or "2084" in d.get("space", "")]
    if not hdr:
        return _c("hdr", "Windows HDR", OK, "HDR is off on your monitors. PC game captures will look normal.")
    hdr_canvas = (cfg.color_space or "").startswith("2100") if cfg.found else False
    bad = [s["name"] for s in (cfg.screen_captures if cfg.found else []) if not s.get("force_sdr")]
    if hdr_canvas or not bad:
        return _c("hdr", "Windows HDR", OK, f"HDR is on for {len(hdr)} monitor(s). Your OBS setup accounts for it.")
    return _c("hdr", "Windows HDR", ATTN, f"HDR is on for {len(hdr)} monitor(s), and OBS records in SDR. PC captures may look washed out.",
              ["Display Capture / Window Capture: double-click it → tick “Force SDR”.",
               "Game Capture: if an HDR game looks wrong, double-click it and check Color Space (sRGB for a normal SDR recording).",
               "Or turn HDR off while recording PC games: Windows Settings → System → Display → Use HDR.",
               "Capture-card sources (consoles) are not affected."])


def check_settings(cfg, log) -> dict:
    if not cfg.found:
        return _c("settings", "OBS settings check", UNKNOWN, "OBS settings weren’t found.")
    from .live import preflight_rules
    items = [a for a in preflight_rules(cfg, log) if a.key not in ("pf-summary", "pf-ws")]
    bad = [a for a in items if a.severity in ("critical", "warning")]
    tips = [a for a in items if a.severity == "tip"]
    if not bad:
        return _c("settings", "OBS settings check", OK,
                  "Nothing that will cause problems." + (f" {len(tips)} optional tip(s) will show on the Live tab." if tips else ""))
    steps = []
    for a in bad[:5]:
        steps.append(a.title + (": " + a.steps[0] if a.steps else ""))
    return _c("settings", "OBS settings check", ATTN, f"{len(bad)} thing(s) to fix before you record.", steps)


def check_notifications() -> dict:
    if sys.platform != "win32":
        return _c("notify", "Windows notifications", UNKNOWN, "Only available on Windows.")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\PushNotifications") as k:
            v = winreg.QueryValueEx(k, "ToastEnabled")[0]
        if v == 0:
            return _c("notify", "Windows notifications", ATTN, "Windows notifications are turned off, so alerts can’t pop up while you play.",
                      ["Windows Settings → System → Notifications → turn Notifications on."], ("Open notification settings", "url:ms-settings:notifications"))
    except Exception:
        pass
    return _c("notify", "Windows notifications", OK, "Notifications are on.",
              ["Tip: Do Not Disturb and Windows “game mode” notification rules can hide pop-ups while a full-screen game runs. "
               "Check Windows Settings → System → Notifications if alerts don’t appear."], ("Open notification settings", "url:ms-settings:notifications"))


def run_all(port: int = 4455, password: str = "") -> list:
    cfg = obs_config.load()
    log = _latest_log()
    out = []
    for fn in (lambda: check_obs(log), lambda: check_websocket(port, password), lambda: check_drive(cfg),
               lambda: check_encoder(cfg, log), lambda: check_hdr(cfg, log), lambda: check_settings(cfg, log), check_notifications):
        try:
            out.append(fn())
        except Exception as e:  # never let one check break the setup
            out.append(_c("err", "Check", UNKNOWN, f"Couldn’t run this check: {e}"))
    return out
