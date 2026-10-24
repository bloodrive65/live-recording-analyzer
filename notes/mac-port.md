# macOS port notes

Status: planned, not started. This lists what depends on Windows today, so the port can be scoped. The Linux CI job already runs the tests for the platform-independent analysis code.

## Works as-is (pure Python + ffmpeg)
- `analyzer/obs_log.py`: log parsing. Only the default log folder changes.
- `analyzer/analyze.py`: findings and steps, except the drive-type check (see below).
- `analyzer/video_probe.py`: ffprobe/ffmpeg calls. Executable names already drop `.exe` off Windows.
- `analyzer/live.py` rules, `steps.py`, `report.py`, `weblink.py`, `proc.py`, `obsws.py`.
- `docs/` report page.

## Windows-only today
| Where | What | Mac replacement |
|---|---|---|
| `obs_config.py`, `obs_log.py`, `live.py` messages | OBS folder from `%APPDATA%\obs-studio` | `~/Library/Application Support/obs-studio` |
| `analyze.py` (drive type, PowerShell) | USB/external drive detection | `diskutil info` or psutil |
| `monitor.py` | `nvidia-smi` GPU/encoder readings, `GetDriveTypeW`, thread priority | No NVIDIA on modern Macs. Use `powermetrics` (needs admin) or skip GPU readings. Use `os.nice` for priority |
| `winevents.py` | Windows event log (driver resets, disk errors) | `log show` (unified log) or leave out |
| `system_check.py` | OBS install from registry, Windows notification setting | `/Applications/OBS.app`, macOS notification permission |
| `app.py` | Documents folder, default browser from registry, taskbar app ID | `QStandardPaths`, `QDesktopServices`, not needed |
| Advice text | Windows-specific steps (Run as administrator, HDR, Game Mode, NVENC) | Mac-specific steps (Screen Recording permission, VideoToolbox / Apple VT encoder) |
| Packaging | Inno Setup installer | PyInstaller `.app` in a `.dmg`. Signing and notarization needed to avoid Gatekeeper warnings |
| CI | `build.yml` on `windows-latest` | Add a `macos-latest` job to `tests.yml`, then a Mac build job |

## Suggested order
1. Switch the Linux test job to `macos-latest` and make the folder paths per-platform.
2. Gate the Windows-only checks so they report "Not available on macOS" instead of failing.
3. Write Mac-specific advice text for the encoder and permissions.
4. Package and sign.
