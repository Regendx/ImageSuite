# ImageSuite 1.0 release checklist

`0.9.0 RC22` has completed the code-side reliability pass. Do not rename it to `1.0.0` until the required Windows checks below are completed against the exact packaged artifacts.

## Automated gate — completed for RC21

- [x] Full Python compilation
- [x] Release self-check
- [x] Unit and offscreen PySide6 tests
- [x] Empty-canvas mouse regression
- [x] Tab-switch interaction-state regression
- [x] Save/undo/redo dirty-state regression
- [x] Selection and face-circle undo/redo regression
- [x] Atomic save regression
- [x] Recovery write/removal regression
- [x] Damaged recovery quarantine regression
- [x] Animated GIF 10-second boundary, frame timing, loop, undo/redo, recovery, live effect/text/correction playback, frame skipping, and Enhance regressions
- [x] Multi-page TIFF rejection regression
- [x] Move rollback and duplicate-output regression
- [x] Launcher/build-script static checks
- [x] Continuous-slider live-preview regression
- [x] Preview/apply consistency for all editor effects and creative looks
- [x] Randomized image/effect/mask compatibility audit
- [x] Randomized task/tool/preview interaction audit
- [x] Pillow 12.3 full-image and Sketch compatibility regressions
- [x] Full suite under PySide6 6.11.1, Pillow 12.3.0 and NumPy 2.4.6
- [x] Standalone editor shutdown/executor regression
- [x] All 28 curated censorship effects and 19 tuned presets exercised through the PySide6 workspace
- [x] Every visible effect parameter changes output; per-entry chain settings, chained-effect fuzzing, alpha preservation and deterministic preview/apply regressions
- [x] Parallel worker range and AI single-worker behavior regression
- [x] Built-in and persistent custom text watermark preset regression
- [x] Multiline text layout, wrapping, character/line spacing, rotation, opacity, shadow, rounded background, apply/re-edit, and animation regressions
- [x] Emoji/symbol and transparent image sticker preview, move/resize, rotation, opacity, apply/re-edit, category switching, animation, and memory-lifecycle regressions
- [x] Adaptive 1–50 worker planning, bounded task submission, atomic output reservation, and long-session job-history regression
- [x] Repeated still-image and animated-GIF batch memory plateau regression
- [x] Streaming GIF batch decode and original-dimension fingerprint regression

## Windows package gate — required before 1.0

Test the exact portable ZIP and installer produced by `developer_tools\build_release.bat` and `developer_tools\build_installer.bat`.

### Clean systems

- [ ] Windows 10 x64 with no Python installed
- [ ] Windows 11 x64 with no Python installed
- [ ] Portable build launches from a normal local folder
- [ ] Installer completes without administrator rights
- [ ] Start-menu and optional desktop shortcuts work
- [ ] Uninstall removes the program folder without deleting user-created output or recovery data
- [ ] Core application launches while offline

### Core workflows

- [ ] Open, edit, undo, redo, save and Save As
- [ ] Close with Save, Discard and Cancel
- [ ] Force-terminate with unsaved edits and restore recovery next launch
- [ ] Enhance a mixed PNG/JPEG/WebP queue
- [ ] Open, edit, preview, save, recover, and Enhance animated GIFs at 1s, 5s, and 10s
- [ ] Verify GIF timing and looping in Edge, Chrome, and Windows Photos
- [ ] Cancel Enhance while reading, processing and writing
- [ ] Retry failed Enhance files
- [ ] Scan, review, move, undo move and recycle in Organize
- [ ] Cancel Organize during fingerprinting and comparison
- [ ] Close ImageSuite while each background workflow is active

### Filesystem conditions

- [ ] Unicode Windows username and Unicode filenames
- [ ] Long nested paths
- [ ] Read-only destination
- [ ] Locked destination file
- [ ] Full or nearly full disk simulation
- [ ] Source file removed during a batch
- [ ] Two sources with the same filename
- [ ] Network or removable destination disconnected during processing

### Display and accessibility

- [ ] 100%, 125%, 150% and 200% Windows scaling
- [ ] Minimum supported window size
- [ ] Mouse-only Edit/Enhance/Organize workflows
- [ ] Keyboard-only primary workflows
- [ ] Visible focus on menus, buttons, lists and form controls
- [ ] Screen-reader names verified for primary navigation, canvas, queue and result tables

### AI package gate

The core build may ship without bundled AI. Any AI-enabled package must separately pass:

- [ ] CPU AI processing
- [ ] NVIDIA CUDA processing on a supported driver/runtime
- [ ] Missing model, corrupt model and unsupported architecture errors
- [ ] CUDA unavailable after selecting CUDA
- [ ] GPU out-of-memory with actionable recovery guidance
- [ ] Tile sizes 0, 128, 256 and 512 on representative models
- [ ] Transparency and metadata behavior
- [ ] Cancel and close during AI inference

## Release decision

Promote to `1.0.0` only when:

- [ ] No known ordinary-action crash remains
- [ ] No known data-loss bug remains
- [ ] Portable and installer artifacts pass the clean-machine matrix
- [ ] Documentation matches the packaged UI
- [ ] At least several users complete Edit, Enhance and Organize without developer guidance
- [ ] Any remaining limitation is documented and does not violate the core workflow promise

- [x] Normal startup and About-page construction do not import optional PyTorch; explicit diagnostic refresh performs the AI probe.
