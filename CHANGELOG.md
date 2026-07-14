# Changelog

## 0.9.0 RC22

Adaptive animation editing and memory hardening.

- Removed the contradictory 128-million-pixel edit ceiling that could accept an animation during import and reject its first edit.
- Added a dynamic animation-edit budget based on currently available physical memory.
- Large edits now create a balanced working copy instead of failing: frame durations are combined and resolution is reduced only as needed, while total playback duration is preserved.
- Selection rectangles, lassos, and protected face regions scale with an automatically resized working copy.
- Undo restores the previous frame count, dimensions, geometry, and timing; Reset restores the originally imported animation.
- Reworked animation history to transfer frame ownership instead of copying complete frame sets repeatedly during commit, undo, and redo.
- Removed duplicate first-frame storage from active and original animation documents.
- History eviction and document closing now explicitly release PIL frame buffers.
- Shared the physical-memory probe between Enhance worker planning and animation editing.
- Added adaptive-edit, timing, geometry, and ownership-transfer regression coverage; the full suite now contains 170 tests.

## 0.9.0 RC21

Text and sticker workflow overhaul.

- Replaced the single-line Quick Text field with an always-available multiline editor.
- Added custom font selection, bold/italic font variants, alignment, rotation, global opacity, character spacing, line spacing, automatic wrap width, and expanded text presets.
- Added detailed outline, configurable shadow position/blur/opacity/color, background opacity/padding/color, and rounded-corner controls.
- Reworked text rendering into a bounded composited layer so rotated text, shadows, backgrounds, and on-canvas handles share consistent geometry.
- Replaced one-click sticker burn-in with a live sticker overlay that can be moved, resized, rotated, faded, outlined, shadowed, cancelled, applied, and reopened before the next edit.
- Added categorized emoji/symbol sticker palettes plus imported PNG, WebP, and JPEG sticker images.
- Added text and sticker support to live animated previews and final animation application.
- Explicitly releases superseded preview images and temporary text/sticker layers to keep repeated annotation editing memory-stable.
- Added annotation regression coverage; the full suite now contains 167 tests.

## 0.9.0 RC20

Effect-control and target-edge overhaul.

- Added shared target-edge controls for rectangle, lasso, and protected face-circle workflows.
- Added Hard edge, Soft transition, Seamless blend, and Custom edge modes.
- Added independent transition-width and coverage-padding controls; padding keeps selected content fully processed while the blend fades outside it, and expands the protected region for face circles.
- Wired target feathering through still-image preview, animated preview, final application, effect chains, targeted adjustments, and creative effects.
- Rebuilt ASCII Art with edge-oriented contour glyphs, source-aware background color, clearer character rendering, and a new Contour strength control.
- Added a high-glyph-count safety bound and faster wide-lasso expansion so extreme settings do not unnecessarily freeze the editor.
- Clamped imperceptible Gaussian mask tails so untouched pixels remain byte-for-byte unchanged away from the transition.
- Added regression coverage for ASCII contours, rectangle feathering, face protection feathering, and the new UI controls.

## 0.9.0 RC19

Safe Pillow and worker-throughput optimization pass.

- Opaque still images now remain RGB throughout non-AI resize jobs instead of being expanded to RGBA unconditionally. Transparent images retain the original alpha-preserving route.
- Added a true no-op finishing path: when denoise, sharpen, color corrections, and watermarks are disabled, no extra full-resolution processing copies are created.
- Intermediate finishing and watermark buffers are explicitly released as soon as each stage is replaced.
- JPEG export now writes existing RGB results directly instead of allocating another full-size white background image.
- Batch PNG export uses a faster lossless compression level while normal editor saves retain their previous compression behavior.
- Worker planning was calibrated from measured batches: typical JPEG/TIFF photo jobs cap near six active workers, PNG/WebP near four, while genuinely tiny files can still scale much higher.
- The Fast resize preset now uses 2× Bicubic, JPEG, and zero finishing passes.
- Batch progress now reports the encoding stage separately from resizing.
- Fixed metadata-disabled saves accidentally falling back to the image's original metadata.

