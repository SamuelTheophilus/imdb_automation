"""
Frame quality scoring, selection, and extraction for the video pipeline.

Phase 1: PIL/numpy sharpness scoring and best-frame selection.
Phase 2: Frame extraction from video files.

OpenCV handles MP4/MOV/AVI.  For WebM (browser live recordings) OpenCV
lacks the VP8/VP9 codec on macOS, so those files fall back to imageio-ffmpeg
which ships its own static ffmpeg binary independent of the system install.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# Sample one frame per second by default.  Higher values capture more detail
# at the cost of more API tokens; lower values may miss key product faces.
_DEFAULT_SAMPLE_FPS = 1.0

# Hard cap on extracted frames before sharpness filtering.  Prevents runaway
# costs on long videos (e.g. a 5-minute recording at 1 fps = 300 frames).
_MAX_RAW_FRAMES = 60


def score_sharpness(image_path: Path) -> float:
    """Return a sharpness score for one image using Laplacian variance.

    The Laplacian highlights edges; its variance is high for sharp images
    and low for blurry ones.  Grayscale conversion prevents colour from
    inflating the score.
    """
    img = Image.open(image_path).convert("L")
    arr = np.array(img, dtype=np.float32)

    # 3x3 discrete Laplacian kernel
    kernel = np.array([[0, 1, 0],
                       [1, -4, 1],
                       [0, 1, 0]], dtype=np.float32)

    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(arr, (3, 3))
    laplacian = (windows * kernel).sum(axis=(-2, -1))
    return float(laplacian.var())


def select_best_frames(paths: list[Path], max_frames: int = 12) -> list[Path]:
    """Return up to *max_frames* paths ranked by sharpness, best first.

    If fewer than max_frames paths are provided all are returned, still
    sorted so the primary image (index 0) is the sharpest one.
    """
    if not paths:
        return []

    scored = []
    for p in paths:
        try:
            score = score_sharpness(p)
        except Exception:
            score = 0.0  # unreadable image sorts to the back
        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:max_frames]]


async def select_best_frames_async(paths: list[Path], max_frames: int = 12) -> list[Path]:
    """Async wrapper -- runs sharpness scoring in a thread to avoid blocking."""
    return await asyncio.to_thread(select_best_frames, paths, max_frames)


def _extract_frames_opencv(
    video_path: Path,
    output_dir: Path,
    sample_fps: float,
    max_frames: int,
) -> list[Path]:
    """Extract frames using OpenCV.  Works for MP4, MOV, AVI, etc."""
    cap = cv2.VideoCapture(str(video_path))
    try:
        video_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        # Skip this many native frames between each saved sample.
        frame_interval = max(1, int(round(video_fps / sample_fps)))
        output_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        frame_idx = 0
        while len(saved) < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % frame_interval == 0:
                out_path = output_dir / f"frame_{frame_idx:06d}.jpg"
                cv2.imwrite(str(out_path), frame)
                saved.append(out_path)
            frame_idx += 1
    finally:
        cap.release()
    return saved


def _extract_frames_imageio(
    video_path: Path,
    output_dir: Path,
    sample_fps: float,
    max_frames: int,
) -> list[Path]:
    """Extract frames using imageio-ffmpeg's bundled ffmpeg binary.

    Used as a fallback for formats OpenCV cannot decode on macOS, primarily
    WebM files produced by browser MediaRecorder.  imageio-ffmpeg ships a
    static ffmpeg binary so it is independent of the system ffmpeg install.
    """
    import imageio  # deferred import -- only needed for WebM fallback

    output_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    reader = imageio.get_reader(str(video_path))
    try:
        meta = reader.get_meta_data()
        video_fps = float(meta.get("fps") or 25.0)
        frame_interval = max(1, int(round(video_fps / sample_fps)))
        frame_idx = 0
        for frame in reader:
            if len(saved) >= max_frames:
                break
            if frame_idx % frame_interval == 0:
                out_path = output_dir / f"frame_{frame_idx:06d}.jpg"
                # imageio returns RGB numpy arrays; PIL saves them correctly.
                Image.fromarray(frame).save(str(out_path))
                saved.append(out_path)
            frame_idx += 1
    finally:
        reader.close()

    return saved


def extract_frames_from_video(
    video_path: Path,
    output_dir: Path,
    sample_fps: float = _DEFAULT_SAMPLE_FPS,
    max_frames: int = _MAX_RAW_FRAMES,
) -> list[Path]:
    """Extract evenly-spaced frames from a video file.

    Tries OpenCV first (fast, handles MP4/MOV/AVI).  If OpenCV cannot open
    the file (e.g. WebM on macOS), falls back to imageio-ffmpeg which uses
    a bundled static ffmpeg binary and supports VP8/VP9 WebM recordings.

    Args:
        video_path:  Path to the input video (.mp4, .mov, .avi, .webm, ...).
        output_dir:  Directory where extracted frame JPEGs are written.
        sample_fps:  Frames to sample per second of video.
        max_frames:  Hard cap on total frames extracted.

    Returns:
        List of paths to the extracted frame images, in time order.
    """
    # Probe with OpenCV to decide which extractor to use.
    cap = cv2.VideoCapture(str(video_path))
    can_open = cap.isOpened()
    cap.release()

    if can_open:
        return _extract_frames_opencv(video_path, output_dir, sample_fps, max_frames)

    # OpenCV failed -- fall back to imageio-ffmpeg (handles WebM, etc.)
    return _extract_frames_imageio(video_path, output_dir, sample_fps, max_frames)


async def extract_frames_from_video_async(
    video_path: Path,
    output_dir: Path,
    sample_fps: float = _DEFAULT_SAMPLE_FPS,
    max_frames: int = _MAX_RAW_FRAMES,
) -> list[Path]:
    """Async wrapper -- runs frame extraction in a thread to avoid blocking."""
    return await asyncio.to_thread(
        extract_frames_from_video, video_path, output_dir, sample_fps, max_frames
    )
