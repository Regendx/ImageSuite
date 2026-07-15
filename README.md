# ImageSuite 0.9.0 RC33

ImageSuite combines QuickFX editing, UpMark enhancement and watermarking, and VisualDupe similarity cleanup in one native PySide6 desktop application.

This is a **release candidate**, not the final 1.0 release. The core application has completed its code-side reliability pass. The remaining 1.0 gate is validation of the packaged Windows builds on clean Windows 10/11 systems and real NVIDIA hardware.

## Main workflows

### Edit

- MP4/WebM files open in a resizable video-editor style clip window with playback, a thumbnail filmstrip, a playhead, draggable In/Out handles, Set In/Out buttons, and optional precise time fields
- MP4/WebM export can preserve the matching source-audio segment even after visual edits; direct unedited export remains available for original-resolution video, while edited frames are rendered straight to video and remuxed with source audio
- Video selection has no fixed five-minute ceiling; long or multi-hour ranges automatically use a sparse, memory-bounded editing proxy while preserving the complete selected duration
- Large animation edits automatically use a memory-aware working copy instead of failing; playback duration is preserved, Undo restores the prior copy, and Reset restores the imported source
- Continuous automatic previews while effect, correction, creative, and text settings are changing
- Preview begins from the first rectangle/lasso/face-circle gesture; no extra Preview button or slider nudge is required
- Large images use bounded display previews while Apply always processes the original full-resolution image
- Rectangle and lasso selections
- Multiple protected face circles
- 28 curated censorship effects covering privacy blur, directional distortion, glass, tile scrambling, print styles, redaction, and digital interference
- ASCII Art uses a font-calibrated 95-character luminance ramp, preserves source hues, detects equal-brightness color boundaries, and uses the existing Color preservation slider to move from compact stable palettes to full RGB
- ASCII Art uses cached glyph masks and vectorized frame composition, restoring practical processing speed for GIF/MP4/WebM clips without removing color-aware contours
- Effect-specific live controls: every visible slider is meaningful, relabeled for the selected effect, and irrelevant controls are hidden instead of disabled
- Up to five independent parameters per effect, including mix/opacity, size, softness, detail, direction, texture, threshold, palette, and distortion controls
- Multi-effect censor chains with drag-to-reorder and 19 tuned privacy/style presets
- Every chain entry retains its own complete parameter set and restores it when selected
- One-click reset for the current effect or selected chain entry
- Apply any single effect or effect chain outside protected faces
- Blur, pixel, mosaic, black, clone and heal brushes
- Multiline text with live preview, direct dragging, eight resize handles, rotation, opacity, automatic wrapping, custom fonts, bold/italic variants, character and line spacing, alignment, outline, configurable shadow, and rounded backgrounds
- Text presets for captions, titles, subtitles, memes, labels, quotes, lower thirds, and watermarks; the most recent text can be reopened before the next edit
- Simple sticker workflow with categorized emoji/symbol palettes and imported PNG/WebP/JPEG stickers; stickers can be moved, resized, rotated, faded, outlined, shadowed, cancelled, and reopened before the next edit
- Arrows, boxes and creative effects
- Brightness, contrast, saturation and sharpness corrections
- Crop, resize, rotate, flip, reset, cinematic bars, and animation-duration extension by repeating a loop or holding the final frame
- Adaptive GIF, MP4, and WebM editing with live playback: effects, chains, selections, corrections, creative looks, text, and stickers update on the moving animation before Apply
- Core video probing, proxy decoding, thumbnails, and rendered encoding use the bundled FFmpeg directly, avoiding an intermediate GIF and preventing stale pipe/file-handle buildup during repeated video work
- Animation Save As uses the same visible In/Out timeline and preview playback, and can set an exact GIF playback duration, palette size, dithering, and optimization
- Unedited MP4/WebM clips can export directly from the original video through FFmpeg, preserving original resolution and audio without creating a GIF or loading the export back through Python frames
- Edited MP4/WebM documents still encode straight from edited frames to video; the export dialog clearly distinguishes this rendered path from direct source export
- The QuickFX tool column uses a draggable splitter that remembers its width, and video import/export dialogs can be resized or maximized
- Multiple documents, recovery, chronological undo/redo and keyboard navigation

### Enhance

- Pillow resize methods for fast, predictable processing
- Optional Spandrel/PyTorch AI models
- CPU, CUDA and optional DirectML device selection
- Tiled AI processing with actionable out-of-memory errors
- Finishing controls and text/image watermarks
- Visible non-AI parallel processing control from 1 to 50 workers; the value is a maximum and the runtime planner automatically lowers active workers when image size, RAM, CPU saturation, or animated GIFs make a larger value slower or unsafe
- Bounded task submission, coalesced progress updates, fast batch encoding, atomic output reservation, and explicit image-buffer cleanup for stable long batches
- Built-in and persistent custom text watermark presets
- Animated GIF enhancement up to 10 seconds with frame timing and loop preservation
- Queue reordering, cancellation, retry-failed and output review
- Collision-safe output names, timestamped folders and ZIP export

### Organize