## 0.9.0 RC18

Single-launcher package cleanup.

- Replaced the collection of user-facing batch files with one obvious root launcher: **ImageSuite.bat**.
- Added `--install-ai`, `--repair`, and `--debug` modes to the same launcher instead of shipping separate AI, repair, and diagnostics launchers.
- Added **Install / Repair AI** directly to Enhance; it opens the same main launcher in AI setup mode.
- Moved build, installer, release, and Git publishing scripts into `developer_tools` so they are not confused with application launchers.
- Added first-run cleanup for obsolete RC17 root launchers after an overwrite upgrade.
- Updated AI error messages and documentation to point to the in-app installer and the single launcher.

## 0.9.0 RC17

Fresh-install dependency repair and diagnostic hardening.

- Added the missing `imageio` and `imageio-ffmpeg` runtime dependencies required by GIF, MP4, and WebM support.
- Added `dependency_check.py` to report the exact missing Python package, binary/DLL import error, or ImageSuite import traceback.
- Reworked `run_imagesuite.bat` to repair existing `.venv` installations, prefer binary wheels, retry without pip cache, and preserve a detailed pip log under `%TEMP%`.
- Removed the misleading assumption that every dependency failure is caused by internet, antivirus, or proxy problems.
- Updated the AI installer to verify the same core dependencies and provide exact AI import failures.
- Added launcher regressions covering animation runtime dependencies and explicit dependency diagnostics.

## 0.9.0 RC16

AI modernization and whole-app performance hardening.

- Added AI profiles: **Balanced**, **Fast**, **Low memory**, **Maximum quality**, and **Custom**.
- Added **Auto / FP16 / FP32** precision controls. Auto uses FP16 on CUDA and FP32 elsewhere, with an automatic FP32 fallback when a model rejects FP16.
- Added automatic tile sizing based on device and available GPU/RAM, plus automatic OOM retry with progressively smaller tiles.
- Replaced full float output/weight accumulation with overlap-cropped direct image stitching to lower AI peak memory.
- Reduced AI preview input from the general 1200px preview path to a configurable 640px default while final output still uses the original source.
- Added an explicit **Check AI** action and a styled backend/model status card with model size, device, precision, tile mode, PyTorch, GPU, and VRAM information.
- Fixed animated Enhance exports reserving `.gif` even when MP4 or WebM was selected.
- Changed Organize groups to load only visible thumbnails instead of decoding every row immediately.
- Fixed a selected-image preview leak in Organize by closing the temporary PIL image after Qt conversion.
- Added cache cleanup when Organize closes.
- Added regression tests for auto tiles, OOM retries, stitched output, AI profiles, animated extensions, and lazy thumbnails.

## 0.9.0 RC15

Performance and stability optimization pass across the app.

- Reworked thumbnail loading so GIF, MP4, and WebM previews use a lightweight first-frame path instead of decoding the full animation just to show a thumbnail.
- Added draft-decoding for large static thumbnails to reduce preview-time memory pressure on big JPEG and similar files.
- Added a cached canvas pixmap path so overlay repaints no longer rebuild the underlying image raster on every paint event.
- Tightened editor preview-cache lifecycle by clearing downsample caches when documents change, tabs switch, or the editor closes.
- Fixed an Enhance bug where AI-model preview runs could retain the loaded model after preview-only work completed.
- Added regression coverage for fast animated thumbnails and AI preview cleanup.

## 0.9.0 RC14

Animation workflow and export-controls pass, with extra bug cleanup.

- Added an animation scrubber, previous/next frame buttons, and Home/End / PageUp/PageDown shortcuts in Edit.
- Added loop-preview start/end controls so playback can focus on a smaller frame range while you tune effects.
- Added export controls for animation output in Enhance: forced FPS, video bitrate, GIF palette size, GIF dithering, and GIF optimization.
- Fixed a playback regression where loop bounds could silently collapse to frame 1 and make live preview appear stuck.
- Fixed a common video-export failure path by padding odd-sized frames to even dimensions before MP4/WebM encoding.
- Added regression coverage for animation export controls and loop-bound scrubbing.

