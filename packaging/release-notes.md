## What's new in 1.4.5

Reliability fixes. Each one has a test that proves the failure case, and the tests now run automatically on Windows and Linux for every change.

- **Fixed: the analysis could crash when Windows logged a problem during the recording.** A graphics driver reset, disk or USB error, or app crash within a couple of minutes of a recording made the whole analysis fail, which is exactly when those events matter most. They now appear on the report's timeline.
- **Fixed: "No relevant Windows events" was shown even when the event log couldn't be read.** That step is now marked "Couldn't finish" with the reason.
- **Fixed: the deep scan could report "No repeated or frozen frames" when it hadn't actually finished.** If ffmpeg stops with an error, can't be found, is cancelled, or only gets partway through the file, the step is marked "Couldn't finish" with the reason. The report then makes no claim about repeated frames.
- **Fixed: a cut-off recording could look complete.** If the frame-by-frame check can't read to the end of the file (drive disconnected, OBS closed before finishing the file, file still being copied), the report says how much was checked, for example "Only the first 0:12:30 of 2:41:28 could be read". It also explains how to recover the file, instead of saying "No missing frames found".
- The installer is now only built when all tests pass.

Includes everything from 1.4.4.

Update with Settings → Updates → Check for updates now, or download **Live-Recording-Analyzer-Setup-*.exe** below.
