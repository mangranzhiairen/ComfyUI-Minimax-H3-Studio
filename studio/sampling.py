"""真实采样链路 —— 完全对齐 ComfyUI 官方 MiniMax H3 工作流（video_minimax_h3_r2v.json）：

MiniMaxH3ImageToVideo / ReferenceToVideo（conditioning + AV latent）
  → MiniMaxH3SigmaShift（video/audio shift）
  → BasicScheduler → BasicGuider / CFGGuider → KSamplerSelect → RandomNoise
  → SamplerCustomAdvanced（官方自定义采样节点组合，非 comfy.sample.sample）
  → VAEDecode（视频）+ VAEDecodeAudio（音频）直接解码同一 AV latent（不分离）

实现要点：ComfyUI V3 节点（io.ComfyNode）的 execute 返回 NodeOutput，
统一经 unpack_node_output 取 args（兼容 tuple/list 旧式输出）。
"""

from __future__ import annotations

import logging
from typing import Any

import torch

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.sampling")


def unpack_node_output(out: Any):
    """兼容 V3 节点（io.NodeOutput）与普通 tuple/list 的输出。"""
    if hasattr(out, "args"):
        args = out.args
        if args:
            return args
    if isinstance(out, (tuple, list)):
        return out
    raise RuntimeError(f"无法解析节点输出: {type(out)!r}")


def _load_minimax_nodes():
    from comfy_extras.nodes_minimax_h3 import (
        MiniMaxH3ImageToVideo,
        MiniMaxH3ReferenceToVideo,
        MiniMaxH3SigmaShift,
    )

    return MiniMaxH3ImageToVideo, MiniMaxH3ReferenceToVideo, MiniMaxH3SigmaShift


