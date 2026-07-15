from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Callable, Optional
import uuid

from PIL import Image

from imagesuite.utils import (
    HARD_ANIMATION_FRAMES,
    VIDEO_SOURCE_DURATION_MS_KEY,
    VIDEO_SOURCE_PATH_KEY,
    VIDEO_SOURCE_START_MS_KEY,
    available_memory_bytes,
)


ANIMATION_FRAMES_KEY = "_imagesuite_animation_frames"
ANIMATION_DURATIONS_KEY = "_imagesuite_animation_durations"
ANIMATION_LOOP_KEY = "_imagesuite_animation_loop"
MIN_ANIMATION_EDIT_PIXELS = 96_000_000
MAX_ANIMATION_EDIT_PIXELS = 384_000_000


@dataclass
class RectMask:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    def normalized(self) -> "RectMask":
        return RectMask(
            min(self.left, self.right),
            min(self.top, self.bottom),
            max(self.left, self.right),
            max(self.top, self.bottom),
        )

    def copy(self) -> "RectMask":
        return RectMask(*self.box)


@dataclass
class HistoryState:
    image: Image.Image | None
    animation_tail: list[Image.Image] | None
    frame_durations: list[int] | None
    image_revision: int | None
    selection: Optional[RectMask]
    face_masks: list[RectMask]
    lasso_points: list[tuple[int, int]]

    @property
    def approximate_bytes(self) -> int:
        if self.image is None:
            return 0
        bands = max(1, len(self.image.getbands()))
        total = self.image.width * self.image.height * bands
        if self.animation_tail:
            total += sum(frame.width * frame.height * max(1, len(frame.getbands())) for frame in self.animation_tail)
        return total

    def close(self) -> None:
        images = ([self.image] if self.image is not None else []) + list(self.animation_tail or [])
        seen: set[int] = set()
        for image in images:
            if id(image) in seen:
                continue
            seen.add(id(image))
            try:
                image.close()
            except Exception:
                pass
        self.image = None
        self.animation_tail = None
        self.frame_durations = None


@dataclass(frozen=True)
class AnimationEditSummary:
    original_frames: int
    output_frames: int
    original_size: tuple[int, int]
    output_size: tuple[int, int]
    stride: int = 1
    scale: float = 1.0
    cancelled: bool = False

    @property
    def reduced(self) -> bool:
        return self.stride > 1 or self.scale < 0.999

    @property
    def description(self) -> str:
        if self.cancelled:
            return "Animation edit cancelled"
        if not self.reduced:
            return ""
        parts: list[str] = []
        if self.output_frames != self.original_frames:
            parts.append(f"{self.original_frames}→{self.output_frames} frames")
        if self.output_size != self.original_size:
            parts.append(
                f"{self.original_size[0]}×{self.original_size[1]}→"
                f"{self.output_size[0]}×{self.output_size[1]}"
            )
        return "Optimized animation for editing: " + ", ".join(parts) + "; playback duration preserved"


def animation_edit_budget_pixels() -> int:
    """Budget one new RGBA frame set from currently available memory."""
    available = max(512 * 1024**2, available_memory_bytes())
    dynamic = int(available * 0.28) // 4
    return max(MIN_ANIMATION_EDIT_PIXELS, min(MAX_ANIMATION_EDIT_PIXELS, dynamic))


