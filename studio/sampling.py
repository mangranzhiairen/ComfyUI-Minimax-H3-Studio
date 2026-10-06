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


def _call_official(node_class, **kwargs):
    """按**关键字**调用官方 conditioning 节点的 execute。

    官方 MiniMaxH3 节点的 execute 签名跨 ComfyUI 版本变过（同一份插件要同时跑新旧版）：

    - 旧版 ReferenceToVideo: ``(clip, vae, audio_vae, prompt, width, height, length, ref_image_size=…)``
    - 新版 ReferenceToVideo: ``(clip, prompt, width, height, length, ref_image_size="match", vae=None, audio_vae=None, …)``
      （vae/audio_vae 改成 optional 输入后，位置上移到了 ref_image_size 之后）

    位置传参在版本不匹配时会**静默错位**：vae 落到 prompt、width 收到 VAE 对象、
    height 收到 prompt 字符串，最终在 core 里炸出难以定位的
    ``unsupported operand type(s) for //: 'str' and 'int'``。
    所以这里一律按名字传参，并在名字对不上时给出可读报错（而不是又猜一次位置）。
    """
    import inspect

    try:
        params = inspect.signature(node_class.execute).parameters
    except (TypeError, ValueError):  # 拿不到签名（非 Python 可调用）→ 直接按关键字调用
        params = None
    if params is not None and not any(
        p.kind is p.VAR_KEYWORD for p in params.values()
    ):
        accepted = {
            name
            for name, p in params.items()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }
        unsupported = sorted(set(kwargs) - accepted)
        if unsupported:
            raise RuntimeError(
                f"{node_class.__name__}.execute 不接受参数 {', '.join(unsupported)}"
                f"（当前 ComfyUI 的签名: {', '.join(params)}）——"
                "官方 MiniMaxH3 节点签名已变更，请升级/适配本插件"
            )
    return node_class.execute(**kwargs)


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
        out = _call_official(
            ReferenceToVideo,
            clip=ctx.clip,
            vae=ctx.video_vae,
            audio_vae=ctx.audio_vae,
            prompt=cr.prompt,
            width=width,
            height=height,
            length=cr.length,
            ref_image_size=cr.ref_image_size,
            ref_images=ref_images,
            ref_videos=ref_videos,
            ref_audios=ref_audios,
        )
    else:
        first_frame = load_image(cr.first_frame) if cr.first_frame else None
        last_frame = load_image(cr.last_frame) if cr.last_frame else None
        out = _call_official(
            ImageToVideo,
            clip=ctx.clip,
            vae=ctx.video_vae,
            prompt=cr.prompt,
            width=width,
            height=height,
            length=cr.length,
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


def build_pipeline_runtime(
    *,
    model,
    positive,
    latent: dict,
    seed: int,
    steps: int,
    sampler_name: str,
    scheduler: str,
    shift_video: float = 12.0,
    shift_audio: float = 3.0,
    on_step=None,
):
    """构造采样流程（子图）骨架槽的运行时值 —— 惰性求值，只有被用到的槽才算。

    语义对齐内置官方链（`sample_single_stage`）：

    | 槽 | 值 |
    |---|---|
    | conditioning | 条件（positive） |
    | latent | AV 空 latent |
    | model | **已做 SigmaShift 的模型**（shift_video/audio 取节点 widget） |
    | scheduler | 用上面这个模型算出的 **SIGMAS**（scheduler/steps，denoise=1.0） |
    | sampler | KSamplerSelect(sampler_name) |
    | noise | RandomNoise(seed) |

    这样"官方等价模型"（子图里 BasicGuider + KSamplerSelect + RandomNoise +
    SamplerCustomAdvanced，接 model/conditioning/sampler/noise/scheduler/latent）
    跑出来应与不挂采样流程时一致，可作为回归基准。用户也可以不用 model/scheduler，
    在子图里自己拉偏移/调度链（那时这两槽就不求值，零额外开销）。

    `on_step`：逐步回调 `(step, x0, x, total_steps)`。给了就把它挂到 model 上
    （OUTER_SAMPLE 包装器，见 `attach_sampler_progress`），这样**用户链里有几段采样都能**拿到
    进度与 x0（进度条 + live 预览），不依赖对方用哪个 guider/sampler 节点。
    """
    from .pipeline import PipelineRuntime

    from comfy_extras.nodes_custom_sampler import (
        BasicScheduler,
        KSamplerSelect,
        RandomNoise,
    )

    memo: dict[str, Any] = {}

    def _once(key: str, factory):
        if key not in memo:
            memo[key] = factory()
        return memo[key]

    def shifted_model():
        _, _, SigmaShift = _load_minimax_nodes()
        out = SigmaShift.execute(model, float(shift_video), float(shift_audio))
        return unpack_node_output(out)[0]

    def sigmas():
        out = BasicScheduler.execute(
            _once("model", shifted_model), str(scheduler), int(steps), 1.0
        )
        return unpack_node_output(out)[0]

    def model_slot():
        """model 槽：偏移后的模型 + 逐步回调包装器（挂在模型上，链上每段采样都触发）。"""
        m = _once("model", shifted_model)
        return attach_sampler_progress(m, on_step) if on_step is not None else m

    return PipelineRuntime(
        providers={
            "conditioning": lambda: positive,
            "latent": lambda: latent,
            "model": model_slot,
            "scheduler": lambda: _once("sigmas", sigmas),
            "sampler": lambda: _once(
                "sampler", lambda: unpack_node_output(KSamplerSelect.execute(str(sampler_name)))[0]
            ),
            "noise": lambda: _once(
                "noise", lambda: unpack_node_output(RandomNoise.execute(int(seed)))[0]
            ),
        }
    )


def _nested_latent_view(x0, latent_shapes):
    """把回调里的 x0 还原成「嵌套（AV）视图」。

    ⚠️ 必须做这一步：我们的包装器挂在 `OUTER_SAMPLE` 上，而
    `comfy/samplers.py: CFGGuider.sample` 是这样组织的 ——

        if latent_image.is_nested:
            latent_image, latent_shapes = pack_latents(latent_image.unbind())   # AV → 扁平
        if len(latent_shapes) > 1 and callback is not None:
            packed_callback = callback
            def callback(step, x0, x, total_steps):          # 在这一层还原成嵌套视图
                x0 = NestedTensor(unpack_latents(x0, latent_shapes))
                return packed_callback(step, x0, x, total_steps)
        executor = WrapperExecutor.new_class_executor(self.outer_sample, ...)   # ← 我们在这层内

    也就是说：采样器交给我们的 x0 是**打包后的扁平张量**，而下面的 nested→callback 包装器
    在更外层。studio 的 TAE live 预览要求 5D 视频流（`video_stream()`），不还原就会
    静默不出预览（进度条不受影响，因为它不看 x0）。
    KJNodes 的 `_normalize_packed_x0()` 做的正是同一件事。
    """
    if x0 is None or latent_shapes is None or len(latent_shapes) <= 1:
        return x0
    if hasattr(x0, "tensors") or not torch.is_tensor(x0):  # 已经是嵌套/非张量：不动
        return x0
    try:
        import comfy.nested_tensor
        import comfy.utils

        return comfy.nested_tensor.NestedTensor(comfy.utils.unpack_latents(x0, latent_shapes))
    except Exception:  # noqa: BLE001 还原失败就按原样交出去（预览会自行降级）
        return x0


class SamplerProgressWrapper:
    """挂在 model 上的 `OUTER_SAMPLE` 包装器：逐步回调 → studio 进度/live 预览。

    **为什么挂在 model 上而不是采样节点上**（同 KJNodes `ModelPreviewOverride` 的做法）：
    ComfyUI 的 guider（BasicGuider / CFGGuider…）在 `sample()` 里会把
    `model_options[WrappersMP.OUTER_SAMPLE]` 的所有包装器串成 WrapperExecutor，
    再执行真正的采样（comfy/samplers.py: `outer_sample` / `WrapperExecutor`）。
    因此只要模型里带了包装器，**用户链里有几段采样、用哪个 guider/sampler 节点都能拿到**：

    - `sigmas` → 本段总步数
    - `callback(step, x0, x, total_steps)` → 每一步的 x0（进度条 + live 预览都靠它）

    我们只包一层回调：先把 x0 还原成嵌套视图转给 studio（进度/预览），再原样调用官方回调，
    不影响官方预览链。
    """

    def __init__(self, on_step):
        self.on_step = on_step

    def __call__(
        self,
        executor,
        noise,
        latent_image,
        sampler,
        sigmas,
        denoise_mask=None,
        callback=None,
        disable_pbar=False,
        seed=None,
        latent_shapes=None,
    ):
        on_step = self.on_step

        def wrapped_callback(step, x0, x, total_steps):
            if on_step is not None:
                try:
                    # x0 交给 studio 前还原成 AV 嵌套视图（TAE 预览要 5D 视频流）
                    on_step(step, _nested_latent_view(x0, latent_shapes), x, total_steps)
                except Exception:  # noqa: BLE001 进度/预览失败绝不能影响采样
                    log.debug("采样进度回调异常", exc_info=True)
            if callback is not None:
                callback(step, x0, x, total_steps)

        return executor(
            noise,
            latent_image,
            sampler,
            sigmas,
            denoise_mask,
            wrapped_callback,
            disable_pbar,
            seed,
            latent_shapes=latent_shapes,
        )


def attach_sampler_progress(model, on_step):
    """给模型挂上逐步回调（cloned ModelPatcher，共享权重，代价很小）。

    挂不上（老版本没有 patcher_extension）时退回原模型：进度/预览缺失但采样照跑。
    """
    if on_step is None:
        return model
    try:
        from comfy.patcher_extension import WrappersMP

        patcher = model.clone()
        patcher.add_wrapper_with_key(
            WrappersMP.OUTER_SAMPLE, "studio_progress", SamplerProgressWrapper(on_step)
        )
        return patcher
    except Exception as exc:  # noqa: BLE001
        log.warning("挂采样进度包装器失败（进度条/live 预览将不可用）：%s", exc)
        return model


# ---------- 中断清理（ComfyUI 官方路径在中断时会被跳过的收尾） ----------

_GUARD_INSTALLED = False


def install_sampling_interrupt_guards() -> None:
    """给 CFGGuider.outer_sample 补一层「异常也清理」的护罩（幂等）。

    官方 outer_sample() 的收尾写在函数末尾、**不在 finally 里**：

    - comfy.sampler_helpers.cleanup_models(self.conds, self.loaded_models)
    - del self.inner_model / del self.loaded_models

    采样循环里抛 InterruptProcessingException（中断）时会直接跳过这几行，于是 guider 一直
    抱着 inner_model（真实 BaseModel）/ loaded_models / conds。guider 是 BasicGuider 节点的
    缓存输出、会跨 prompt 存活，被抱住的模型就永远回收不了 —— 这正是中断后
    memory leak with model 警告的来源。这里只在异常路径补做同等清理（正常返回不受影响）。
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    try:
        import comfy.sampler_helpers as _sh
        import comfy.samplers as _cs
    except Exception:  # noqa: BLE001 非采样环境（测试）直接跳过
        return

    orig = getattr(_cs.CFGGuider, 'outer_sample', None)
    if orig is None:
        _GUARD_INSTALLED = True
        return
    if getattr(orig, '_studio_interrupt_cleanup', False):
        _GUARD_INSTALLED = True
        return

    def outer_sample(self, *args, **kwargs):
        try:
            return orig(self, *args, **kwargs)
        except BaseException:
            # 官方只在正常返回时清理；异常（尤其中断）时由我们补上
            try:
                _sh.cleanup_models(
                    getattr(self, 'conds', None) or {},
                    getattr(self, 'loaded_models', None) or [],
                )
            except Exception:  # noqa: BLE001
                log.debug("中断清理: 采样附加模型清理失败", exc_info=True)
            for name in ('inner_model', 'loaded_models', 'conds'):
                if hasattr(self, name):
                    try:
                        delattr(self, name)
                    except Exception:  # noqa: BLE001
                        pass
            raise

    outer_sample._studio_interrupt_cleanup = True  # type: ignore[attr-defined]
    outer_sample._studio_orig = orig  # type: ignore[attr-defined]
    try:
        _cs.CFGGuider.outer_sample = outer_sample  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        return
    _GUARD_INSTALLED = True
    log.debug("已安装采样中断清理护罩（CFGGuider.outer_sample）")


def cleanup_after_interrupt() -> None:
    """中断后补做 ComfyUI 正常路径会做、但中断 / 异步节点路径跳过的全局清理。

    只做清理不做业务：任何一步失败都静默降级。
    """
    # 1) 模型前向在模块级全局留下的 prefetch 队列 / CUDA graph（执行器 per-node finally 的活）
    try:
        import comfy.model_prefetch

        comfy.model_prefetch.cleanup_prefetch_queues()
    except Exception:  # noqa: BLE001
        log.debug("中断清理: prefetch queue 清理失败", exc_info=True)

    # 2) aimdo 的 cast buffer / vbar watermark（与执行器 finally 一致）
    try:
        import comfy.memory_management as cmm
        import comfy.model_management as mm

        if getattr(cmm, 'aimdo_enabled', False):
            mm.reset_cast_buffers()
            try:
                import comfy_aimdo.model_vbar

                comfy_aimdo.model_vbar.vbars_reset_watermark_limits()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        log.debug("中断清理: aimdo 缓存重置失败", exc_info=True)

    # 3) 先回收没人引用的模型，再让 ComfyUI 删掉对应的 dead 记录（true leak 只能靠这步清）
    try:
        import gc

        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    try:
        import comfy.model_management as mm

        mm.cleanup_models()
        mm.soft_empty_cache()
    except Exception:  # noqa: BLE001
        log.debug("中断清理: 显存回收失败", exc_info=True)


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