- Perceptual duplicate and near-duplicate grouping
- Thumbnail review and keyboard-first navigation
- Keep-best rules based on resolution, sharpness, date, file size or path
- Reversible moves
- Recycle Bin support with partial-failure reporting
- Copy and CSV export with rollback/atomic-write protection

## Batch performance and memory

- **Maximum workers** is an upper limit, not a promise to launch that many full-resolution jobs. Small images can still use all 50; photo-sized and animated batches are automatically capped to the measured safer range.
- Completed files no longer wait behind a global save lock while retaining full output images in RAM.
- Animated GIF sources are decoded one frame at a time, and only processed frames needed by the GIF encoder remain resident.
- Batch PNG/JPEG/WebP encoding favors throughput by default; normal editor saves retain the slower size-optimization path.
- Optional AI models are released after a completed batch instead of permanently pinning CPU/GPU memory.
- Organize fingerprinting and thumbnail caches are bounded and use reduced previews rather than full-resolution images.

## Installation

### Installed Windows build

The GitHub release workflow builds a per-user Windows installer named **`ImageSuite-Setup-v<version>.exe`**. The installer does not require administrator rights and can add:

- Start Menu and optional Desktop shortcuts
- A dedicated **Open in ImageSuite** File Explorer command for images and videos
- **Open folder in ImageSuite** for folders
- Automatic update support through GitHub Releases

The Explorer command is a direct shell action, not an **Open with** file association, so ImageSuite does not take over the default app for any file type. On Windows 11, traditional third-party shell commands may appear inside **Show more options**.

ImageSuite uses one running application instance. Right-clicking additional files sends them to the existing window instead of launching duplicate editors.

### Updates

Installed Windows builds check GitHub Releases at most once per day by default. Update checks can be disabled, and release-candidate updates can be included or excluded under **Help → Preferences**. **Help → Check for updates…** always performs a manual check.

ImageSuite asks before downloading and again before installation. When GitHub provides an asset SHA-256 digest, the downloaded installer is verified before it can run. Installation begins only after ImageSuite closes, then the app reopens automatically.

For the updater to recognize a release, attach an installer asset whose filename contains **ImageSuite**, **Setup**, and ends in **`.exe`**. The included release workflow does this automatically for version tags.

### Source launcher

1. Extract the entire ZIP.
2. Double-click **`ImageSuite.bat`**.
3. The single launcher finds Python 3.10 or newer, creates `.venv`, installs or repairs missing dependencies, verifies them, and starts ImageSuite.

Do not run the batch file from inside the ZIP preview.

Optional AI support is installed from **Enhance → Install / Repair AI**. The app uses the same `ImageSuite.bat` launcher internally, so there is no separate AI launcher to find. Place compatible `.pth`, `.pt`, or `.safetensors` models in `models`.

### Portable Windows build

A release maintainer can run:

```text
developer_tools\build_release.bat
```

This runs the release self-check, all tests and PyInstaller before creating a portable folder and ZIP. The normal executable is windowed and does not show a console.

### Installer

After installing Inno Setup 6, run:

```text
developer_tools\build_installer.bat
```

The installer is per-user, does not require administrator privileges, and includes the optional File Explorer command. Tagging a commit as `v0.9.0-RC33` also runs `.github/workflows/release.yml`, builds the installer on Windows, and attaches it to the GitHub Release used by the updater.

### AI-enabled executable build

The standard executable is intentionally a smaller core build. To bundle PyTorch and Spandrel into the executable, run:

```text
build_exe_ai.bat
```

The AI build is substantially larger. CUDA behavior must be verified on the target Windows/NVIDIA configuration before distributing it.

## First-use workflow

- Drop one image to open it in **Edit**.
- Drop multiple images to choose **Edit** or **Enhance**.
- Drop a folder to choose **Edit**, **Enhance**, or **Organize**.
- Press `Ctrl+K` to search common commands.
- Press `F1` for mouse and keyboard navigation.

## Mouse and keyboard navigation

### Global

- `Ctrl+1…5`: switch workspaces
- `Alt+Left/Right`: previous or next workspace
- `Ctrl+K`: command palette
- Mouse Back/Forward: workspace navigation; group navigation in Organize

### Editor

- Space + left-drag, middle-drag or right-drag: pan
- Wheel: zoom around the pointer
- Shift + wheel: horizontal pan
- Alt + wheel: resize active text, sticker, or brush
- Arrow keys: move active text, selection or face circle
- Shift + arrow: move by 10 pixels
- Alt + arrow: resize the active item
- Ctrl + arrow: pan
- `R`: rectangle, `L`: lasso, `C`: face circles, `T`: text
- `S`: sticker, `A`: arrow, `X`: box, `P`: pan
- `G`: blur brush, `J`: pixel brush, `M`: mosaic brush
- `Shift+B`: black brush, `K`: clone, `H`: heal
- `[` / `]`: brush size
- `Ctrl+Z` / `Ctrl+Y`: chronological undo/redo, including masks
- `Ctrl+Enter`: apply the active text or sticker
- `Ctrl+Space`: play or pause the active GIF while live settings remain editable
- `Esc`: cancel active preview or selection

