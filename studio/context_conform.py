"""通用 latent 上下文空间整形（context conform）。

问题
----
自定义采样（例如二采）里，段 A 的最终 latent 是放大后的目标网格（例 1.0），
而消费方（段 B 的低清阶段，或任何声明了目标网格的调用点）需要 0.4 网格；两边
空间尺寸不一致时，续接直接 splice 会导致 H3 patchify 行错位 / 蜂窝花屏。

方案（Phase 1：直接缩放兜底，永远可用）
------------------------------------
把任意来源分辨率的 context latent 的视频流按目标网格做空间插值：
- 缩小用 area（抗混叠），放大用 bilinear（align_corners=False）；可显式指定。
- 按帧、按通道做均值补偿，修插值带来的色偏。
- **时间轴永不插值**：context 尾必须是上一段的真实帧，时间只由调用方切片。
- **音频流不动**：H3 的音频 latent [B,C,2,T] 没有空间维。

消费方只需给出目标 latent 或目标视频网格；已匹配时零拷贝返回，幂等，且
绝不就地改写入参。

H3 流约定
--------
samples 里第 0 流是视频（5D [B,C,T,H,W]，或图像 4D [B,C,H,W]），其余流是音频
（[B,C,2,T]）。本模块只对第 0 流做空间缩放，与官方 MiniMaxH3 AV latent 排布一致。

Phase 2（原生栅格阶梯 / native low-res carry）不在本模块范围内；如需要，另行
引入 register_context_grid 与持久化递归。
"""

# Copyright (C) 2026 mangranzhiairen
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.

from __future__ import annotations

import logging
from typing import Any

import torch
import torch.nn.functional as F

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.context_conform")

_VALID_MODES = ("area", "bilinear", "bicubic", "nearest", "nearest-exact")


# ---------- 流拆分 / 重建 ----------

def split_streams(samples: Any) -> list:
    """samples（NestedTensor / tuple / list / 单张量）→ 流列表。"""
    if torch.is_tensor(samples):
        return [samples]
    if hasattr(samples, "unbind"):
        return list(samples.unbind())
    if hasattr(samples, "tensors"):
        return list(samples.tensors)
    if isinstance(samples, (tuple, list)):
        return list(samples)
    raise ValueError(f"context_conform: 无法拆解 samples（{type(samples)!r}）")


def rebuild_streams(parts: list, template: Any) -> Any:
    """按模板类型把流列表装回去（NestedTensor 优先，其次模板同类型）。

    真实运行环境走 comfy.nested_tensor.NestedTensor；无 comfy 的单测环境退回
    模板同类型（FakeNested）或 tuple，保证纯张量测试可跑。
    """
    is_container = template is not None and not isinstance(template, (tuple, list)) and not torch.is_tensor(template)
    if is_container and (hasattr(template, "unbind") or hasattr(template, "tensors")):
        try:
            import comfy.nested_tensor

            return comfy.nested_tensor.NestedTensor(tuple(parts))
        except Exception:  # noqa: BLE001 测试环境没有 comfy：退回模板同类型
            try:
                return type(template)(tuple(parts))
            except Exception:  # noqa: BLE001
                pass
    if isinstance(template, list):
        return parts
    if isinstance(template, tuple):
        return tuple(parts)
    if len(parts) == 1:
        return parts[0]
    try:
        import comfy.nested_tensor

        return comfy.nested_tensor.NestedTensor(tuple(parts))
    except Exception:  # noqa: BLE001
        return tuple(parts)


# ---------- 几何查询 ----------

def video_stream(latent: dict) -> torch.Tensor:
    """latent 的视频流（5D，图像 latent 补一个 T=1 轴）。"""
    if not isinstance(latent, dict) or latent.get("samples") is None:
        raise ValueError("context_conform: latent 缺少 samples")
    video = split_streams(latent["samples"])[0]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if video.ndim != 5:
        raise ValueError(
            f"context_conform: 期望视频 latent [B,C,T,H,W]，得到 {tuple(video.shape)}"
        )
    return video


def video_hw(latent: dict | None) -> tuple | None:
    """视频 latent 的空间尺寸 (H, W)；读不出来返回 None。"""
    if latent is None:
        return None
    try:
        video = video_stream(latent)
    except Exception:  # noqa: BLE001
        return None
    return int(video.shape[-2]), int(video.shape[-1])


# H3 视频 VAE 的空间压缩比：`space_down = (2,2,2,2,1,1)` → prod = 16
# （comfy/ldm/minimax/vae.py: MiniMaxH3VideoVAE.vae_ratio；解码输出 h*w 的 vae_ratio 倍）
VAE_SPATIAL_RATIO = 16