## 0.9.0 RC13

Adaptive animation import and export expansion.

- Replaced strict 10-second animated-GIF rejection with an adaptive import path that keeps long or heavy animations editable by automatically reducing frame count and, when needed, resolution.
- Added animated **MP4** and **WebM** import support across Editor and Enhance.
- Added animated **GIF**, **MP4**, and **WebM** export support. Animated documents can now be saved to those formats instead of GIF-only.
- Enhance output format now includes **MP4** and **WebM** for animated inputs.
- Added animation regression coverage for long-GIF reduction and MP4/WebM round-tripping.

## 0.9.0 RC12

Extensive batch throughput, cancellation, and memory hardening.

- Reinterpreted the 1–50 worker control as a maximum. A standard-library planner now selects the effective worker count from target dimensions, expected temporary copies, available RAM, logical CPUs, queue size, and animated-GIF cost.
- Small images can still use all 50 workers; typical photo batches are automatically limited to the range that benchmarks showed was both faster and substantially lighter on memory.
- Replaced eager submission of the entire queue with a bounded active window, improving cancellation and preventing large future queues.
- Removed the global output-write lock. Output names are now reserved atomically with `O_EXCL`, allowing independent encoders to finish without full-resolution images waiting in RAM.
- Batch outputs use faster encoding settings; normal editor saves retain size optimization. ZIP export stores already-compressed images without wasting CPU on a second compression pass.
- Removed redundant full-image copies when finishing and watermarks are disabled. Added bounded font and watermark-image caches.
- Reworked denoise to one blended 3×3 median pass instead of the extremely slow 5×5 path.
- Animated GIF batches now stream source frames instead of retaining both complete decoded and processed frame sequences.
- Reduced tiled-AI weight storage to one broadcast channel and release the cached AI model after a completed batch.
- Coalesced worker progress and Job-page refresh events to prevent the Qt event queue from growing during fast or multi-frame batches.
- Explicitly closes batch previews and processed image buffers, trims unused native memory after jobs, and retains only the first completed output path in the UI.
- Optimized Organize fingerprinting with JPEG draft decoding and bounded 512px previews; replaced eager fingerprint submission with a bounded queue.
- Replaced the 512-entry thumbnail cache with a 32 MB LRU cache and clear it between scans.
- Bounded in-session job history to 200 records.
- Added 8 new regression checks; 129 automated tests pass under PySide6 6.11.1, Pillow 12.3.0, and NumPy 2.4.6.

## 0.9.0 RC11

Enhance throughput and text watermark preset restoration.

- Moved **Parallel workers** into the normal Resize panel so it is no longer hidden behind advanced options.
- Raised the supported non-AI worker range to **1–50**, with queue-size capping and validation.
- Added clear AI-mode behavior: AI processing remains single-worker to avoid duplicate model/GPU memory contention.
- Added built-in text watermark presets: Subtle Corner, Bold Copyright, Diagonal Proof, Caption Bar, and Soft Center Mark.
- Added save, overwrite, load, and delete support for persistent custom text watermark presets.
- Custom presets preserve text, font, placement, opacity, rotation, outline, shadow, and background settings.
- Hardened preset-file loading and atomic persistence; failed saves/deletes roll back in-memory changes.
- Fixed Blueprint grid compositing so Grid Spacing visibly changes the effect.

## 0.9.0 RC10

Creative censor expansion and dynamic effect tuning improvements.

- Added four new creative censorship effects: **ASCII Art**, **Blueprint**, **Neon Edges**, and **Topographic Lines**.
- Added four new presets: **ASCII mask**, **Blueprint concealment**, **Neon wireframe**, and **Topographic concealment**.
- Reused the existing dynamic five-parameter effect system so every new effect stays fully adjustable instead of using dead or disabled sliders.
- Added deterministic engine coverage for the new effects and compile-checked the full edited source.

## 0.9.0 RC9

Live animated GIF preview while editing.

