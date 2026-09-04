"""段间引导 —— Motion Context 的 latent 直接传递方案（不 VAE 解码）。

上一段采样输出的 AV latent 尾部按 latent step 直接切成 keyframes，
钉入下一段 conditioning（minimax_keyframes，含每步真实帧偏移）；
音频同样从 latent 直接切尾部。采样长度 = 可见帧 + 上下文帧（网格对齐），
解码后裁掉钉入的前缀（trim）。

实现思路参考 NikoDemon80 的 ComfyUI-H3-Motion-Context 项目（致谢见 README，
该项目为 GPL-3.0；本仓库整体亦以 GPL-3.0 发布，见仓库根 LICENSE）。
"""

# Copyright (C) 2026 mangranzhiairen
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with this
# program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import logging

import torch

from .payload import align_frame_count

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.motion_context")

FPS = 24.0
AUDIO_HZ = 40.0
FRAME_RESCALE = 5.0 / 3.0
# H3 VAE 每个 latent step 覆盖的像素帧数（5 帧周期）
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

# 像素窗口 ↔ 整数字符串 step 的可选上下文长度
CONTEXT_FRAME_CHOICES = (5, 22, 39, 56)
DEFAULT_CONTEXT_FRAMES = 22
DEFAULT_AUDIO_CONTEXT_FRAMES = 24
# 可选 pin 窗口（必须能拆成整数 latent steps）
VIDEO_RUN_GRID = (124, 107, 90, 73, 56, 39, 22, 5, 1)


def snap_context_frames(raw: int | float | None) -> int:
    """吸附到支持的上下文窗口（默认 22）。"""
    try:
        n = int(raw or DEFAULT_CONTEXT_FRAMES)
    except (TypeError, ValueError):
        n = DEFAULT_CONTEXT_FRAMES
    return int(min(CONTEXT_FRAME_CHOICES, key=lambda g: (abs(g - n), -g)))


def pixel_frames_for_latent_t(latent_t: int) -> int:
    """latent 时间步 → 覆盖的像素帧数。"""
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(int(latent_t)))


def steps_for_frames(n: int) -> int | None:
    """n 帧恰好是几个 latent step；不是整步返回 None。"""
    k, covered = 0, 0
    while covered < n:
        covered += FRAME_PER_TOKEN[k % 5]
        k += 1
    return k if covered == n else None


def step_offsets(latent_t: int) -> list[int]:
    """每个 latent step 对应的像素帧起点（0 起）。"""
    out, acc = [], 0
    for k in range(int(latent_t)):
        out.append(acc)
        acc += FRAME_PER_TOKEN[k % 5]
    return out


def _streams_from_latent(latent: dict) -> list[torch.Tensor]:
    samples = latent["samples"]
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = list(samples)
    else:
        raise ValueError(f"AV latent 无法拆分流: {type(samples)!r}")
    if not parts:
        raise ValueError("AV latent 没有流")
    return parts


def video_from_latent(latent: dict) -> torch.Tensor:
    """AV latent 的第一流 → 视频 latent [B,C,T,H,W]。"""
    video = _streams_from_latent(latent)[0]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if video.ndim != 5:
        raise ValueError(f"期望视频 latent [B,C,T,H,W]，得到 {tuple(video.shape)}")
    return video


def _video_tail_blocks(latent: dict, n: int) -> tuple[list[torch.Tensor], list[int], int]:
    """从 latent 末尾切出 n 帧对应的 latent 块（每步 [1,C,1,H,W]）+ 偏移 + 覆盖帧数。"""
    video = video_from_latent(latent)
    total = int(video.shape[2])
    steps = steps_for_frames(n)
    if steps is None:
        raise ValueError(
            f"{n} 帧不是整数 latent steps（可用: {', '.join(str(x) for x in CONTEXT_FRAME_CHOICES)}）"
        )
    if steps > total:
        raise ValueError(f"上下文只有 {total} 步，无法切 {steps} 步")
    start = total - steps
    # 相位断言：tail 起始必须在 5 帧周期位置 0（17k+5 帧 → 5g+2 步，窗口 2/7/12/17 步
    # 恒满足；若 VAE 网格变化则拒绝，避免 join 相位错位静默偏移）
    if start % 5 != 0:
        raise RuntimeError(
            f"段间引导: {steps} 步 tail 从 {total} 步 latent 切出的起点在周期位置 "
            f"{start % 5}（应为 0），VAE 网格与预期不符，拒绝错位 join"
        )
    blocks = [video[:1, :, start + k : start + k + 1].clone() for k in range(steps)]
    covered = pixel_frames_for_latent_t(steps)
    if covered != n:
        raise RuntimeError(
            f"段间引导: {steps} 步覆盖 {covered} 帧，预期 {n} 帧（VAE 网格变化？）"
        )
    return blocks, step_offsets(steps), covered


