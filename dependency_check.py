from __future__ import annotations

import argparse
import importlib
import sys
import traceback

CORE_DEPENDENCIES = (
    ("PySide6", "PySide6"),
    ("Pillow", "PIL"),
    ("NumPy", "numpy"),
    ("Send2Trash", "send2trash"),
    ("ImageIO", "imageio"),
    ("ImageIO-FFmpeg", "imageio_ffmpeg"),
)

AI_DEPENDENCIES = (
    ("PyTorch", "torch"),
    ("Spandrel", "spandrel"),
    ("Safetensors", "safetensors"),
)


def check_modules(dependencies: tuple[tuple[str, str], ...]) -> list[str]:
    errors: list[str] = []
    for display_name, module_name in dependencies:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # Import failures can include binary/DLL errors.
            errors.append(f"{display_name} ({module_name}): {type(exc).__name__}: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify ImageSuite Python dependencies.")
    parser.add_argument("--ai", action="store_true", help="Also verify optional AI dependencies.")
    parser.add_argument("--packages-only", action="store_true", help="Skip importing the ImageSuite application.")
    parser.add_argument("--quiet", action="store_true", help="Print only failures.")
    args = parser.parse_args()

    dependencies = CORE_DEPENDENCIES + (AI_DEPENDENCIES if args.ai else ())
    errors = check_modules(dependencies)
    if errors:
        print("ImageSuite dependency check failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    if not args.packages_only:
        try:
            importlib.import_module("imagesuite.main_window")
        except Exception:
            print("The packages imported, but ImageSuite itself could not be imported:")
            traceback.print_exc()
            return 2

    if not args.quiet:
        label = "Core and AI dependencies" if args.ai else "Core dependencies"
        print(f"{label} are installed and import correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
