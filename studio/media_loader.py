"""素材加载：从 ComfyUI input 目录读取图片/视频/音频 → tensor / AUDIO dict。

- 图片 → [1, H, W, 3] float32（0~1）
- 视频 → [T, H, W, 3] float32（OpenCV 解码，RGB）
- 音频 → ComfyUI AUDIO dict {waveform: [1, ch, samples], sample_rate}
"""

from __future__ import annotations

import os
import subprocess
from collections import OrderedDict
from typing import Any

import numpy as np
import torch

import folder_paths

# 图片加载 LRU 缓存（同 Comfy LoadImage 思路）：文件未变时跳过重复解码。
# 首尾帧/参考图在逐段执行时可能被反复加载（同文件同内容命中同一 tensor）。
_IMAGE_CACHE: OrderedDict[tuple, torch.Tensor] = OrderedDict()
_IMAGE_CACHE_MAX = 16


def _image_cache_key(abs_path: str) -> tuple | None:
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
    return (abs_path, int(st.st_size), mtime_ns)


def _image_cache_get(key: tuple) -> torch.Tensor | None:
    hit = _IMAGE_CACHE.get(key)
    if hit is None:
        return None
    _IMAGE_CACHE.move_to_end(key)
    return hit


def _image_cache_put(key: tuple, tensor: torch.Tensor) -> None:
    _IMAGE_CACHE[key] = tensor
    _IMAGE_CACHE.move_to_end(key)
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)


def resolve_path(rel_path: str) -> str:
    """相对 ComfyUI input 目录的路径 → 绝对路径。"""
    rel = str(rel_path).replace("\\", "/")
    return os.path.join(folder_paths.get_input_directory(), rel.replace("/", os.sep))


def _ensure_file(abs_path: str) -> None:
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"素材文件不存在: {abs_path}")


def load_image(rel_path: str) -> torch.Tensor:
    """加载图片 → [1, H, W, 3] float32（同文件命中 LRU 缓存，跳过重复解码）。

    缓存键含文件大小与 mtime（文件变更自动失效）；加载后放到
    intermediate_device()/intermediate_dtype()（默认 CPU，不占显存）。
    """
    from PIL import Image

    abs_path = resolve_path(rel_path)
    _ensure_file(abs_path)
    key = _image_cache_key(abs_path)
    cached = _image_cache_get(key) if key else None
    if cached is not None:
        return cached
    img = Image.open(abs_path).convert("RGB")
    arr = np.array(img, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(arr).unsqueeze(0)
    try:
        import comfy.model_management as mm

        tensor = tensor.to(device=mm.intermediate_device(), dtype=mm.intermediate_dtype())
    except Exception:  # noqa: BLE001 无 ComfyUI 环境（单测）时保持 CPU float32
        pass
    if key:
        _image_cache_put(key, tensor)
    return tensor


def load_video(rel_path: str, max_frames: int | None = None) -> torch.Tensor:
    """解码视频 → [T, H, W, 3] float32（OpenCV）。"""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("加载视频需要 opencv-python（pip install opencv-python-headless）") from exc

    abs_path = resolve_path(rel_path)
    _ensure_file(abs_path)
    cap = cv2.VideoCapture(abs_path)
    if not cap.isOpened():
        raise RuntimeError(f"无法解码视频: {abs_path}")
    frames: list[torch.Tensor] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(torch.from_numpy(frame.astype(np.float32) / 255.0))
            if max_frames and len(frames) >= max_frames:
                break
    finally:
        cap.release()
    if not frames:
        raise RuntimeError(f"视频没有可解码帧: {abs_path}")
    return torch.stack(frames)


def _ffmpeg_bin() -> str:
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except ImportError:
        import shutil

        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError("音频解码需要 FFmpeg（pip install imageio-ffmpeg 或系统安装）")


def load_audio(rel_path: str, sample_rate: int = 32000, channels: int = 2) -> dict[str, Any]:
    """加载音频 → {waveform: [1, ch, samples] float32, sample_rate}。

    统一转为 32kHz 双声道（H3 音频网格 40Hz 的整数倍），后续由 H3 内部处理。
    """
    abs_path = resolve_path(rel_path)
    _ensure_file(abs_path)
    ffmpeg = _ffmpeg_bin()
    args = [
        ffmpeg, "-v", "error", "-i", abs_path,
        "-vn", "-ac", str(channels), "-ar", str(sample_rate),
        "-f", "f32le", "-",
    ]
    try:
        res = subprocess.run(args, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"音频解码失败: {abs_path} ({exc.stderr.decode(errors='ignore')[:200]})") from exc
    pcm = np.frombuffer(res.stdout, dtype=np.float32)
    total = pcm.size
    usable = total - (total % channels)
    wave = torch.from_numpy(pcm[:usable]).reshape(1, channels, -1).contiguous()
    return {"waveform": wave, "sample_rate": int(sample_rate)}
