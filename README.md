# Live Recording Analyzer

A Windows desktop app that watches OBS Studio while you record and tells you, in plain English, what is going wrong and how to fix it — before, during and after a recording. (Formerly "OBS Session Analyzer".)

Website, help and downloads: https://bloodrive65.github.io/live-recording-analyzer/

- **Guided setup (optional):** on first start the app asks whether you want it to check this PC step by step — OBS, the connection to OBS, the recording drive, graphics card and encoder, Windows HDR and notifications. Tick "Remember my choice" to stop the question. Re-run it any time from Settings.
- **Check OBS settings:** reads your OBS profile, scenes and sources and the latest OBS log, with a progress indicator while it works, and lists only what would cause a problem in your next recording. Steps match your OBS setup (Simple or Advanced output mode, the capture source type, and so on). Lower-priority items are marked as tips.
- **During recording:** a live Advisor feed plus Windows pop-ups, so you don't have to watch the app. It reports skipped frames along with what the PC was doing at that second, catches files that stop growing, drive stalls and late audio, and offers a Pause recording button when that helps.
- **After recording:** an automatic report with timestamps (clock time and video position), likely causes and fixes. An optional deep scan finds repeated or frozen frames inside the video.
- **Light on resources:** background work runs at low priority (analysis runs in its own process, so the window stays responsive); a Light mode halves its work; its own CPU and memory use is shown in the status bar.

The app never changes OBS settings on its own.

## Install
Download `Live-Recording-Analyzer-Setup-x.y.z.exe` from [Releases](https://github.com/bloodrive65/live-recording-analyzer/releases). The installer is unsigned, so Windows SmartScreen may warn you. Click **More info → Run anyway**. Installing 1.2 replaces OBS Session Analyzer 1.1 and keeps your sessions, reports and settings.

One-time OBS step: **Tools → WebSocket Server Settings → tick Enable WebSocket server → OK**. The connection status in the app turns green within a few seconds; the password is read automatically.

## Build from source
```
pip install -r requirements.txt pyinstaller
python app.py
```
The GitHub Actions workflow builds the installer and the portable zip on `windows-latest`, with ffmpeg/ffprobe from gyan.dev.
