from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import time
from threading import Lock

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from imagesuite.utils import image_files, unique_destination


_FINGERPRINT_CACHE: dict[tuple[str, int, int], "FileFingerprint"] = {}
_FINGERPRINT_CACHE_LIMIT = 4096
_FINGERPRINT_CACHE_LOCK = Lock()


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    size_bytes: int
    width: int
    height: int
    mtime: float
    a_hash: int
    d_hash: int
    color_hist: tuple[float, ...]
    sharpness: float

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000


@dataclass
class SimilarImage:
    fp: FileFingerprint
    score_to_anchor: float = 100.0
    checked: bool = False


@dataclass
class SimilarityGroup:
    id: int
    anchor: FileFingerprint
    members: list[SimilarImage] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def total_bytes(self) -> int:
        return sum(m.fp.size_bytes for m in self.members)

    @property
    def min_score(self) -> float:
        return min((m.score_to_anchor for m in self.members), default=100.0)


def _bits_to_int(bits: Iterable[bool]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return value


def _average_hash(img: Image.Image, hash_size: int = 8) -> int:
    arr = np.asarray(img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS), dtype=np.float32)
    return _bits_to_int(arr.flatten() >= arr.mean())


def _difference_hash(img: Image.Image, hash_size: int = 8) -> int:
    arr = np.asarray(img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS), dtype=np.float32)
    return _bits_to_int((arr[:, 1:] > arr[:, :-1]).flatten())


def _color_histogram(img: Image.Image, bins: int = 8) -> tuple[float, ...]:
    arr = np.asarray(img.convert("RGB").resize((128, 128), Image.Resampling.BILINEAR), dtype=np.uint8)
    parts: list[np.ndarray] = []
    for channel in range(3):
        hist, _ = np.histogram(arr[:, :, channel], bins=bins, range=(0, 256))
        hist = hist.astype(np.float32)
        total = hist.sum()
        if total:
            hist /= total
        parts.append(hist)
    return tuple(float(x) for x in np.concatenate(parts))


def _sharpness(img: Image.Image) -> float:
    arr = np.asarray(img.convert("L").resize((256, 256), Image.Resampling.BILINEAR), dtype=np.float32)
    center = arr[1:-1, 1:-1]
    lap = arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:] - 4.0 * center
    return float(lap.var())


def fingerprint(path: str | Path) -> FileFingerprint:
    p = Path(path)
    stat = p.stat()
    key = (str(p), stat.st_size, stat.st_mtime_ns)
    with _FINGERPRINT_CACHE_LOCK:
        cached = _FINGERPRINT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with Image.open(p) as raw:
            width, height = raw.size
            try:
                orientation = int(raw.getexif().get(274, 1) or 1)
            except Exception:
                orientation = 1
            if orientation in {5, 6, 7, 8}:
                width, height = height, width
            # JPEG draft decoding and an early thumbnail avoid keeping the full
            # source raster alive merely to compute 8×8/256×256 fingerprints.
            try:
                raw.draft("RGB", (512, 512))
            except Exception:
                pass
            preview = ImageOps.exif_transpose(raw)
            preview.thumbnail((512, 512), Image.Resampling.LANCZOS)
            img = preview.convert("RGB")
            try:
                result = FileFingerprint(str(p), stat.st_size, width, height, stat.st_mtime, _average_hash(img), _difference_hash(img), _color_histogram(img), _sharpness(img))
            finally:
                img.close()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"Could not read image: {p} ({exc})") from exc
    # Ponytail: bounded in-memory cache only. A persistent cache is the upgrade path
    # if repeated scans across application restarts become the measured bottleneck.
    with _FINGERPRINT_CACHE_LOCK:
        if len(_FINGERPRINT_CACHE) >= _FINGERPRINT_CACHE_LIMIT:
            _FINGERPRINT_CACHE.clear()
        _FINGERPRINT_CACHE[key] = result
    return result