def latent_pixel_size(latent: dict | None) -> tuple[int, int] | None:
    """AV latent 视频流对应的像素尺寸 (width, height)；读不出来返回 None。

    自定义采样流程（子图）可能中途二采放大，最终 latent 网格与采样画布不再是 1:1；
    落库的 canvas 只描述画布，实际出片尺寸必须读 latent 本身（采样结束、释放前）。
    """
    hw = video_hw(latent)
    if hw is None:
        return None
    return int(hw[1]) * VAE_SPATIAL_RATIO, int(hw[0]) * VAE_SPATIAL_RATIO


# ---------- 核心：视频流空间缩放 ----------

def resize_video_latent(
    video: torch.Tensor,
    dst_h: int,
    dst_w: int,
    *,
    mode: str | None = None,
    preserve_mean: bool = True,
) -> torch.Tensor:
    """视频 latent 空间缩放；时间轴不动，逐帧插值。

    - mode=None 自动：缩小用 area，放大用 bilinear。
    - preserve_mean：按帧、按通道把缩放结果均值拉回源均值，修插值色偏。
    """
    work = video
    squeezed = False
    if work.ndim == 4:
        work = work.unsqueeze(0)
        squeezed = True
    if work.ndim != 5:
        raise ValueError(f"context_conform: 期望 5D 视频 latent，得到 {tuple(video.shape)}")
    b, c, t, h, w = (int(x) for x in work.shape)
    dst_h, dst_w = max(1, int(dst_h)), max(1, int(dst_w))
    if (h, w) == (dst_h, dst_w):
        return video
    if mode is None:
        shrink = (dst_h * dst_w) < (h * w)
        mode = "area" if shrink else "bilinear"
    if mode not in _VALID_MODES:
        raise ValueError(f"context_conform: 不支持的插值模式 {mode!r}（可选 {list(_VALID_MODES)}）")
    kwargs: dict = {"mode": mode}
    if mode in ("bilinear", "bicubic"):
        kwargs["align_corners"] = False
    # B C T H W → (B*T) C H W；时间维只进 batch，绝不参与插值
    flat = work.permute(0, 2, 1, 3, 4).reshape(b * t, c, h, w).float()
    out = F.interpolate(flat, size=(dst_h, dst_w), **kwargs)
    out = out.reshape(b, t, c, dst_h, dst_w).permute(0, 2, 1, 3, 4)
    if preserve_mean:
        src_mean = work.float().mean(dim=(-2, -1), keepdim=True)
        out_mean = out.mean(dim=(-2, -1), keepdim=True)
        out = out + (src_mean - out_mean)
    out = out.to(dtype=work.dtype)
    if squeezed:
        out = out.squeeze(0)
    return out.contiguous()


# ---------- latent 级整形 ----------

def conform_context_latent(
    latent: dict,
    *,
    target_latent: dict | None = None,
    target_video_hw: tuple | None = None,
    mode: str | None = None,
    preserve_mean: bool = True,
    video_stream_index: int = 0,
) -> dict:
    """把 context latent 的视频流整形到目标网格，返回新 dict（幂等）。

    只动视频流与同构的 noise_mask 视频流；音频流、时间轴、其余元数据原样保留。
    已匹配时原样返回入参对象（零拷贝快路径）。目标读不出时也原样返回，由上游
    既有校验负责报错。
    """
    if target_video_hw is None:
        target_video_hw = video_hw(target_latent)
    if target_video_hw is None:
        return latent
    dst_h, dst_w = int(target_video_hw[0]), int(target_video_hw[1])
    streams = split_streams(latent["samples"])
    if not streams or video_stream_index >= len(streams):
        return latent
    src = streams[video_stream_index]
    if src.ndim not in (4, 5):
        return latent
    h, w = int(src.shape[-2]), int(src.shape[-1])
    if (h, w) == (dst_h, dst_w):
        return latent

    out = dict(latent)
    new_streams = list(streams)
    new_streams[video_stream_index] = resize_video_latent(
        src, dst_h, dst_w, mode=mode, preserve_mean=preserve_mean
    )
    out["samples"] = rebuild_streams(new_streams, latent.get("samples"))

    mask = latent.get("noise_mask")
    if mask is not None:
        try:
            mask_streams = split_streams(mask)
            if mask_streams and video_stream_index < len(mask_streams):
                mv = mask_streams[video_stream_index]
                if mv.ndim in (4, 5) and tuple(mv.shape[-2:]) == (h, w):
                    ms = list(mask_streams)
                    ms[video_stream_index] = resize_video_latent(
                        mv, dst_h, dst_w, mode=mode, preserve_mean=False
                    )
                    out["noise_mask"] = rebuild_streams(ms, mask)
        except Exception as exc:  # noqa: BLE001 mask 整形失败不阻断（续接会重建 mask）
            log.debug("context_conform: noise_mask 整形跳过（%s）", exc)

    log.info(
        "context_conform: 视频 context 空间整形 %dx%d → %dx%d（mode=%s，音频/时间轴不动）",
        h, w, dst_h, dst_w, mode or "auto",
    )
    return out