### Enhance

- `Delete`: remove selected queue items
- `Alt+Up/Down`: reorder selected queue items
- `Ctrl+P`: preview
- `Ctrl+Enter`: start processing
- `Esc`: cancel

### Organize

- `J` / `K`: next or previous group
- Arrow keys: navigate rows
- Space or `X`: toggle checked state
- `Enter` or `O`: open selected file
- `E`: open in editor
- `R`: reveal in Explorer
- `B`: check everything except the preferred image
- `Ctrl+Delete`: recycle checked files

## Recovery and data safety

- Image saves use a temporary sibling file and atomic replacement.
- A failed save leaves the existing file and open document intact.
- Dirty documents are recovered asynchronously; unchanged revisions are not repeatedly rewritten.
- Discarded and successfully saved documents remove their recovery files.
- Damaged recovery pairs are moved to `recovery_failed` instead of blocking startup.
- Multi-file moves roll back after a failure. Partial copies are removed.
- Undo history has both entry and approximate memory limits.
- Save state is revision-based: save → undo becomes dirty, and redo to the saved revision becomes clean again.

Recovery is not a substitute for saving important work.

## Image formats and metadata

Supported image files:

- PNG
- JPEG/JPG/JFIF
- WebP
- BMP
- TIFF
- GIF

Animated GIF, MP4, and WebM files are supported in Edit and Enhance. MP4/WebM import and export provide a visible timeline with preview playback and draggable In/Out handles. There is no fixed clip-duration ceiling: long selections use a bounded proxy that reduces temporal or spatial detail as needed while preserving the complete selected duration. ImageSuite preserves animation timing and supports GIF, MP4, and WebM export. Use **Transform → Extend animation duration…** to repeat the full animation, repeat the current preview-loop range, or hold the final frame until an exact target duration. Use the animation controls below the canvas, or press `Ctrl+Space`, to play the animation while changing effects, masks, corrections, creative effects, and Quick Text.

For an unedited MP4/WebM, direct export trims the original source at full resolution and retains audio without using the proxy or a GIF conversion. Pixel-edited video is rendered from the editable proxy, so extremely long sources may intentionally contain less temporal detail than the original. This keeps memory bounded; ImageSuite is not yet a full non-linear video compositor that replays every image-editing operation against every original source frame.

Brush, clone, and heal remain unavailable for animations because copying one frame's painted pixels into every frame would corrupt motion. Multi-page TIFF files are still rejected rather than silently editing one page. Extremely large animations can still exceed the hard safety ceiling and may need trimming or resolution reduction.

ImageSuite can preserve ICC color profiles and DPI. Personal EXIF fields, GPS data and camera metadata are intentionally not copied. This behavior is shown in Preferences.


## Dependency installation troubleshooting

`ImageSuite.bat` creates a private `.venv` and verifies every required runtime package before launching. If setup fails, it prints the exact missing module or DLL and writes pip details to `%TEMP%\ImageSuite-pip-install.log`.

To repair the environment manually from the ImageSuite folder:

```text
.venv\Scripts\python.exe -m pip install --prefer-binary --no-cache-dir -r requirements.txt
```

Deleting only the `.venv` folder is safe; the launcher recreates it without removing models, presets, settings, or output files. You can also run `ImageSuite.bat --repair`.

## Diagnostics

Open **More → About** or **Help → About ImageSuite** to see:

- ImageSuite, Python, PySide6, Pillow and NumPy versions
- OS and architecture
- Portable-mode status
- AI/PyTorch/CUDA availability
- Data and log locations

Unhandled errors are written to a rotating log. Repeated copies of the same error are suppressed for five seconds, and the error dialog can copy a diagnostic report.

For source troubleshooting, run the same launcher in debug mode:

```text
ImageSuite.bat --debug
```

## Portable mode

Create an empty `portable.flag` beside `app.py` or `ImageSuite.exe`. Settings, recovery, recent items and Quick Text history are then stored beside the application.

Without portable mode, installed builds keep writable models and settings under the user profile, and default output under `Pictures\ImageSuite`.

## Release checks

Run:

```text
python release_check.py
python -m pytest
```

The Windows build script runs both automatically before PyInstaller.

See `RELEASE_CHECKLIST.md` for the clean-machine checks still required before changing the version to 1.0.0.

## Known ceilings

- Similarity grouping remains accuracy-first pairwise comparison. A mathematically safe hash upper-bound skips impossible matches, but very large libraries are still fundamentally O(n²).
- AI uses one cached model because the application runs one AI workflow at a time.
- Final output writes are serialized to guarantee collision-safe names.
- ImageSuite is a focused image utility, not a full layer-based painting application.

## Diagnostics

Core diagnostics load without importing PyTorch; use **Refresh** when AI/CUDA details are needed.


## AI profiles and smart tiling

Enhance includes Balanced, Fast, Low memory, and Maximum quality profiles. Auto precision uses FP16 on CUDA when supported, while automatic tile sizing and OOM recovery reduce failed GPU jobs. Use **Check AI** beside the model selector to inspect the active backend and available VRAM.