def _visual_similarity(left: FileFingerprint, right: FileFingerprint, threshold: float | None = None) -> float | None:
    a_sim = 1.0 - ((left.a_hash ^ right.a_hash).bit_count() / 64.0)
    d_sim = 1.0 - ((left.d_hash ^ right.d_hash).bit_count() / 64.0)
    if threshold is not None and ((0.38 * a_sim) + (0.42 * d_sim) + 0.20) * 100 < threshold:
        return None
    # 24 Python floats are cheaper to compare directly than allocating two NumPy arrays per pair.
    histogram_distance = sum(abs(a - b) for a, b in zip(left.color_hist, right.color_hist))
    h_sim = max(0.0, min(1.0, 1.0 - histogram_distance / 6.0))
    return round(max(0.0, min(100.0, ((0.38 * a_sim) + (0.42 * d_sim) + (0.20 * h_sim)) * 100)), 2)


def visual_similarity(left: FileFingerprint, right: FileFingerprint) -> float:
    return float(_visual_similarity(left, right) or 0.0)


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n)); self.rank = [0] * n
    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]; x = self.parent[x]
        return x
    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a == b: return
        if self.rank[a] < self.rank[b]: a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]: self.rank[a] += 1


def build_groups(fingerprints: list[FileFingerprint], threshold: float, progress: Callable[[str, int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> list[SimilarityGroup]:
    n = len(fingerprints)
    if n < 2: return []
    uf = UnionFind(n)
    total = n * (n - 1) // 2
    done = 0; last_emit = 0.0
    # Ponytail: exact pairwise comparison is O(n²). Upgrade to approximate-neighbor
    # candidate bucketing only when real libraries make this accuracy-first path too slow.
    for i in range(n):
        if cancelled and cancelled():
            return []
        for j in range(i + 1, n):
            if done % 256 == 0 and cancelled and cancelled():
                return []
            score = _visual_similarity(fingerprints[i], fingerprints[j], threshold)
            if score is not None and score >= threshold:
                uf.union(i, j)
            done += 1
        now = time.monotonic()
        if progress and (now - last_emit > 0.15 or i == n - 1):
            progress("Comparing", done, total); last_emit = now
    buckets: dict[int, list[int]] = {}
    for index in range(n): buckets.setdefault(uf.find(index), []).append(index)
    groups: list[SimilarityGroup] = []
    for indexes in buckets.values():
        if len(indexes) < 2: continue
        anchor_index = max(indexes, key=lambda ix: (fingerprints[ix].width * fingerprints[ix].height, fingerprints[ix].size_bytes, fingerprints[ix].sharpness))
        anchor = fingerprints[anchor_index]
        members = [SimilarImage(fp=fingerprints[ix], score_to_anchor=visual_similarity(anchor, fingerprints[ix])) for ix in indexes]
        members.sort(key=lambda m: (-m.score_to_anchor, -(m.fp.width * m.fp.height), m.fp.path.lower()))
        groups.append(SimilarityGroup(0, anchor, members))
    groups.sort(key=lambda g: (-g.count, -g.total_bytes, g.anchor.path.lower()))
    for i, group in enumerate(groups, 1): group.id = i
    return groups


def scan_folder(root: str | Path, recursive: bool, threshold: float, workers: int, progress: Callable[[str, int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> tuple[list[SimilarityGroup], list[str]]:
    paths = image_files(root, recursive)
    if progress:
        progress("Found images", len(paths), len(paths))
    fingerprints: list[FileFingerprint] = []
    errors: list[str] = []
    if not paths:
        return [], errors
    worker_count = max(1, min(len(paths), int(workers), max(2, (os.cpu_count() or 4) * 2)))
    iterator = iter(paths)
    pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="imagesuite-fingerprint")
    pending: dict[object, Path] = {}

    def submit_next() -> bool:
        if cancelled and cancelled():
            return False
        try:
            path = next(iterator)
        except StopIteration:
            return False
        pending[pool.submit(fingerprint, path)] = path
        return True

    for _ in range(min(len(paths), worker_count * 2)):
        if not submit_next():
            break
    done_count = 0
    last_emit = 0.0
    try:
        while pending:
            if cancelled and cancelled():
                return [], errors
            finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in finished:
                path = pending.pop(future)
                try:
                    fingerprints.append(future.result())
                except Exception as exc:
                    errors.append(f"{path}: {exc}")
                done_count += 1
                now = time.monotonic()
                if progress and (now - last_emit >= 0.08 or done_count == len(paths)):
                    progress("Fingerprinting", done_count, len(paths))
                    last_emit = now
                submit_next()
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        pending.clear()
    if cancelled and cancelled():
        return [], errors
    return build_groups(fingerprints, threshold, progress, cancelled), errors


def format_bytes(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB": return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{num} B"


def choose_best(group: SimilarityGroup, rule: str) -> int:
    key_map = {
        "Keep highest resolution": lambda m: (m.fp.width * m.fp.height, m.fp.size_bytes, m.fp.sharpness),
        "Keep sharpest": lambda m: (m.fp.sharpness, m.fp.width * m.fp.height),
        "Keep newest": lambda m: (m.fp.mtime,),
        "Keep oldest": lambda m: (-m.fp.mtime,),
        "Keep largest file": lambda m: (m.fp.size_bytes,),
        "Keep smallest file": lambda m: (-m.fp.size_bytes,),
        "Keep shortest path": lambda m: (-len(m.fp.path),),
    }
    return max(range(len(group.members)), key=lambda i: key_map[rule](group.members[i]))


def move_files(paths: list[str], destination: str | Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    try:
        for source in paths:
            target = unique_destination(destination, source)
            shutil.move(source, target)
            results.append((source, str(target)))
    except Exception:
        rollback_errors: list[str] = []
        for original, moved in reversed(results):
            try:
                if Path(moved).exists() and not Path(original).exists():
                    Path(original).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(moved, original)
            except Exception as rollback_error:
                rollback_errors.append(f"{moved}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError("Move failed and some files could not be restored: " + "; ".join(rollback_errors))
        raise
    return results


def copy_files(paths: list[str], destination: str | Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    try:
        for source in paths:
            target = unique_destination(destination, source)
            shutil.copy2(source, target)
            results.append((source, str(target)))
    except Exception:
        for _source, copied in reversed(results):
            Path(copied).unlink(missing_ok=True)
        raise
    return results


def recycle_files(paths: list[str]) -> tuple[list[str], list[str]]:
    try: from send2trash import send2trash
    except Exception as exc: raise RuntimeError("Install send2trash to use the Recycle Bin action.") from exc
    recycled: list[str] = []
    errors: list[str] = []
    for path in paths:
        try:
            send2trash(path)
            recycled.append(path)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return recycled, errors


def open_file(path: str) -> None:
    if sys.platform.startswith("win"): os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin": subprocess.Popen(["open", path])
    else: subprocess.Popen(["xdg-open", path])


def reveal_file(path: str) -> None:
    if sys.platform.startswith("win"): subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin": subprocess.Popen(["open", "-R", path])
    else: subprocess.Popen(["xdg-open", str(Path(path).parent)])


def export_csv(groups: list[SimilarityGroup], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False, mode="w", newline="", encoding="utf-8-sig") as handle:
        temporary = Path(handle.name)
        writer = csv.writer(handle)
        writer.writerow(["group","checked","score_to_anchor","path","anchor_path","width","height","megapixels","size_bytes","mtime","sharpness"])
        for group in groups:
            for member in group.members:
                fp = member.fp
                writer.writerow([group.id, member.checked, member.score_to_anchor, fp.path, group.anchor.path, fp.width, fp.height, f"{fp.megapixels:.3f}", fp.size_bytes, fp.mtime, f"{fp.sharpness:.2f}"])
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
