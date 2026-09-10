"""素材加载：从 ComfyUI input 目录读取图片/视频/音频 → tensor / AUDIO dict。

- 图片 → [1, H, W, 3] float32（0~1，PIL）
- 视频 → [T, H, W, 3] float32（PyAV 解码，RGB）
- 音频 → ComfyUI AUDIO dict {waveform: [1, ch, samples], sample_rate}（PyAV 解码 + 重采样）

视频/音频统一走 PyAV（``av``）——它是 ComfyUI 本体的硬依赖（requirements.txt 的
``av>=17.0.0``，且 comfy_api 的视频实现顶层就 ``import av``），因此本插件**不需要任何
额外 pip 依赖**。原先用的 opencv-python-headless / imageio-ffmpeg 已移除：

- 视频：与旧 OpenCV 实现逐帧完全一致（h264 yuv420p/yuv444p、mpeg4、vp9 实测 bit-exact）；
  唯一差异是 10-bit 视频的 10→8 bit 转换策略不同（帧数与尺寸一致，最大像素差 116）。
- 音频：**修正了旧实现的一个解交错 bug**（详见 :func:`load_audio`），输出不再与旧实现相同。
"""

from __future__ import annotations

import os
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
    """解码视频 → [T, H, W, 3] float32（PyAV）。

    ``to_ndarray(format="rgb24")`` 直接输出 RGB，无需再做 BGR→RGB 转换。
    """
    import av

    abs_path = resolve_path(rel_path)
    _ensure_file(abs_path)
    frames: list[torch.Tensor] = []
    try:
        with av.open(abs_path) as container:
            if not container.streams.video:
                raise RuntimeError(f"文件不含视频流: {abs_path}")
            for frame in container.decode(container.streams.video[0]):
                rgb = frame.to_ndarray(format="rgb24")
                frames.append(torch.from_numpy(rgb.astype(np.float32) / 255.0))
                if max_frames and len(frames) >= max_frames:
                    break
    except av.error.FFmpegError as exc:
        raise RuntimeError(f"无法解码视频: {abs_path} ({exc})") from exc
    if not frames:
        raise RuntimeError(f"视频没有可解码帧: {abs_path}")
    return torch.stack(frames)


# PyAV AudioResampler 的 layout 名（当前只用到单/双声道）
_AUDIO_LAYOUTS = {1: "mono", 2: "stereo"}


def load_audio(rel_path: str, sample_rate: int = 32000, channels: int = 2) -> dict[str, Any]:
    """加载音频 → {waveform: [1, ch, samples] float32, sample_rate}。

    统一转为 32kHz 双声道（H3 音频网格 40Hz 的整数倍），后续由 H3 内部处理。

    **修正说明**：旧实现用 ``np.frombuffer(...).reshape(1, channels, -1)`` 处理
    ffmpeg 输出的交错 PCM，这并不会解交错——左"声道"实际是交错流的前半段、
    右"声道"是后半段（把时间轴切成两半当成了左右声道）。PyAV 的 AudioResampler
    直接给出 planar ``fltp`` 帧，通道天然分离，输出即为正确的 [L, R]。
    已用左右声道取值不同的素材验证：新实现 ch0 恒为 L、ch1 恒为 R。
    """
    import av

    layout = _AUDIO_LAYOUTS.get(channels)
    if layout is None:
        raise ValueError(f"不支持的声道数: {channels}（仅支持 1=单声道 / 2=双声道）")

    abs_path = resolve_path(rel_path)
    _ensure_file(abs_path)

    chunks: list[np.ndarray] = []
    try:
        with av.open(abs_path) as container:
            if not container.streams.audio:
                raise RuntimeError(f"文件不含音频流: {abs_path}")
            resampler = av.audio.resampler.AudioResampler(
                format="fltp", layout=layout, rate=sample_rate
            )
            for frame in container.decode(container.streams.audio[0]):
                for resampled in resampler.resample(frame):
                    chunks.append(resampled.to_ndarray())
            # flush：取出重采样器内部残留的尾部样本
            for resampled in resampler.resample(None):
                chunks.append(resampled.to_ndarray())
    except av.error.FFmpegError as exc:
        raise RuntimeError(f"音频解码失败: {abs_path} ({exc})") from exc

    if not chunks:
        raise RuntimeError(f"音频没有可解码样本: {abs_path}")
    pcm = np.concatenate(chunks, axis=1)  # (channels, samples) float32
    wave = torch.from_numpy(pcm).unsqueeze(0).contiguous()
    return {"waveform": wave, "sample_rate": int(sample_rate)}