- Reworked GIF playback so animation timing and live-preview rendering run independently.
- Censor effects, complete effect chains, rectangle/lasso/face targeting, corrections, creative effects, and Quick Text now update on the currently playing GIF frame.
- Added an always-visible **Play GIF live** control below the canvas plus `Ctrl+Space` play/pause.
- Playback uses elapsed animation time and skips overdue frames when a complex effect takes longer than the source frame duration, preventing a 10-second GIF from stretching into a slow-motion preview.
- Added a bounded 48 MB cache for downscaled animation frames; still-image previews retain their existing path.
- Before/compare view now uses the current raw animation frame rather than freezing on the first frame.
- Cancel Preview keeps the GIF playing and returns to the unmodified moving animation.
- Switching documents or closing the editor stops playback and releases animation-preview cache memory.
- Added frame-position feedback and regression coverage for live effects, selections, corrections, creative effects, Quick Text, cancel behavior, current-frame Before view, global play controls, and late-frame skipping.
- 115 automated tests pass.

## 0.9.0 RC8

Animated GIF support up to 10 seconds.

- Animated GIFs up to 10,000 ms now open as complete frame sequences instead of being rejected.
- Preserves individual frame durations and loop count when saving, recovering, transferring to Enhance, and exporting enhanced GIFs.
- Area effects, effect chains, adjustments, creative transforms, Quick Text, stickers, arrows, boxes, crop, resize, rotate, reset, and cinematic bars apply consistently to every frame.
- Added lightweight Play/Pause GIF preview under the editor More menu while retaining the first frame as the precise editing surface.
- Animated edits use a cancellable progress dialog; still-image editing keeps the existing zero-overhead path.
- Brush, clone, and heal tools are disabled for animated GIFs because their frame-specific painting model cannot be safely replicated across motion.
- Added 600-frame and decoded-memory safety ceilings, plus an aggregate output-memory check in Enhance.
- Added GIF as an Enhance output option; animated input is always preserved as GIF.
- Added animated recovery support and clean removal of GIF recovery/transfer files.
- Added regression coverage for exact 10-second acceptance, over-10-second rejection, multi-page TIFF rejection, frame timing, looping, undo/redo, recovery, editor effects, playback, and Enhance output.
- 107 automated tests pass.

## 0.9.0 RC7

Rebuilt the censorship controls around meaningful per-effect parameters instead of a fixed row of disabled sliders.

- Replaced the fixed Blur/Pixel/Mosaic/Strength/Pattern controls with five dynamic parameter slots whose labels, ranges, suffixes, and visibility match the selected effect.
- Every visible parameter is regression-tested to produce a real output change; dependent controls appear only when they become meaningful.
- Expanded the curated library from 17 to 24 effects: Privacy Blur, Directional Blur, Faceted Glass, Encrypted Tiles, Prism Split, Wave Scramble, Barcode Redaction, Ordered Dither, and Photocopy join the existing high-quality effects.
- Upgraded existing effects with substantially more control: pixel color levels and grid, mosaic pre-blur/grid styling, frosted-glass refraction/grain, tunable redaction textures, angled marker/tape/halftone, directional glitches, CRT bloom/noise/separation, and adjustable silhouette/comic/thermal rendering.
- Added 15 fully tuned chain presets including Face Anonymizer, Faceted Privacy, Encrypted Glass, Barcode Concealment, Prism Interference, and Photocopy Mask.
- Chain entries preserve all five independent values and restore the exact controls when selected.
- Added Reset Effect Settings without adding another permanent toolbar.
- Fixed uint8 overflow that made color noise unexpectedly dark.
- Rebuilt Barcode Redaction around variable-width brightness-driven bars.
- Retuned Photocopy defaults so the result is usable instead of mostly black.
- Preserved RC4-RC6 text/spec compatibility through legacy parameter aliases.
- 101 automated tests pass under PySide6 6.11.1, Pillow 12.3.0, and NumPy 2.4.6.

## 0.9.0 RC6

Quality-first censorship rebuild after the RC5 visual audit.