def _align32(value: int) -> int:
    """H3 patchify 要求宽高为 32 的倍数（向下对齐）。"""
    return max(32, (int(value) // 32) * 32)


def run_minimax_conditioning(ctx, cr):
    """按 ConditioningResult 调用官方 conditioning 节点，返回 (positive, latent)。

    cr 来自 Task 子类 build_conditioning()（所有模式共用同一描述结构）：
    素材字段是 input 目录相对路径，这里加载成 tensor / AUDIO dict 传入官方节点。
    """
    from .media_loader import load_audio, load_image, load_video

    ImageToVideo, ReferenceToVideo, _ = _load_minimax_nodes()

    width = _align32(cr.width)
    height = _align32(cr.height)

    if cr.node == "MiniMaxH3ReferenceToVideo":
        ref_images = {k: load_image(p) for k, p in cr.ref_images.items()} or None
        ref_videos = {k: load_video(p) for k, p in cr.ref_videos.items()} or None
        ref_audios = {k: load_audio(p) for k, p in cr.ref_audios.items()} or None
        out = ReferenceToVideo.execute(
            ctx.clip,
            ctx.video_vae,
            ctx.audio_vae,
            cr.prompt,
            width,
            height,
            cr.length,
            cr.ref_image_size,
            ref_images=ref_images,
            ref_videos=ref_videos,
            ref_audios=ref_audios,
        )
    else:
        first_frame = load_image(cr.first_frame) if cr.first_frame else None
        last_frame = load_image(cr.last_frame) if cr.last_frame else None
        out = ImageToVideo.execute(
            ctx.clip,
            ctx.video_vae,
            cr.prompt,
            width,
            height,
            cr.length,
            first_frame=first_frame,
            last_frame=last_frame,
        )

    positive, latent = unpack_node_output(out)
    return positive, latent


def _use_basic_guider(cfg: float, negative) -> bool:
    """官方 r2v 模板使用 BasicGuider（无 CFG）：无 negative 且 cfg≈1.0 时启用。"""
    if negative:
        return False
    return abs(float(cfg) - 1.0) < 1e-6


def sample_single_stage(
    *,
    model,
    positive,
    negative,
    latent: dict,
    seed: int,
    cfg: float,
    steps: int,
    sampler_name: str,
    scheduler: str,
    shift_video: float = 12.0,
    shift_audio: float = 3.0,
    denoise: float = 1.0,
    progress=None,
) -> dict:
    """官方 MiniMax H3 单阶段采样：与官方 r2v 工作流同款自定义采样节点组合。

    MiniMaxH3SigmaShift → BasicScheduler → BasicGuider / CFGGuider
      → KSamplerSelect → RandomNoise → SamplerCustomAdvanced

    progress：每步回调 callback(step, steps_total, latent)（可选），
    用于前端卡片进度条（段级采样进度）；与 SamplerCustomAdvanced 内部
    的 latent_preview 回调链共存（包装 guider.sample 注入）。
    """
    from comfy_extras.nodes_custom_sampler import (
        BasicGuider,
        BasicScheduler,
        CFGGuider,
        KSamplerSelect,
        RandomNoise,
        SamplerCustomAdvanced,
    )

    _, _, SigmaShift = _load_minimax_nodes()

    shifted = SigmaShift.execute(model, float(shift_video), float(shift_audio))
    model_use = unpack_node_output(shifted)[0]

    denoise_use = float(max(0.0, min(1.0, denoise)))
    sigma_out = BasicScheduler.execute(model_use, str(scheduler), int(steps), denoise_use)
    sigma_t = unpack_node_output(sigma_out)[0]

    sampler_obj = unpack_node_output(KSamplerSelect.execute(str(sampler_name)))[0]
    noise_obj = unpack_node_output(RandomNoise.execute(int(seed)))[0]

    neg = negative if negative else []
    if _use_basic_guider(cfg, neg):
        guider = unpack_node_output(BasicGuider.execute(model_use, positive))[0]
    else:
        guider = unpack_node_output(CFGGuider.execute(model_use, positive, neg, float(cfg)))[0]

    def _run_official() -> dict:
        sampled = SamplerCustomAdvanced.execute(noise_obj, guider, sampler_obj, sigma_t, latent)
        return unpack_node_output(sampled)[0]

    if progress is None:
        out = _run_official()
    else:
        orig_sample = guider.sample

        def sample_wrapped(noise, latent_image, sampler, sigmas_in, **kwargs):
            inner_cb = kwargs.get("callback")

            def callback(step, x0, x, total_steps):
                try:
                    if total_steps > 0:
                        progress(step, x0, x, total_steps)
                except Exception as exc:  # noqa: BLE001 进度回调异常不影响采样
                    log.debug("Step progress callback skipped: %s", exc)
                if inner_cb is not None:
                    inner_cb(step, x0, x, total_steps)

            kwargs["callback"] = callback
            return orig_sample(noise, latent_image, sampler, sigmas_in, **kwargs)

        guider.sample = sample_wrapped
        try:
            out = _run_official()
        finally:
            guider.sample = orig_sample

    return out


def empty_audio_dict() -> dict[str, Any]:
    """静音/无音频输出占位（ComfyUI AUDIO 结构）。"""
    return {
        "waveform": torch.zeros(1, 1, 1, dtype=torch.float32),
        "sample_rate": 32000,
    }


def decode_av_latent(
    samples: dict,
    vae,
    audio_vae,
    *,
    decode_audio: bool = True,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """AV latent 直接解码（对齐官方 r2v 工作流）：VAEDecode + VAEDecodeAudio 同吃 AV latent。

    VAEDecode 解出视频流，VAEDecodeAudio 解出音频流，均不需要
    LTXVSeparateAVLatent 预分离；官方 VAE 把 pixels 写到 intermediate_device()。
    解码成片立即搬到 CPU：GPU 显存只留给采样/解码过程，多段时成片
    在 CPU 内存累积（输出必需），避免显存被所有段成片占用。
    """
    from nodes import VAEDecode

    images, = VAEDecode().decode(vae, samples)
    if getattr(images, "device", None) is not None and images.device.type != "cpu":
        images = images.cpu()
    if images.dtype != torch.float32:
        images = images.float()

    if not decode_audio or audio_vae is None:
        return images, empty_audio_dict()

    try:
        from comfy_extras.nodes_audio import VAEDecodeAudio
    except ImportError:
        from comfy_extras.nodes_lt import VAEDecodeAudio  # type: ignore

    try:
        audio_out = VAEDecodeAudio.execute(audio_vae, samples)
        audio = unpack_node_output(audio_out)[0]
        if not isinstance(audio, dict) or audio.get("waveform") is None:
            audio = empty_audio_dict()
        else:
            audio = {
                "waveform": audio["waveform"].cpu().float(),
                "sample_rate": audio.get("sample_rate", 32000),
            }
    except Exception as exc:  # noqa: BLE001 音频解码失败不阻断成片
        log.warning("音频解码失败，输出静音: %s", exc)
        audio = empty_audio_dict()

    return images, audio
