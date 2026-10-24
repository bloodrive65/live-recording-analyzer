# Project notes: Live Recording Analyzer

Short rules for anyone (person or AI assistant) changing this repo. What the app does for users is in README.md and on the website (`docs/`). This file covers how to work on it.

## What it is
- A Windows 10/11 desktop app (Python 3.12, PySide6) that watches OBS Studio while recording and explains problems after.
- Supported platform: Windows only. A macOS version is planned; see `notes/mac-port.md`.
- Shipped as an Inno Setup installer built by GitHub Actions. Users don't run scripts.

## Module map
- `app.py`: the whole window (tabs, tray pop-ups, settings). `VERSION` is set here.
- `analyzer/analyze.py`: `run()` builds findings plus `Result.steps` (the "How this recording was checked" list).
- `analyzer/obs_log.py`, `obs_config.py`, `obsws.py`: OBS log, OBS profile/scenes, OBS WebSocket.
- `analyzer/live.py`: live advisor rules (`LiveAdvisor`) and `quick_file_check`.
- `analyzer/monitor.py`: once-a-second PC readings. `video_probe.py`: ffprobe/ffmpeg and the deep scan.
- `analyzer/winevents.py`: Windows event log (returns `[]` on other systems).
- `analyzer/report.py` (offline HTML), `weblink.py` (report link), `steps.py`, `proc.py` (analysis process).
- `docs/`: GitHub Pages site. `docs/report.html` and `docs/assets/report.js` show reports from the link.
- One module per job. Don't split modules further, and don't merge them, unless there is a concrete reason.

## Product rules
- Write for any user, not one person's PC. Use plain language and explain any jargon.
- Every problem message says what happened, where it comes from (game, GPU, encoder, drive, OBS setting, capture device, Windows), and the exact steps to fix it for the hardware and software involved. A bare "mismatch" message is not acceptable.
- Only warn when the evidence shows a real problem. Lower-priority items are tips.
- Never change OBS settings automatically. Tell the user what to change and where.
- Analyze only when the user asks, unless they turned on automatic analysis (`auto_after`).
- Alerts that matter during recording must also go to Windows tray pop-ups.
- Checking logs and reports must stay fast (well under a minute). Slow work goes in a background thread or the analysis process.
- Don't add features the user didn't ask for.
- Brand: name "Live Recording Analyzer", #1e1e22 tile, neutral gray/white accent, pixel-art chip icon. No emojis in the app or docs.

## Privacy
- Never commit a real user's OBS log, report, hardware IDs or folder paths. Tests use the made-up data in `tests/fixtures.py`.
- Report links must keep folder names out. Reports stay on the PC or inside the link's `#` part.
- Don't change repo access or add collaborators. The owner decides that.

## Testing rules
- Run before every commit: `python -m unittest discover -s tests -p "test_*.py" -v` (needs ffmpeg and ffprobe on PATH).
- Every bug fix comes with a test that fails on the old code and passes on the new code. Include the failure case, not just the normal path.
- Keep tests small and fast: fixtures only, temporary APPDATA (`Workspace`), and no writing to real OBS folders.
- When a check can't finish, the result must say "Couldn't finish" or "Only part of the file could be checked", never all-clear. Tests should cover that.
- CI (`tests.yml`) runs on every push on Windows and Linux. `build.yml` runs the tests again before building.

## Evidence when reporting work
Label claims:
- **VERIFIED**: you ran it or saw it (test output, screenshot, CI run).
- **INFERRED**: follows from the code but wasn't run.
- **UNVERIFIED**: not checked.

Never say a test passed without the output. End each change summary with: files changed, behavior changed, how it was validated, and remaining risks.

## Release checklist
1. Bump the version in three places: `VERSION` in `app.py`, `$ver` in `.github/workflows/build.yml`, and `AppVersion` in `packaging/installer.iss`.
2. Update `packaging/release-notes.md` in plain language.
3. Rebuild the website if the help pages changed (the site generator writes into `docs/`).
4. Run the tests and look at screenshots of any changed screens.
5. Commit, push, and wait for Tests to pass. Then tag `vX.Y.Z` and push the tag. CI builds and publishes the installer only if the tests pass.
6. Before release, review the changes separately and look for realistic, high-impact failures first.

## Scope discipline
- Make the requested change. No speculative refactors or drive-by rewrites.
- If something seems wrong but is out of scope, write it down and ask.
