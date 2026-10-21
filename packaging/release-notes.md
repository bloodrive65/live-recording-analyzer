## What's new in 1.4.0

- **Faster, smoother window.** The whole app used to run at low priority, so Windows let almost anything else go first, including redrawing the menus. Now the window runs at normal priority. Only the background work (the PC checks and the analysis) runs at low priority, so your game and OBS still come first.
- **The analysis runs as a separate process.** Reading a large recording no longer slows down the menus.
- **No more freezes from sleeping drives.** "Your latest recording" is checked in the background, so a USB or network drive that is spinning up can't freeze the window.
- **Time estimate.** The Analyzer tab shows about how long the quick check and the deep scan will take, plus a countdown while it runs. The estimate learns from how fast your PC actually is.
- **New wave progress bar** while analyzing.
- **New look:** a pixel-art chip icon and a neutral gray theme.
- **Slowness log.** If the window ever can't respond for more than a tenth of a second, the moment is written to `perf.log` in the data folder (Documents\Live Recording Analyzer).

Update with Settings → Updates → Check for updates now, or download **Live-Recording-Analyzer-Setup-*.exe** below.