- Removed 13 weak or broken censor entries instead of keeping effect-count filler.
- Fixed patterns that stretched a single strip/quadrant across the image.
- Removed wraparound distortion that copied pixels from the opposite image edge.
- Replaced the library with 17 visually distinct, alpha-safe effects: Soft Blur, Deep Blur, Pixelate, Mosaic, Frosted Glass, Glass Tiles, Black/White Redaction, Noise Redaction, Marker Scribble, Redaction Tape, Halftone Dots, Glitch Blocks, CRT Distortion, Silhouette, Comic Cutout, and Thermal Map.
- Added 11 retuned presets built from per-effect settings rather than one shared global value.
- Effect-chain entries now store independent Strength, Pattern Size, blur, pixel, and mosaic settings.
- Selecting a chain entry restores its controls; changing one entry no longer changes its siblings.
- Apply Outside Faces now processes the complete effect chain instead of only the current selector entry.
- Vectorized halftone rendering to avoid a Python draw loop per dot.
- Added effect descriptions and compatibility aliases for RC4/RC5 text-only chain entries.
- Expanded the release self-check to render and apply a chained censor preset.
- 95 automated tests pass, plus 1,100 randomized effect/mask cases, 800 randomized UI actions, and 62 exact preview-versus-Apply cases.

## 0.9.0 RC5

Expanded censorship engine and effect chaining.

- Added 23 new censorship and distortion effects for a total of 30 selectable effects.
- Added Box Blur, Motion Blur, Color Blocks, Frosted Glass, Low Detail, Posterize, Threshold, Halftone, Gray, Crosshatch, Scanlines, Checkerboard, Bars, Glitch, Channel Shift, Smear, Wave, Shred, Grayscale, Invert, Solarize, Edge Map, and Emboss.
- Added 11 ready-made effect-chain presets, including Maximum Privacy, Frosted Privacy, Document Redaction, Analog Signal Loss, and Digital Scramble.
- Effect chains can be reordered by dragging, and selecting a chain item exposes the controls relevant to that effect.
- Added a deliberate eight-effect chain ceiling to keep live previews responsive.
- Noise is deterministic so the applied result matches the live preview.
- Blur, pixelation, mosaic, and every new censor transform preserve the original alpha channel.
- Replaced the expensive median-filter Low Detail implementation with a native BoxBlur/posterize path.
- Fixed stale RC3 version labels in release scripts and documentation.

## 0.9.0 RC4
- Added multi-effect censorship chains. You can stack several censor effects and preview/apply them in sequence.
- Added new censorship effects: White, Noise, and Hatch.
- Live previews and Apply now use the active effect chain when present, or the current single effect otherwise.
- Added regression coverage for chained effects and the new censorship transforms.

## 0.9.0 RC3
- Startup diagnostics no longer import optional PyTorch; AI/CUDA details are loaded only when diagnostics are explicitly refreshed or copied.

Extensive live-preview, interaction, and Pillow compatibility audit.

### Live preview reliability

- Replaced trailing-edge debounce behavior with coalesced throttling, so previews update continuously while a slider is still moving.
- Prevents competing effect, correction, creative, and text timers from overwriting one another.
- Apply always recomputes from the current controls instead of committing an older pending preview.
- Automatically leaves incompatible hidden Text/Brush tools when changing task panels, without navigating away from the panel the user selected.
- The first rectangle, lasso, or face-circle change starts the selected effect preview; touching a slider first is no longer required.
- Pending callbacks cannot render on the wrong task tab or after switching to a direct brush/annotation tool.
- Changing between rectangle, lasso, and protected-face targeting refreshes the effect scope immediately.
- Checked Before view is automatically released when a setting changes, so a valid preview cannot remain hidden.
- Empty and incomplete masks target nothing instead of crashing or unexpectedly affecting the whole image.

### Performance and visual consistency

- Large-image previews use cached bounded sources while Apply remains full resolution.
- Expensive corrections and creative looks use smaller display-only sources, reducing measured Cinematic preview time to roughly one third on large test images.
- Mosaic was rewritten using native Pillow resizing instead of a Python loop per tile.
- Quick Text reuses a bounded font cache and responsive preview source.
- Glow and Sketch scale their blur radii for downsampled previews, making the displayed result better match the final full-resolution render.
- Small-image live previews are regression-tested byte-for-byte against the applied output for every effect and creative look.