def _plan_animation_reduction(
    frame_count: int,
    durations: list[int],
    width: int,
    height: int,
    budget_pixels: int,
) -> tuple[int, float]:
    total_pixels = max(1, frame_count * width * height)
    if total_pixels <= budget_pixels:
        return 1, 1.0

    total_ms = max(1, sum(durations) if durations else frame_count * 100)
    fps = frame_count * 1000 / total_ms
    max_stride_near_ten_fps = max(1, int(fps / 10.0))
    required_stride = max(1, math.ceil(total_pixels / budget_pixels))
    stride = min(required_stride, max_stride_near_ten_fps)

    kept = max(2, math.ceil(frame_count / stride))
    remaining = width * height * kept
    scale = min(1.0, math.sqrt(budget_pixels / max(1, remaining)))

    # Prefer at least half resolution. If that cannot fit, sacrifice more frames
    # before shrinking the working copy further.
    if scale < 0.5 and frame_count > 2:
        max_frames_at_half = max(2, budget_pixels // max(1, int(width * height * 0.25)))
        stride = max(stride, math.ceil(frame_count / max_frames_at_half))
        stride = min(stride, max(1, math.ceil(frame_count / 2)))
        kept = max(2, math.ceil(frame_count / stride))
        remaining = width * height * kept
        scale = min(1.0, math.sqrt(budget_pixels / max(1, remaining)))

    scale = max(0.25, scale)
    while width * height * max(2, math.ceil(frame_count / stride)) * scale * scale > budget_pixels and stride < math.ceil(frame_count / 2):
        stride += 1
    kept = max(2, math.ceil(frame_count / stride))
    scale = min(scale, math.sqrt(budget_pixels / max(1, width * height * kept)))
    return max(1, stride), max(0.25, min(1.0, scale))


@dataclass
class ImageDocument:
    image: Image.Image
    original_image: Image.Image
    path: Optional[Path] = None
    metadata: dict[str, object] = field(default_factory=dict)
    animation_frames: list[Image.Image] = field(default_factory=list)
    original_animation_frames: list[Image.Image] = field(default_factory=list)
    frame_durations: list[int] = field(default_factory=list)
    original_frame_durations: list[int] = field(default_factory=list)
    animation_loop: int = 0
    dirty: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    undo_stack: list[HistoryState] = field(default_factory=list)
    redo_stack: list[HistoryState] = field(default_factory=list)
    selection: Optional[RectMask] = None
    face_masks: list[RectMask] = field(default_factory=list)
    lasso_points: list[tuple[int, int]] = field(default_factory=list)
    zoom: float = 1.0
    max_history: int = 30
    max_history_bytes: int = 512 * 1024 * 1024
    image_revision: int = 0
    saved_revision: int = 0
    next_revision: int = 0

    @classmethod
    def from_image(cls, image: Image.Image, path: Optional[Path] = None) -> "ImageDocument":
        raw_frames = image.info.get(ANIMATION_FRAMES_KEY, [])
        animation_frames = [frame.convert("RGBA") for frame in raw_frames] if isinstance(raw_frames, list) else []
        if animation_frames:
            durations_raw = image.info.get(ANIMATION_DURATIONS_KEY, [])
            durations = [max(10, int(value or 100)) for value in durations_raw] if isinstance(durations_raw, list) else []
            if len(durations) != len(animation_frames):
                durations = [100] * len(animation_frames)
            loop = int(image.info.get(ANIMATION_LOOP_KEY, 0) or 0)
            originals = [frame.copy() for frame in animation_frames]
            return cls(
                image=animation_frames[0],
                original_image=originals[0],
                path=path,
                metadata={
                    key: image.info[key]
                    for key in (
                        "icc_profile",
                        "dpi",
                        VIDEO_SOURCE_PATH_KEY,
                        VIDEO_SOURCE_START_MS_KEY,
                        VIDEO_SOURCE_DURATION_MS_KEY,
                    )
                    if key in image.info and image.info[key] is not None
                },
                animation_frames=animation_frames,
                original_animation_frames=originals,
                frame_durations=list(durations),
                original_frame_durations=list(durations),
                animation_loop=loop,
            )

        rgba = image.convert("RGBA")
        metadata = {
            key: image.info[key]
            for key in ("icc_profile", "dpi")
            if key in image.info and image.info[key]
        }
        return cls(image=rgba, original_image=rgba.copy(), path=path, metadata=metadata)

    @property
    def is_animated(self) -> bool:
        return len(self.animation_frames) > 1

    @property
    def frame_count(self) -> int:
        return len(self.animation_frames) if self.is_animated else 1

    @property
    def animation_duration_ms(self) -> int:
        return sum(self.frame_durations) if self.is_animated else 0

    @property
    def direct_video_source(self) -> Optional[Path]:
        source = self.source_video
        if source is None or self.image_revision != 0:
            return None
        return source

    @property
    def source_video(self) -> Optional[Path]:
        """Return the imported source video even after pixel edits.

        Direct video export requires revision zero, but source audio can still
        be remuxed onto a rendered edited video. Keeping those capabilities
        separate avoids disabling audio preservation after the first edit.
        """
        raw = self.metadata.get(VIDEO_SOURCE_PATH_KEY)
        if not raw or not self.is_animated:
            return None
        source = Path(str(raw))
        return source if source.suffix.lower() in {".mp4", ".webm"} and source.exists() else None

    @property
    def direct_video_start_ms(self) -> int:
        return max(0, int(self.metadata.get(VIDEO_SOURCE_START_MS_KEY, 0) or 0))

    @property
    def direct_video_duration_ms(self) -> int:
        return max(0, int(self.metadata.get(VIDEO_SOURCE_DURATION_MS_KEY, self.animation_duration_ms) or 0))

    def frames(self) -> list[Image.Image]:
        return self.animation_frames if self.is_animated else [self.image]

    @property
    def display_name(self) -> str:
        name = self.path.name if self.path else "Untitled"
        return f"{name}{' *' if self.dirty else ''}"

    def mark_unsaved(self) -> None:
        if self.image_revision == self.saved_revision:
            self.next_revision += 1
            self.image_revision = self.next_revision
        self.dirty = True

    def mark_saved(self) -> None:
        self.saved_revision = self.image_revision
        self.dirty = False

    def snapshot(self, image: Image.Image | None = None, *, masks_only: bool = False) -> HistoryState:
        return HistoryState(
            image=None if masks_only else (self.image if image is None else image).copy(),
            animation_tail=None if masks_only or not self.is_animated else [frame.copy() for frame in self.animation_frames[1:]],
            frame_durations=None if masks_only or not self.is_animated else list(self.frame_durations),
            image_revision=None if masks_only else self.image_revision,
            selection=self.selection.copy() if self.selection else None,
            face_masks=[mask.copy() for mask in self.face_masks],
            lasso_points=list(self.lasso_points),
        )

    def _take_state(self, *, masks_only: bool = False, image: Image.Image | None = None) -> HistoryState:
        if masks_only:
            state_image = None
            tail = None
            durations = None
            revision = None
        elif image is not None:
            state_image = image
            tail = None
            durations = None
            revision = self.image_revision
        elif self.is_animated:
            state_image = self.animation_frames[0]
            tail = list(self.animation_frames[1:])
            durations = list(self.frame_durations)
            revision = self.image_revision
        else:
            state_image = self.image
            tail = None
            durations = None
            revision = self.image_revision
        return HistoryState(
            image=state_image,
            animation_tail=tail,
            frame_durations=durations,
            image_revision=revision,
            selection=self.selection.copy() if self.selection else None,
            face_masks=[mask.copy() for mask in self.face_masks],
            lasso_points=list(self.lasso_points),
        )

    @staticmethod
    def _close_stack(stack: list[HistoryState]) -> None:
        for state in stack:
            state.close()
        stack.clear()

    def _trim_stack(self, stack: list[HistoryState]) -> None:
        while len(stack) > self.max_history:
            stack.pop(0).close()
        total = sum(state.approximate_bytes for state in stack)
        while len(stack) > 1 and total > self.max_history_bytes:
            removed = stack.pop(0)
            total -= removed.approximate_bytes
            removed.close()

    def _push_history(self, state: HistoryState) -> None:
        self.undo_stack.append(state)
        self._trim_stack(self.undo_stack)
        self._close_stack(self.redo_stack)

    def push_undo(self, image: Image.Image | None = None) -> None:
        self._push_history(self.snapshot(image))

    def push_mask_undo(self) -> None:
        self._push_history(self.snapshot(masks_only=True))

    def _scale_geometry(self, scale_x: float, scale_y: float) -> None:
        if abs(scale_x - 1.0) < 0.001 and abs(scale_y - 1.0) < 0.001:
            return

        def scale_mask(mask: RectMask) -> RectMask:
            return RectMask(
                round(mask.left * scale_x),
                round(mask.top * scale_y),
                round(mask.right * scale_x),
                round(mask.bottom * scale_y),
            ).normalized()

        self.selection = scale_mask(self.selection) if self.selection else None
        self.face_masks = [scale_mask(mask) for mask in self.face_masks]
        self.lasso_points = [(round(x * scale_x), round(y * scale_y)) for x, y in self.lasso_points]

    def commit_frames(
        self,
        frames: list[Image.Image],
        *,
        durations: list[int] | None = None,
        clear_masks: bool = False,
        geometry_scale: tuple[float, float] = (1.0, 1.0),
    ) -> None:
        if not frames:
            raise ValueError("An animation must contain at least one frame.")
        normalized: list[Image.Image] = []
        for frame in frames:
            if frame.mode == "RGBA":
                normalized.append(frame)
            else:
                converted = frame.convert("RGBA")
                frame.close()
                normalized.append(converted)
        duration_values = [max(10, int(value or 100)) for value in (durations or self.frame_durations)]
        if len(normalized) > 1 and len(duration_values) != len(normalized):
            duration_values = [100] * len(normalized)

        self._push_history(self._take_state())
        self.image = normalized[0]
        self.animation_frames = normalized if len(normalized) > 1 else []
        self.frame_durations = duration_values if len(normalized) > 1 else []
        self.next_revision += 1
        self.image_revision = self.next_revision
        self.dirty = self.image_revision != self.saved_revision
        if clear_masks:
            self.selection = None
            self.face_masks.clear()
            self.lasso_points.clear()
        else:
            self._scale_geometry(*geometry_scale)

    def extend_animation(
        self,
        target_duration_ms: int,
        *,
        mode: str = "repeat",
        range_start: int = 0,
        range_end: int | None = None,
        max_frames: int = HARD_ANIMATION_FRAMES,
    ) -> int:
        """Extend an animation to an exact duration and return added frames.

        ``repeat`` appends the selected frame range until the requested duration
        is reached. ``hold`` keeps the frame count unchanged and lengthens the
        final frame. The operation commits through normal document history, so
        Undo/Redo and recovery keep working without a second animation model.
        """
        if not self.is_animated:
            raise ValueError("Only animated documents can be extended.")

        target = int(target_duration_ms)
        current = self.animation_duration_ms
        if target <= current:
            raise ValueError("The new duration must be longer than the current animation.")

        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"repeat", "hold"}:
            raise ValueError(f"Unsupported animation extension mode: {mode}")

        source_frames = list(self.animation_frames)
        source_durations = [max(10, int(value or 100)) for value in self.frame_durations]
        extra = target - current

        if normalized_mode == "hold":
            new_frames = [frame.copy() for frame in source_frames]
            try:
                new_durations = list(source_durations)
                new_durations[-1] += extra
                self.commit_frames(new_frames, durations=new_durations)
                return 0
            except Exception:
                for frame in new_frames:
                    try:
                        frame.close()
                    except Exception:
                        pass
                raise

        start = max(0, min(len(source_frames) - 1, int(range_start)))
        end_value = len(source_frames) - 1 if range_end is None else int(range_end)
        end = max(start, min(len(source_frames) - 1, end_value))
        cycle_frames = source_frames[start:end + 1]
        cycle_durations = source_durations[start:end + 1]
        cycle_ms = sum(cycle_durations)
        if not cycle_frames or cycle_ms <= 0:
            raise ValueError("The selected animation range is empty.")

        full_cycles, remainder = divmod(extra, cycle_ms)
        additional_count = full_cycles * len(cycle_frames)
        remaining_for_count = remainder
        if remaining_for_count:
            for duration in cycle_durations:
                if remaining_for_count < 10:
                    break
                additional_count += 1
                remaining_for_count -= min(duration, remaining_for_count)
                if remaining_for_count <= 0:
                    break

        output_count = len(source_frames) + additional_count
        pixel_budget_frames = max(
            len(source_frames),
            animation_edit_budget_pixels() // max(1, self.image.width * self.image.height),
        )
        safe_frame_limit = max(len(source_frames), min(max_frames, pixel_budget_frames))
        if output_count > safe_frame_limit:
            max_repeat_seconds = current / 1000 + (
                max(0, safe_frame_limit - len(source_frames)) * cycle_ms
                / max(1, len(cycle_frames))
                / 1000
            )
            raise ValueError(
                f"That extension would create {output_count:,} editable frames. "
                f"This document is currently limited to about {safe_frame_limit:,} frames "
                f"({max_repeat_seconds:.1f}s with this loop). Use Hold final frame, "
                "shorten the target, or lower the animation resolution first."
            )

        new_frames: list[Image.Image] = []
        try:
            new_frames.extend(frame.copy() for frame in source_frames)
            new_durations = list(source_durations)
            for _ in range(full_cycles):
                new_frames.extend(frame.copy() for frame in cycle_frames)
                new_durations.extend(cycle_durations)

            remaining = remainder
            for frame, duration in zip(cycle_frames, cycle_durations):
                if remaining <= 0:
                    break
                # GIF timing is stored in 10 ms units. Avoid creating a frame
                # that commit_frames would round up and make the result longer.
                if remaining < 10:
                    new_durations[-1] += remaining
                    remaining = 0
                    break
                frame_duration = min(duration, remaining)
                new_frames.append(frame.copy())
                new_durations.append(frame_duration)
                remaining -= frame_duration

            self.commit_frames(new_frames, durations=new_durations)
            return len(new_frames) - len(source_frames)
        except Exception:
            for frame in new_frames:
                try:
                    frame.close()
                except Exception:
                    pass
            raise

    @staticmethod
    def _transform_owned(frame: Image.Image, transform: Callable[[Image.Image], Image.Image]) -> Image.Image:
        source = frame.copy()
        result: Image.Image | None = None
        try:
            result = transform(source)
            if result is not source:
                source.close()
            if result.mode == "RGBA":
                return result
            converted = result.convert("RGBA")
            result.close()
            return converted
        except Exception:
            if result is not None and result is not source:
                try:
                    result.close()
                except Exception:
                    pass
            try:
                source.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _resize_owned(frame: Image.Image, scale: float) -> Image.Image:
        if scale >= 0.999:
            return frame
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        resized = frame.resize(size, Image.Resampling.LANCZOS)
        frame.close()
        return resized

    def apply_transform(
        self,
        transform: Callable[[Image.Image], Image.Image],
        *,
        clear_masks: bool = False,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        budget_pixels: int | None = None,
    ) -> AnimationEditSummary | None:
        if not self.is_animated:
            self.commit(self._transform_owned(self.image, transform), clear_masks=clear_masks)
            return None

        source_frames = list(self.animation_frames)
        source_durations = list(self.frame_durations)
        original_count = len(source_frames)
        original_size = source_frames[0].size
        output_frames: list[Image.Image] = []
        first_result: Image.Image | None = None
        try:
            if cancelled and cancelled():
                return AnimationEditSummary(original_count, original_count, original_size, original_size, cancelled=True)
            first_result = self._transform_owned(source_frames[0], transform)
            budget = max(1, int(budget_pixels or animation_edit_budget_pixels()))
            stride, scale = _plan_animation_reduction(
                original_count,
                source_durations,
                first_result.width,
                first_result.height,
                budget,
            )
            output_size = (
                max(1, round(first_result.width * scale)),
                max(1, round(first_result.height * scale)),
            )
            output_frames.append(self._resize_owned(first_result, scale))
            first_result = None
            output_durations = [sum(source_durations[0:stride])]
            if progress:
                progress(min(stride, original_count), original_count)

            for start in range(stride, original_count, stride):
                if cancelled and cancelled():
                    for frame in output_frames:
                        frame.close()
                    return AnimationEditSummary(
                        original_count,
                        len(output_frames),
                        original_size,
                        output_size,
                        stride,
                        scale,
                        cancelled=True,
                    )
                transformed = self._transform_owned(source_frames[start], transform)
                output_frames.append(self._resize_owned(transformed, scale))
                output_durations.append(sum(source_durations[start:start + stride]))
                if progress:
                    progress(min(start + stride, original_count), original_count)

            summary = AnimationEditSummary(
                original_count,
                len(output_frames),
                original_size,
                output_size,
                stride,
                scale,
            )
            scale_x = output_size[0] / max(1, original_size[0])
            scale_y = output_size[1] / max(1, original_size[1])
            self.commit_frames(
                output_frames,
                durations=output_durations,
                clear_masks=clear_masks,
                geometry_scale=(scale_x, scale_y),
            )
            return summary
        except Exception:
            if first_result is not None:
                first_result.close()
            for frame in output_frames:
                try:
                    frame.close()
                except Exception:
                    pass
            raise

    def commit(self, image: Image.Image, *, clear_masks: bool = False) -> None:
        if self.is_animated:
            raise RuntimeError("Animated edits must be applied to every frame.")
        if image.mode == "RGBA":
            normalized = image
        else:
            normalized = image.convert("RGBA")
            image.close()
        self._push_history(self._take_state())
        self.image = normalized
        self.animation_frames = []
        self.frame_durations = []
        self.next_revision += 1
        self.image_revision = self.next_revision
        self.dirty = self.image_revision != self.saved_revision
        if clear_masks:
            self.selection = None
            self.face_masks.clear()
            self.lasso_points.clear()

    def commit_in_place(self, previous_image: Image.Image) -> None:
        """Finalize an operation that already modified ``self.image`` in place."""
        if self.is_animated:
            raise RuntimeError("Brush, clone, and heal tools are not available for animations.")
        self._push_history(self._take_state(image=previous_image))
        self.next_revision += 1
        self.image_revision = self.next_revision
        self.dirty = self.image_revision != self.saved_revision

    def _restore(self, state: HistoryState) -> None:
        if state.image is not None:
            self.image = state.image
            if state.animation_tail is not None:
                self.animation_frames = [state.image, *state.animation_tail]
                self.frame_durations = list(state.frame_durations or [100] * len(self.animation_frames))
            else:
                self.animation_frames = []
                self.frame_durations = []
            self.image_revision = int(state.image_revision or 0)
            self.next_revision = max(self.next_revision, self.image_revision)
            self.dirty = self.image_revision != self.saved_revision
        self.selection = state.selection.copy() if state.selection else None
        self.face_masks = [mask.copy() for mask in state.face_masks]
        self.lasso_points = list(state.lasso_points)

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        state = self.undo_stack.pop()
        self.redo_stack.append(self._take_state(masks_only=state.image is None))
        self._trim_stack(self.redo_stack)
        self._restore(state)
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        state = self.redo_stack.pop()
        self.undo_stack.append(self._take_state(masks_only=state.image is None))
        self._trim_stack(self.undo_stack)
        self._restore(state)
        return True

    def reset(self) -> None:
        if self.original_animation_frames:
            self.commit_frames(
                [frame.copy() for frame in self.original_animation_frames],
                durations=list(self.original_frame_durations),
                clear_masks=False,
            )
        else:
            self.commit(self.original_image.copy(), clear_masks=False)

    def close(self) -> None:
        images = [self.image, self.original_image, *self.animation_frames, *self.original_animation_frames]
        seen: set[int] = set()
        for image in images:
            if id(image) in seen:
                continue
            seen.add(id(image))
            try:
                image.close()
            except Exception:
                pass
        self._close_stack(self.undo_stack)
        self._close_stack(self.redo_stack)
        self.animation_frames.clear()
        self.original_animation_frames.clear()
