"""Task 基类 —— 每个生成模式一个子类，执行器只调度。

设计要点：
- BaseTask.execute() 是模板方法：validate → build_conditioning → sample → decode
- 子类唯一必须实现 build_conditioning()（本模式的条件构建/素材校验）
- sample / decode 为基类公共逻辑（真实链路接入后在此实现）
- 子类不直接 import comfy 节点，全部经 TaskContext 注入，保持可独立测试
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from ..payload import (
    CanvasConfig,
    ClipPayload,
    StudioPayload,
)
from ..sampling import run_minimax_conditioning

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.studio")


@dataclass(frozen=True)
class SamplingConfig:
    """全局采样参数（由节点 widget 注入，不进 timeline_data）。"""

    seed: int = 0
    steps: int = 25
    cfg: float = 1.0
    sampler: str = "res_multistep"
    scheduler: str = "simple"
    shift_video: float = 12.0
    shift_audio: float = 3.0


@dataclass(frozen=True)
class TaskContext:
    """一次执行任务的运行时上下文：模型依赖 + 全局配置 + 进度回调。"""

    canvas: CanvasConfig
    sampling: SamplingConfig
    # 段间引导（Motion Context latent 传递）：默认关闭
    continuity_enabled: bool = False
    continuity_frames: int = 22
    # 模型依赖（由节点注入；缺失的依赖在 validate 阶段拦截）
    model: Any = None
    video_vae: Any = None
    audio_vae: Any = None
    clip: Any = None
    # 进度回调 progress(seg_id, phase, value, step=None, steps_total=None, x0=None)，可选
    # x0：采样每步的预测去噪 latent（AV NestedTensor，live 预览解码用；GPU tensor 生命周期仅回调内）
    progress: Any = None


@dataclass(frozen=True)
class ConditioningResult:
    """条件构建结果（描述将如何调用官方节点）。

    frozen 不可变：消费链中不会被意外改动；需要派生变体时用 with_* 方法
    （内部 dataclasses.replace 生成新实例，保持不可变性）。
    """

    node: str  # "MiniMaxH3ImageToVideo" | "MiniMaxH3ReferenceToVideo"
    prompt: str
    width: int
    height: int
    length: int  # 对齐 17k+5 后的帧数
    first_frame: str | None = None
    last_frame: str | None = None
    ref_images: dict[str, str] = field(default_factory=dict)
    ref_videos: dict[str, str] = field(default_factory=dict)
    ref_audios: dict[str, str] = field(default_factory=dict)
    ref_image_size: str = "match"

    def with_length(self, length: int) -> "ConditioningResult":
        """派生新实例：覆盖采样长度（段间引导时 = 可见帧 + 上下文帧）。"""
        return replace(self, length=length)

    def summary(self) -> str:
        parts = [
            f"节点={self.node}",
            f"{self.width}×{self.height}",
            f"{self.length}帧",
        ]
        if self.first_frame:
            parts.append(f"首帧={self.first_frame}")
        if self.last_frame:
            parts.append(f"尾帧={self.last_frame}")
        for key, val in self.ref_images.items():
            parts.append(f"{key}={val}")
        for key, val in self.ref_videos.items():
            parts.append(f"{key}={val}")
        for key, val in self.ref_audios.items():
            parts.append(f"{key}={val}")
        if self.ref_image_size != "match":
            parts.append(f"ref_size={self.ref_image_size}")
        return " ".join(parts)


@dataclass
class SegmentResult:
    """单段执行结果。"""

    segment_index: int
    mode: str
    frames: int
    conditioning: ConditioningResult
    images: Any = None
    audio: Any = None
    prompt: str = ""
    # 采样输出的 AV latent（段间引导直接传递用，不 VAE 解码）
    av_latent: Any = None


class BaseTask(ABC):
    """任务抽象基类：模板方法定义整段执行流水线。"""

    mode: ClassVar[str] = ""

    def __init__(self, segment: ClipPayload, ctx: TaskContext):
        if not self.mode:
            raise TypeError(f"{type(self).__name__} 必须定义 mode 类属性")
        self.segment = segment
        self.ctx = ctx

    # ---------- 模板方法 ----------

    async def execute(self, prev_av: Any = None) -> SegmentResult:
        """采样本段并返回 AV latent（不解码）。

        解码在任务末尾统一进行（采样-解码分离架构，见 executor）。
        prev_av 为上一段采样输出的 AV latent（段间引导用，从磁盘加载）。

        async：采样流程（子图）的内层节点执行要走 ComfyUI 官方执行器（await），
        而 Studio 节点本身也是 async，所以整条链路都在 ComfyUI 的事件循环线程里，
        不另起线程做 CUDA（跨线程张量生命周期会让 cudaMallocAsync 在中断时炸进程）。
        """
        self.validate()
        conditioning = self.build_conditioning()

        # 段间引导：上一段 latent 尾部钉入当前段（latent 直接传递，不 VAE 解码）。
        # 是否续接由 executor 按片段开关（clip.continuity）决定 prev_av 是否传入
        use_continuity = prev_av is not None
        trim_frames = 0
        if use_continuity:
            from ..motion_context import generation_frame_budget

            sample_len, trim_frames = generation_frame_budget(
                conditioning.length, self.ctx.continuity_frames
            )
            # 派生采样长度（可见帧 + 上下文帧）的新 ConditioningResult
            conditioning = conditioning.with_length(sample_len)

        # 真实链路：conditioning（官方节点）→ 采样（官方自定义采样节点组合）
        positive, latent = run_minimax_conditioning(self.ctx, conditioning)
        grid_fit = False
        if use_continuity:
            from ..motion_context import (
                apply_motion_context as _apply_mc,
                install_cond_grid_fit,
                uninstall_cond_grid_fit,
            )

            positive, trim_frames = _apply_mc(
                positive, latent, prev_av, self.ctx.continuity_frames
            )
            # 钉入内容按"当级采样网格"缩放：采样流程（子图）可能中途放大 latent（二采），
            # 第二级换了画布后条件行必须跟着重缩，否则 core 报 shape mismatch。
            # 装不上时退回 apply_motion_context 里按本段网格做的静态那份。
            grid_fit = install_cond_grid_fit(self.ctx.model)

        try:
            samples = await self.sample(positive, latent)
        finally:
            if grid_fit:
                uninstall_cond_grid_fit()

        return SegmentResult(
            segment_index=0,
            mode=self.mode,
            frames=conditioning.length - trim_frames,
            conditioning=conditioning,
            av_latent=samples,  # 采样输出，写盘后由执行器释放
            prompt=self.segment.prompt,
        )

    # ---------- 子类必须实现 ----------

    @abstractmethod
    def build_conditioning(self) -> ConditioningResult:
        """构建本模式的条件（所有模式共用同一描述结构）。"""

    # ---------- 子类可选覆写 ----------

    def validate(self) -> None:
        """素材校验，默认不校验；如 v2v 必须有源视频。"""

    # ---------- 基类公共：真实采样链路（官方 MiniMax H3 流程） ----------

    async def sample(self, positive, latent: dict) -> dict:
        """采样：挂了采样流程（子图）就走子图，否则走内置官方链。

        内置链：SigmaShift → BasicScheduler → Guider → SamplerCustomAdvanced，
        每步进度经包装 guider.sample 转发 → ctx.progress（前端卡片进度条）。
        内置链是同步调用（官方节点类直接调），不需要 await。"""
        from ..sampling import install_sampling_interrupt_guards

        # 补上 ComfyUI 中断路径会跳过的收尾（否则 guider 会一直抱着真实模型 → 泄漏警告）
        install_sampling_interrupt_guards()
        if self.segment.pipeline is not None:
            return await self._sample_with_pipeline(positive, latent)

        from ..sampling import sample_single_stage

        s = self.ctx.sampling
        return sample_single_stage(
            model=self.ctx.model,
            positive=positive,
            negative=[],
            latent=latent,
            seed=s.seed,
            cfg=s.cfg,
            steps=s.steps,
            sampler_name=s.sampler,
            scheduler=s.scheduler,
            shift_video=s.shift_video,
            shift_audio=s.shift_audio,
            progress=self._ksampler_progress(),
        )

    async def _sample_with_pipeline(self, positive, latent: dict) -> dict:
        """执行该片段绑定的采样流程（前端摊平图 → 官方零件调度，见 studio/pipeline.py）。

        进度/预览：把逐步回调挂到 model 上（OUTER_SAMPLE 包装器，见
        sampling.attach_sampler_progress），因此用户链里**每段采样**都会推进进度条、
        推送 x0 给 live 预览（与内置链同一条 studio_progress / studio_preview 通道）。
        极少数不含采样的流程（纯 latent 运算）没有逐步回调，只有首尾两次粗略进度。
        """
        from ..pipeline import PipelineError, run_pipeline_graph
        from ..sampling import build_pipeline_runtime

        pipeline = self.segment.pipeline
        assert pipeline is not None
        s = self.ctx.sampling
        runtime = build_pipeline_runtime(
            model=self.ctx.model,
            positive=positive,
            latent=latent,
            seed=s.seed,
            steps=s.steps,
            sampler_name=s.sampler,
            scheduler=s.scheduler,
            shift_video=s.shift_video,
            shift_audio=s.shift_audio,
            on_step=self._ksampler_progress(),
        )
        self._report("sampling", 0.0)
        try:
            return await run_pipeline_graph(
                pipeline.graph,
                runtime,
                prompt_id=f"studio-pipeline-{self.segment.id}",
            )
        except PipelineError as exc:
            # 采样流程的问题要能一眼分清"我（用户）违规了"还是"插件坏了"，
            # 因此统一带上片段与流程名（PipelineError 的消息本身已是用户可读中文）。
            raise PipelineError(
                f"片段 {self.segment.id} 的采样流程「{pipeline.name}」无法执行：{exc}"
            ) from exc
        finally:
            self._report("sampling", 1.0)

    def _ksampler_progress(self):
        """采样每步回调 → ctx.progress(seg_id, 'sampling', 0~1)（前端卡片进度条 + live 预览）。

        签名与采样器的逐步回调对齐：callback(step, x0, x, total_steps)。两条注入路径：
        - 内置链：包装 `guider.sample`（见 sampling.sample_single_stage）
        - 采样流程（子图）：挂在 model 上的 OUTER_SAMPLE 包装器
          （见 sampling.attach_sampler_progress），链上每段采样都会触发
        """
        cb = self.ctx.progress
        if cb is None:
            return None

        def on_step(step: int, x0, _x, steps_total: int) -> None:
            if steps_total > 0:
                # step 从 0 开始（0..total-1）：+1 后首步=1/total、末步=total/total=100%
                cb(
                    self.segment.id, "sampling", (step + 1) / steps_total,
                    step + 1, steps_total, x0,
                )

        return on_step

    def decode(self, samples: dict) -> tuple[Any, dict]:
        """AV latent 直接解码（VAEDecode + VAEDecodeAudio 同吃 AV latent，不分离）。"""
        from ..sampling import decode_av_latent

        return decode_av_latent(
            samples,
            self.ctx.video_vae,
            self.ctx.audio_vae,
            decode_audio=True,
        )

    # ---------- 工具 ----------

    def _frames(self) -> int:
        return self.segment.frames(self.ctx.canvas.fps)

    def _report(self, phase: str, value: float = 0.0) -> None:
        if self.ctx.progress:
            try:
                self.ctx.progress(self.segment.id, phase, value)
            except Exception:  # noqa: BLE001 进度回调异常不影响执行
                log.debug("progress 回调异常", exc_info=True)


def validate_audio_deps(ctx: TaskContext, segment: ClipPayload, needs_audio: bool) -> None:
    """r2v/v2v/rv2v 需要 audio_vae 的公共校验。"""
    if needs_audio and ctx.audio_vae is None:
        raise ValueError(
            f"段 {segment.id} (mode={segment.mode}): 参考音频/源视频需要 audio_vae 输入"
        )


# 官方 MiniMaxH3ReferenceToVideo 参考素材数量上限（Autogrow 声明）
MAX_REF_IMAGES = 9
MAX_REF_VIDEOS = 3
MAX_REF_AUDIOS = 3


def _check_ref_limits(
    segment: ClipPayload,
    ref_images: dict | None,
    ref_videos: dict | None,
    ref_audios: dict | None,
) -> None:
    """参考素材数量校验：超官方上限会被官方节点静默忽略（参考丢失无提示）。"""
    for name, refs, limit in (
        ("参考图", ref_images, MAX_REF_IMAGES),
        ("参考视频", ref_videos, MAX_REF_VIDEOS),
        ("参考音频", ref_audios, MAX_REF_AUDIOS),
    ):
        count = len(refs or {})
        if count > limit:
            raise ValueError(
                f"段 {segment.id}: {name}数量 {count} 超过官方上限 {limit}"
                "（超出的会被 MiniMaxH3ReferenceToVideo 忽略）"
            )


def build_common_conditioning(
    ctx: TaskContext,
    segment: ClipPayload,
    *,
    use_reference: bool,
    first_frame: str | None = None,
    last_frame: str | None = None,
    ref_images: dict[str, str] | None = None,
    ref_videos: dict[str, str] | None = None,
    ref_audios: dict[str, str] | None = None,
    ref_image_size: str = "match",
) -> ConditioningResult:
    """子类通用的 ConditioningResult 构造（对应官方 ImageToVideo / ReferenceToVideo）。"""
    _check_ref_limits(segment, ref_images, ref_videos, ref_audios)
    node = "MiniMaxH3ReferenceToVideo" if use_reference else "MiniMaxH3ImageToVideo"
    return ConditioningResult(
        node=node,
        prompt=segment.prompt,
        width=ctx.canvas.width,
        height=ctx.canvas.height,
        length=segment.frames(ctx.canvas.fps),
        first_frame=first_frame,
        last_frame=last_frame,
        ref_images=ref_images or {},
        ref_videos=ref_videos or {},
        ref_audios=ref_audios or {},
        ref_image_size=ref_image_size,
    )