### Compatibility and correctness

- Replaced the unavailable `ImageChops.divide` Sketch implementation with a supported Pillow calculation.
- Preserves alpha through Grayscale, Cinematic, Mosaic, Glow, Sketch, and the remaining creative effects.
- Fixed full-image effects on Pillow 12.3 and hardened zero-size selections after preview scaling.
- Brush sliders no longer trigger unrelated full-image previews, and selecting a different area effect safely leaves brush mode.
- Standalone editor shutdown now stops preview/autosave timers and closes the recovery executor idempotently, preventing rare process hangs after longer sessions or tests.

### Audit coverage

- Added live-preview, task-routing, cache invalidation, transparency, incomplete-mask, brush-state, and preview/apply consistency regressions.
- Completed randomized effect/mask and UI-action stress runs in addition to the normal automated suite and release self-check.
- Re-ran the full suite with the user-reported PySide6 6.11.1, Pillow 12.3.0, and NumPy 2.4.6 combination.

## 0.9.0 RC2

- Fixed full-image effect preview/application with no rectangle or lasso on Pillow 12.3.
- Added a regression test for the no-selection target mask path.
- No processing, UI, or dependency changes.

## 0.9.0 RC1

Release-candidate reliability and packaging pass.

### Safety and stability

- Fixed empty-canvas mouse movement accessing a missing document.
- Clears transient drag, brush, mask and text state when changing or closing documents.
- Reworked saved/dirty state around monotonic document revisions.
- Added approximate undo-memory ceilings in addition to history depth.
- Made recovery asynchronous and revision-aware.
- Removes ghost recovery files after Save or Discard.
- Quarantines malformed recovery pairs instead of blocking startup.
- Separates editor-to-Enhance transfer files from crash recovery.
- Added safe cancellation and shutdown for active Enhance and Organize workers.
- Added readable operation errors and a global exception/logging handler with duplicate suppression.

### Files and formats

- Preserves ICC color profiles and DPI when enabled.
- Explicitly strips personal EXIF/GPS metadata.
- Rejects animated and multi-page input instead of silently discarding frames/pages.
- Keeps atomic image and CSV writes, collision-safe output names, move rollback and copy cleanup.

### Enhance and Organize

- Validates settings and AI models before batch processing.
- Adds a 300-megapixel output safety ceiling.
- Improves AI device/model and out-of-memory messages.
- Retains failed files for retry.
- Makes partial Recycle Bin success visible and removes only successful items.
- Preserves unresolved entries when Undo Last Move can only restore part of a batch.

### Interface and accessibility

- Added Preferences for startup workspace, history limits, recovery interval, metadata, tab restoration and default output.
- Added first-run guidance and expanded diagnostics.
- Added visible keyboard focus and accessible names to primary custom controls.
- Restores clean editor tabs incrementally to avoid blocking startup.

### Packaging

- Added PyInstaller specification and Windows version metadata.
- Added portable, installer, debug and optional AI build scripts.
- Added a release self-check that runs before packaging.

## 0.7.3

- Larger eight-point handles for text, selections and face circles.
- Debounced automatic live effect, correction and text previews.
- Typed preview state prevents applying the wrong preview.

## 0.7.2

- Chronological undo/redo for rectangles, lassos and face circles.
- Mask-only history avoids copying full-resolution images.

## 0.7.1

- Fixed Windows Python discovery in all environment-creating launchers.
- Added damaged/moved virtual-environment repair.

## 0.7.0

- Unified drop/open routing, recent items, command palette and session restoration.
- Added result review, context menus, re-edit last text and undo-last-move.
- Added thumbnail and fingerprint caching.

## 0.6.0 and earlier

- Atomic saves, AI model reuse, collision-safe batch output and shared path expansion.
- Simplified progressive-disclosure interface.
- Mouse/keyboard navigation overhaul.
- Directly draggable and resizable Quick Text.