def _audio_tail_from_latent(latent: dict, a_frames: int) -> tuple[torch.Tensor, int]:
    """从 AV latent 的音频流末尾切出音频 latent（stock ref 模式）。"""
    parts = _streams_from_latent(latent)
    if len(parts) < 2:
        raise ValueError("上下文 latent 没有音频流")
    video, audio = parts[0], parts[1]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if audio.ndim == 3:
        audio = audio.unsqueeze(0)
    if audio.ndim != 4:
        raise ValueError(f"期望音频 latent [B,C,2,T]，得到 {tuple(audio.shape)}")
    total_t = int(audio.shape[-1])
    frames = pixel_frames_for_latent_t(int(video.shape[2]))
    rt = int(round(a_frames / FPS * AUDIO_HZ))
    rt = max(1, min(rt, total_t))
    return audio[:1, ..., total_t - rt :].clone(), rt


def apply_motion_context(
    positive,
    latent: dict,
    context_latent: dict,
    context_length: int,
    audio_context_length: int | None = None,
) -> tuple[list, int]:
    """把上一段 latent 尾部钉入当前 conditioning，返回 (positive, trim_frames)。"""
    import node_helpers

    ctx_frames = snap_context_frames(context_length)

    video = video_from_latent(latent)
    width = int(video.shape[4]) * 16
    height = int(video.shape[3]) * 16
    frame_count = pixel_frames_for_latent_t(int(video.shape[2]))

    src = video_from_latent(context_latent)
    src_w, src_h = int(src.shape[4]) * 16, int(src.shape[3]) * 16
    if (src_w, src_h) != (width, height):
        raise ValueError(
            f"段间引导: 上一段 latent 是 {src_w}x{src_h}，当前段是 {width}x{height}；"
            "latent 无法缩放，请保证各段画布一致"
        )

    available = pixel_frames_for_latent_t(int(src.shape[2]))
    n = min(ctx_frames, available)
    n = next(g for g in VIDEO_RUN_GRID if g <= n)
    if n >= frame_count:
        raise ValueError(f"段间引导: 无法把 {n} 帧钉进 {frame_count} 帧的片段")

    blocks, offsets, covered = _video_tail_blocks(context_latent, n)
    ctx_keyframes = [
        {"resolved_frame_index": int(p), "latent": blk}
        for p, blk in zip(offsets, blocks)
    ]

    # 音频：从上一段 latent 直接切尾部，作为 stock reference 追加
    audio_ctx = audio_context_length if audio_context_length is not None else DEFAULT_AUDIO_CONTEXT_FRAMES
    audio_latent, rt = _audio_tail_from_latent(context_latent, int(audio_ctx))
    audio_ref = {"kind": "audio", "ref_audio_t": rt, "audio_latent": audio_latent}

    # 合并 keyframes：保留已有非 0 锚（如 fl2v 尾帧），丢弃 0 处首帧锚
    merged = list(ctx_keyframes)
    out: list = []
    for emb, extra in positive:
        d = extra.copy()
        prior = d.get("minimax_keyframes") or []
        kept = []
        for kf in prior:
            p = int(kf.get("resolved_frame_index", 0))
            if p >= frame_count:
                raise ValueError(
                    f"段间引导: conditioning 携带的锚点位于帧 {p}，但本片段只有 "
                    f"{frame_count} 帧（conditioning 与 latent 应来自同一节点）"
                )
            if p != 0:
                kept.append(dict(kf))
        d["minimax_keyframes"] = kept + merged
        out.append([emb, d])

    out = node_helpers.conditioning_set_values(out, {"minimax_refs": [audio_ref]}, append=True)

    trim = covered
    log.info(
        "段间引导: 钉入 %d 帧（%d latent steps @ %dx%d），音频 %d 步，解码后裁 %d 帧",
        n, len(blocks), width, height, rt, trim,
    )
    return out, trim


def generation_frame_budget(visible_frames: int, context_frames: int) -> tuple[int, int]:
    """采样长度预算：(采样帧数, 解码后要裁的上下文帧数)。

    采样 = align(visible + context)，裁掉 context 帧后正好导出 visible 帧。
    """
    visible = align_frame_count(max(5, int(visible_frames)))
    ctx = snap_context_frames(context_frames) if context_frames else 0
    if ctx <= 0:
        return visible, 0
    sample = align_frame_count(visible + ctx)
    if ctx >= sample:
        raise ValueError(f"段间引导: 上下文 {ctx} 帧必须小于采样长度 {sample} 帧")
    return sample, ctx


def trim_context_prefix(
    images: torch.Tensor,
    audio: dict | None,
    trim_frames: int,
    *,
    fps: float = FPS,
) -> tuple[torch.Tensor, dict | None]:
    """从解码结果裁掉钉入的前缀（画面与音频同步）。"""
    trim = max(0, int(trim_frames))
    if trim > 0:
        if int(images.shape[0]) <= trim:
            raise ValueError(f"段间引导: 无法从 {int(images.shape[0])} 帧解码中裁 {trim} 帧")
        images = images[trim:]
    if not isinstance(audio, dict) or audio.get("waveform") is None:
        return images, audio
    waveform = audio["waveform"]
    sr = int(audio.get("sample_rate") or 32000)
    drop = int(round((trim / float(fps)) * sr)) if trim > 0 else 0
    if drop > 0 and int(waveform.shape[-1]) > drop:
        waveform = waveform[..., drop:]
    want = int(round((int(images.shape[0]) / float(fps)) * sr))
    if int(waveform.shape[-1]) > want:
        waveform = waveform[..., :want]
    return images, {"waveform": waveform, "sample_rate": sr}
