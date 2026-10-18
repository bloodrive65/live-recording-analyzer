# OBS Session Analyzer

A Windows desktop app that watches OBS while you play and tells you, in plain English, what is going wrong and how to fix it. It works before, during and after a recording.

- **Before recording:** a pre-flight check of your OBS profile, scene sources and last log. It covers color range, 8-bit vs. 10-bit encoder profiles, capture-card FPS (59.94 vs 60), audio sample rates, HDR without Force SDR, recording-drive type and free space, and heavy encoder presets.
- **During recording:** a live Advisor feed and Windows pop-ups. It reports skipped frames along with what the PC was doing at that second (GPU encoder/3D load, CPU, RAM, temperature, recording-drive latency, busiest programs). It also catches files that stop growing, low disk space, audio buffering, and settings that don't match the file. It offers a Pause recording button when that would help.
- **After recording:** an automatic report with timestamps (clock time and video position), likely causes and fixes. An optional deep scan finds repeated or frozen frames inside the video.

The app never changes OBS settings on its own.

## Install
Download `OBS-Session-Analyzer-Setup-x.y.z.exe` from Releases. The installer is unsigned, so Windows SmartScreen may warn you. Click **More info → Run anyway**.

One-time OBS step: **Tools → WebSocket Server Settings → Enable WebSocket server**. The app reads the password automatically.

## Build from source
```
pip install -r requirements.txt pyinstaller
python app.py
```
The GitHub Actions workflow builds the installer and the portable zip on `windows-latest`, with ffmpeg/ffprobe from gyan.dev.
