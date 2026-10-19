## What's new in 1.3.1

- **Fixed a false color-range warning.** OBS writes "Partial" in its log, but every OBS menu calls the same setting "Limited". The app compared the words literally and reported a mismatch that wasn't real. A file marked Limited with OBS set to Limited is now correctly treated as matching.
- **Every finding now shows where it comes from on your PC.** A new "Where it comes from on your PC" box lists the exact menu path, the scene and source involved (read from your OBS log), the current value, and what to change it to.
- **Specific fixes for findings that only described the problem**: missing frames in the file, audio gaps, audio/video length differences, frozen or repeated frames, file resolution or frame rate differences, and color-space marking.
- **Windows HDR finding is smarter**: if the scene you recorded has no screen, window or game capture, it says HDR didn't affect that recording and lists which scenes it would affect.
- Encoder settings are shown with the same names as OBS's menus (for example "P6: Slower (Better Quality)").

Reports made with older versions keep their old text. Analyze the recording again to get the new details.

Download **Live-Recording-Analyzer-Setup-*.exe** below and run it, or use Settings → Updates → Check for updates now in 1.3.0.
