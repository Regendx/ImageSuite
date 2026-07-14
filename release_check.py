"""Small release self-check used before packaging ImageSuite."""
from __future__ import annotations

import compileall
import os
from pathlib import Path
import tempfile

_SELF_CHECK_DATA = tempfile.TemporaryDirectory(prefix="imagesuite-self-check-data-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = _SELF_CHECK_DATA.name
os.environ["XDG_DATA_HOME"] = _SELF_CHECK_DATA.name

from PIL import Image
from PySide6.QtWidgets import QApplication

from imagesuite import __version__
from imagesuite.diagnostics import diagnostics_report
from imagesuite.main_window import MainWindow
from imagesuite.models import RectMask
from imagesuite.utils import read_image, save_animation


def main() -> int:
    root = Path(__file__).resolve().parent
    if not compileall.compile_dir(root, quiet=1):
        raise RuntimeError("Python compilation failed")

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="imagesuite-release-check-") as raw:
        base = Path(raw)
        (base / "portable.flag").touch()
        window = MainWindow(base)
        for index in range(window.stack.count()):
            window.switch_page(index)
            app.processEvents()

        window.editor.add_document(Image.new("RGBA", (96, 72), "white"), dirty=False)
        document = window.editor.document
        assert document is not None
        document.push_mask_undo()
        document.selection = RectMask(5, 5, 40, 35)
        assert document.undo() and document.selection is None
        assert document.redo() and document.selection == RectMask(5, 5, 40, 35)
        document.selection = None
        window.editor.effect_preset_combo.setCurrentText("Digital scramble")
        window.editor.preview_effect()
        assert window.editor.canvas.preview_image is not None
        preview_bytes = window.editor.canvas.preview_image.tobytes()
        window.editor.apply_effect()
        assert document.image.tobytes() == preview_bytes

        gif_path = base / "check.gif"
        gif_frames = [Image.new("RGB", (20, 16), "red"), Image.new("RGB", (20, 16), "blue")]
        gif_frames[0].save(gif_path, save_all=True, append_images=gif_frames[1:], duration=[100, 200], loop=1)
        window.editor.add_document(read_image(gif_path), gif_path, dirty=False)
        animated = window.editor.document
        assert animated is not None and animated.is_animated and animated.animation_duration_ms == 300
        window.editor.effect_combo.setCurrentText("Pixelate")
        window.editor.apply_effect()
        assert animated.frame_count == 2
        gif_output = base / "check-output.gif"
        save_animation(animated.animation_frames, animated.frame_durations, gif_output, loop=animated.animation_loop)
        with Image.open(gif_output) as saved_gif:
            assert saved_gif.n_frames == 2

        assert f"ImageSuite: {__version__}" in diagnostics_report(base)

        for open_document in window.editor.documents:
            open_document.mark_saved()
        window.close()
        app.processEvents()

    print(f"ImageSuite {__version__} release self-check passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _SELF_CHECK_DATA.cleanup()
