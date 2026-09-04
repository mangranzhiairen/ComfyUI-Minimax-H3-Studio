"""StudioExecutor —— 执行器：只管调度，不含任何模式业务逻辑。

架构（采样-解码分离）：
1. 反序列化 StudioPayload，创建任务记录（SQLite）
2. 工厂注入：按 mode 创建 Task 子类实例
3. 采样阶段：逐段执行（Task 只采样返回 AV latent，不解码）
   → latent 写盘（指纹命名）→ 释放内存；段间引导从磁盘加载上一段 latent
4. 解码阶段：任务末尾统一从磁盘加载各段 latent → VAE 解码 → 合并视频/音频
5. 更新任务状态，汇总 ExecutionResult
"""

from __future__ import annotations

import gc
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import torch

from .payload import StudioPayload, PayloadValidationError, clip_to_snapshot, load as load_payload
from .segment_cache import (
    canvas_label,
    content_fingerprint,
    create_task as db_create_task,
    get_sample_meta,
    latent_path,
    load_av_latent,
    merge_audios,
    preview_path,
    preview_url,
    record_version_sample,
    sample_fingerprint,
    save_av_latent,
    update_task_node_id,
    update_task_status,
)
from .tasks import SamplingConfig, SegmentResult, TaskContext, create_task

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.studio")


def _cached_conditioning(segment, sample_len: int, ctx: TaskContext):
    """缓存命中/强制切换时构造最小 ConditioningResult（report 展示用，不参与采样）。"""
    from .tasks import ConditioningResult

    return ConditioningResult(
        node="缓存命中",
        prompt=segment.prompt,
        width=ctx.canvas.width,
        height=ctx.canvas.height,
        length=int(sample_len),
    )


def _broadcast_progress(node_id: str, canvas_fps: float = 24.0):
    """构造段级采样进度回调：向 ComfyUI 前端广播 studio_progress 事件。

    回调签名 progress(seg_id, phase, value, step=None, steps_total=None, x0=None)：
    KSampler 每步（phase='sampling'）经 TaskContext.progress 转发到此，
    前端据此在对应片段卡片底部显示进度条与「当前步数/总步数」。
    x0 为采样每步的预测去噪 latent（AV NestedTensor，GPU tensor 生命周期仅回调内）：
    按节流频率生成 live 预览（TAE 解码 video 流 → 动画 WebP → studio_preview 事件，
    编码/发送放异步线程，队列满则丢弃，不阻塞采样）；预览时长对齐视频实际时长。
    """
    def on_progress(seg_id: str, phase: str, value: float, step=None, steps_total=None, x0=None) -> None:
        try:
            from server import PromptServer

            server = PromptServer.instance
            if server is None:
                return
            payload = {
                "node_id": str(node_id), "clipId": str(seg_id), "phase": phase, "value": float(value),
            }
            if step is not None:
                payload["step"] = int(step)
                payload["stepsTotal"] = int(steps_total or 0)
            server.send_sync("studio_progress", payload)
        except Exception:  # noqa: BLE001 无 ComfyUI 环境（单测）时静默
            pass
        if phase == "sampling" and x0 is not None and step is not None and steps_total:
            _try_live_preview(node_id, seg_id, step, steps_total, x0, canvas_fps)

    return on_progress


def _try_live_preview(node_id: str, clip_id: str, step: int, steps_total: int, x0, canvas_fps: float) -> None:
    """live 预览：节流（约 3 次/段）→ TAE 解码 x0 的 video 流（帧数按实际时长）→ 异步编码 → 广播。

    任何失败静默降级（预览是增强信息，不阻断采样）。
    """
    steps_total = int(steps_total or 0)
    if steps_total <= 0:
        return
    # 过程示意：整段约 2 次（初/末）；24 帧上限解码 ≤3.4s/次，降频控成本
    import math

    every = max(1, math.ceil(steps_total / 2))
    if (int(step) - 1) % every != 0 and int(step) != steps_total:
        return
    from .tae_preview import (
        LIVE_PREVIEW_FRAME_CAP,
        decode_preview_frames,
        encode_preview_webp,
        get_encoder,
        preview_frames_for_latent,
        video_stream,
    )

    video = video_stream(x0)
    if video is None or video.ndim != 5:
        return
    max_frames = preview_frames_for_latent(
        int(video.shape[2]), canvas_fps=canvas_fps, cap=LIVE_PREVIEW_FRAME_CAP
    )
    frames = decode_preview_frames(video, max_frames=max_frames)
    if not frames:
        return
    encoder = get_encoder()

    def _send():
        b64 = encode_preview_webp(frames)
        if not b64:
            return
        try:
            from server import PromptServer

            server = PromptServer.instance
            if server is None:
                return
            server.send_sync(
                "studio_preview",
                {
                    "node_id": str(node_id), "clipId": str(clip_id),
                    "image": b64, "mime": "image/webp",
                    "step": int(step), "stepsTotal": steps_total,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    encoder.submit(_send)  # 队列满则丢弃本次预览


def _broadcast_segment_done(node_id: str):
    """片段采样完成广播：executor 每段采样成功（落库后）→ 前端刷新历史。

    executed 只在节点整体执行完成时触发，逐段刷新需独立事件；version_id 是本次
    采样归属的版本 id，前端据此把该片段当前数据更新为采样快照；preview_url 为
    final preview 的 /view 地址（与 latent 缓存绑定，卡片保持显示最终预览）。
    """
    def on_done(clip_id: str, version_id: int, preview_url: str = "") -> None:
        try:
            from server import PromptServer

            server = PromptServer.instance
            if server is None:
                return
            payload = {
                "node_id": str(node_id), "clipId": str(clip_id), "versionId": int(version_id),
            }
            if preview_url:
                payload["previewUrl"] = preview_url
            server.send_sync("studio_clip_done", payload)
        except Exception:  # noqa: BLE001
            pass

    return on_done


def _save_final_preview(pv_path, av_latent: dict, duration_sec: float = 0.0) -> bool:
    """final preview：TAE 解码最终 latent 的 video 流 → 动画 WebP 落盘（best-effort）。

    帧数按片段设定时长精确推算（上限 5 秒=40 帧，长段压缩保护成本；
    实际解码受 latent token 数限制）；返回是否生成成功（调用方据此构造 /view URL）。
    """
    try:
        from .tae_preview import (
            decode_preview_frames,
            preview_frames_for_duration,
            save_preview_file,
            video_stream,
        )

        video = video_stream(av_latent.get("samples"))
        if video is None or video.ndim != 5:
            return False
        max_frames = preview_frames_for_duration(duration_sec)
        frames = decode_preview_frames(video, max_frames=max_frames)
        if not frames:
            return False
        return save_preview_file(pv_path, frames)
    except Exception as exc:  # noqa: BLE001
        log.debug("final preview 生成失败: %s", exc)
        return False


def _make_context(
    payload: StudioPayload,
    *,
    sampling: SamplingConfig,
    deps: dict,
    continuity_frames: int = 22,
    progress=None,
) -> TaskContext:
    return TaskContext(
        canvas=payload.canvas,
        sampling=sampling,
        continuity_frames=continuity_frames,
        model=deps.get("model"),
        video_vae=deps.get("video_vae"),
        audio_vae=deps.get("audio_vae"),
        clip=deps.get("clip"),
        progress=progress,
    )


@dataclass
class ExecutionResult:
    """一次执行的汇总结果。"""

    payload: StudioPayload
    segments: list[SegmentResult] = field(default_factory=list)
    report: str = ""
    task_id: str = ""
    # 合并输出：images 为全部段帧拼接的 IMAGE，audio 为拼接的 AUDIO
    images: Any = None
    audio: Any = None

    def build_report(self) -> str:
        lines: list[str] = []
        lines.append("创意工作台执行报告（真实采样）")
        if self.task_id:
            lines.append(f"任务: {self.task_id}")
        lines.append(f"画布: {self.payload.canvas.width}×{self.payload.canvas.height} @ {self.payload.canvas.fps}fps")
        lines.append(f"总时长: {self.payload.total_duration_sec}s")
        lines.append(f"片段: {len(self.payload.clips)} 段 | 执行: {len(self.segments)} 段（未勾选跳过）")
        for seg in self.segments:
            lines.append(
                f"  #{seg.segment_index + 1} [{seg.mode}] {seg.prompt[:24] or '（空）'} "
                f"→ {seg.conditioning.summary()}"
            )
        return "\n".join(lines)


class StudioExecutor:
    """执行器：反序列化 → 工厂注入 → 采样落盘 → 末尾解码合并。"""

    def __init__(
        self,
        *,
        sampling: SamplingConfig | None = None,
        deps: dict | None = None,
        node_id: str = "",
        continuity_frames: int = 22,
        unload_models_after: bool = False,
    ):
        self.sampling = sampling or SamplingConfig()
        self.deps = deps or {}
        self.node_id = node_id
        self.continuity_frames = continuity_frames
        # 采样完成后（解码前）卸载采样模型并清显存缓存：解码显存更宽松，
        # 但 UNET/CLIP 后续需重新加载（本任务内不再使用，无额外开销）
        self.unload_models_after = bool(unload_models_after)

    def run(self, timeline_data: str | dict) -> ExecutionResult:
        # 数据契约（定死，不做历史形态兼容）：节点 timeline_data widget 由前端
        # serializeValue 填充为 JSON 字符串：
        #   {"taskId": string|null, "payload": {version, canvas, segments, totalDurationSec}}
        # - payload：前端实时构建的时间线（用户正在编辑的，永远是对的）→ 直接执行
        # - taskId：仅用于写 DB 记录（segments/latent/历史），不参与执行；null=未加载任务
        if isinstance(timeline_data, str):
            try:
                envelope = json.loads(timeline_data or "")
            except json.JSONDecodeError as exc:
                raise PayloadValidationError(f"timeline_data 不是合法 JSON: {exc}") from exc
        else:
            envelope = timeline_data
        if not isinstance(envelope, dict):
            raise PayloadValidationError("timeline_data 必须是对象 {taskId, payload}")

        payload_data = envelope.get("payload")
        if not isinstance(payload_data, dict):
            raise PayloadValidationError("timeline_data.payload 缺失（数据契约: {taskId, payload}）")
        raw_tid = envelope.get("taskId")
        task_id = str(raw_tid) if raw_tid else None

        # 数据契约错误向上抛出，由节点层捕获并回显到 report
        payload = load_payload(payload_data)

        # 采样前兜底校验：锁定 latent 的片段其缓存画布必须与当前画布一致（防解码崩溃）
        self._validate_locked_latent(payload, task_id)

        ctx = _make_context(
            payload,
            sampling=self.sampling,
            deps=self.deps,
            continuity_frames=self.continuity_frames,
            progress=_broadcast_progress(self.node_id, float(payload.canvas.fps or 24)),  # 段级采样进度 → 前端卡片进度条
        )

        # 工厂注入：所有片段都实例化（未勾选的片段也会被校验，保证数据完整性）
        tasks = [create_task(clip, ctx) for clip in payload.clips]
        # ===== 真实采样（采样-解码分离） =====
        sampling_dict = asdict(self.sampling)
        # 任务记录：有 taskId 复用已有任务（DB 纯持久化，执行不读 DB）；无则新建。
        # timeline 存时间线当前数据（前端同款格式：canvas + clips[]），片段当前参数草稿由前端维护。
        if task_id is None:
            # 无前端任务（正常流程编辑期已由前端建任务，此为兜底）：创建占位任务。
            # timeline 直接存前端契约同构的完整 payload（原始 camel JSON，含全部草稿）——
            # 即使之后被 loadTask 加载也是完整可编辑时间线，而非残缺骨架。
            task_id = db_create_task(
                self.node_id,
                json.dumps(payload_data, ensure_ascii=False),
                sampling_dict,
            )
        update_task_node_id(task_id, self.node_id)  # 同步节点 id（节点可能重建，保证路径推导一致）
        update_task_status(task_id, "running")

        try:
            # 采样阶段：排列任务并执行（Task 只产出 latent）→ 写盘 → 释放
            results, sampled = self._sample_all(tasks, ctx, sampling_dict, task_id)
            # 采样完成 → 解码前：可选卸载采样模型（UNET/CLIP）并清显存缓存，
            # 显存让给 VAE 解码；VAE 保留避免重载
            if self.unload_models_after:
                self._free_sampling_models(ctx)
            # 解码合并阶段：获取要解码的 latent → 解码 → 合并输出
            merged_images, merged_audio = self._decode_and_merge(sampled, ctx)
            update_task_status(task_id, "done")

            report = ExecutionResult(
                payload=payload,
                segments=results,
                task_id=task_id,
                images=merged_images,
                audio=merged_audio,
            )
            report.report = report.build_report()
            if self.unload_models_after:
                report.report += "\n任务完成后：已卸载采样模型（UNET/CLIP）并清除显存缓存（VAE 保留）。"
            return report
        except Exception:
            update_task_status(task_id, "failed")
            raise

    def _free_sampling_models(self, ctx: TaskContext) -> None:
        """采样完成 → 解码前：卸载 UNET/CLIP 到内存并清显存缓存，显存让给 VAE 解码。

        free_memory 卸载除 VAE 外的所有已加载模型（VAE 解码要用，保留避免重载）；
        soft_empty_cache 释放 caching allocator 空闲块。任何失败静默降级——
        解码时 ComfyUI 的 load_model_gpu 会自动 free_memory，此处只是提前腾位。
        """
        from comfy import model_management as mm

        # 保留 VAE 相关模型：VAE 已加载（采样 conditioning 阶段用过）时直接匹配
        keep_loaded = []
        for vae in (ctx.video_vae, ctx.audio_vae):
            if vae is None or not hasattr(vae, "load_model"):
                continue
            try:
                vae_models = list(vae.load_model())
            except Exception:  # noqa: BLE001
                vae_models = []
            keep_loaded.extend(lm for lm in mm.current_loaded_models if lm.model in vae_models)
        try:
            mm.free_memory(1e30, mm.get_torch_device(), keep_loaded=keep_loaded)
        except Exception as exc:  # noqa: BLE001
            log.debug("采样模型卸载失败（解码由 ComfyUI 自动管理）: %s", exc)
        try:
            mm.soft_empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def _validate_locked_latent(self, payload: StudioPayload, task_id: str | None) -> None:
        """采样前兜底校验：已启用且锁定 latent 的片段，其缓存画布必须与当前画布一致。

        内容是否匹配由前端"启用采样"动作保证（锁定 latent 时联动恢复该采样的画面语义），
        这里不再比对内容指纹；只拦截真正会崩溃的画布尺寸不一致（复用旧画布 latent
        会在解码合并/段间引导时形状崩溃）。未启用片段不校验（不参与执行）。
        不匹配的片段全部收集一次性报错，避免采样进行到中途才失败。
        """
        if not task_id:
            return  # 无任务（未加载）时无历史可查，采样决策层会兜底降级
        from .segment_cache import get_sample_canvas

        canvas_now = canvas_label(asdict(payload.canvas))
        problems: list[str] = []
        for clip in payload.clips:
            if not clip.enabled or not clip.sample_fp:
                continue
            stored_canvas = get_sample_canvas(task_id, clip.id, clip.sample_fp)
            if stored_canvas and stored_canvas != canvas_now:
                problems.append(
                    f"片段「{clip.prompt[:16] or clip.id}」锁定的 latent 基于 {stored_canvas}，"
                    f"当前画布为 {canvas_now}（画布已变更，该 latent 尺寸不匹配无法出片），"
                    "请取消锁定，或切换回原画布后重新采样"
                )
        if problems:
            raise PayloadValidationError("采样前校验失败：\n- " + "\n- ".join(problems))

    def _sample_all(
        self,
        tasks: list,
        ctx: TaskContext,
        sampling_dict: dict,
        task_id: str,
    ) -> tuple[list[SegmentResult], list[tuple[int, int, str]]]:
        """采样编排：逐段执行（Task 产出 latent）→ latent 写盘 → 释放内存。

        段间引导的上一段 latent 从磁盘加载（内存 0 驻留）。
        返回 (results, sampled)，sampled = [(clip_id, sample_length, export_frames, latent_file)]。
        """
        results: list[SegmentResult] = []
        sampled: list[tuple[str, int, int, str]] = []
        prev_file: str | None = None

        for index, task in enumerate(tasks):
            if not task.segment.enabled:
                continue

            # 段间续接：每片段独立开关（clip.continuity），非全局
            use_cont = bool(task.segment.continuity)
            # 片段内容指纹（提示词条目）+ 采样指纹（latent 文件：clip 归属 + 内容 + 规格 + 工艺）
            content_fp = content_fingerprint(task.segment)
            sample_fp = sample_fingerprint(
                task.segment.id,
                content_fp,
                sampling_dict,
                continuity_enabled=use_cont,
                continuity_frames=self.continuity_frames,
                canvas=canvas_label(asdict(ctx.canvas)),
                duration_sec=float(task.segment.duration_sec or 0),
            )
            # 抽卡级反悔：用户显式指定的历史采样（seed 等取历史记录，跳过采样）
            forced_fp = task.segment.sample_fp or None
            use_fp: str | None = None
            if forced_fp and forced_fp != sample_fp:
                # 强制指定版本（即使与当前参数不同）→ 直接用该 latent 出片
                if latent_path(self.node_id, forced_fp).exists():
                    use_fp = forced_fp
            elif latent_path(self.node_id, sample_fp).exists():
                # 同卡片同参数（含 seed）命中：容器内缓存复用，不重复采样
                use_fp = sample_fp

            if use_fp is not None:
                meta = get_sample_meta(task_id, task.segment.id, use_fp)
                if meta is not None:
                    sample_len, export_frames = meta
                    sampled.append((task.segment.id, sample_len, export_frames, str(latent_path(self.node_id, use_fp))))
                    results.append(
                        SegmentResult(
                            segment_index=index,
                            mode=task.segment.mode,
                            frames=export_frames,
                            conditioning=_cached_conditioning(task.segment, sample_len, ctx),
                            prompt=task.segment.prompt,
                        )
                    )
                    prev_file = str(latent_path(self.node_id, use_fp))  # 下段引导从该缓存加载
                    gc.collect()
                    continue

            # 段间引导：该片段启用续接且存在上一段 latent 时从磁盘加载
            prev_av = load_av_latent(prev_file) if use_cont else None
            result = task.execute(prev_av=prev_av)
            if prev_av is not None:
                del prev_av
            result.segment_index = index
            results.append(result)

            if result.av_latent is not None:
                fpath = latent_path(self.node_id, sample_fp)
                ok = save_av_latent(fpath, result.av_latent)
                # final preview：TAE 解码最终 latent 的 video 流 → 动画 WebP 落盘
                # （preview_{sample_fp}.webp，与 latent 同目录同指纹，随缓存同删/同切换；
                #   帧数按片段设定时长精确推算，上限 5 秒；播放时长由前端帧率自适应兜底）
                pv_ok = _save_final_preview(
                    preview_path(self.node_id, sample_fp),
                    result.av_latent,
                    float(task.segment.duration_sec or 0),
                )
                result.av_latent = None  # 释放内存/显存
                if ok:
                    # 历史只在采样成功时产生：固化片段版本（复用/新建）+ 采样记录
                    version_id = record_version_sample(
                        task_id,
                        task.segment.id,
                        content_fp,
                        sample_fp,
                        snapshot=clip_to_snapshot(task.segment),
                        sampling=sampling_dict,
                        frames=result.frames,
                        sample_len=result.conditioning.length,
                        canvas=canvas_label(asdict(ctx.canvas)),
                        duration_sec=float(task.segment.duration_sec or 0),
                        continuity=use_cont,
                    )
                    _broadcast_segment_done(self.node_id)(
                        task.segment.id,
                        version_id,
                        preview_url=preview_url(self.node_id, sample_fp) if pv_ok else "",
                    )  # 段完成 → 前端刷新历史 + 卡片固化最终预览
                    sampled.append((task.segment.id, result.conditioning.length, result.frames, str(fpath)))
                    prev_file = str(fpath)  # 下段引导从盘加载
                else:
                    prev_file = None  # 写盘失败则退化：下段无引导
            # 段间清理：只 gc Python 引用（安全、低开销）。
            # 不卸载/不清理模型、不碰 CUDA 缓存——保持模型驻留复用，
            # 不影响采样性能；模型管理残留是 ComfyUI 低显存模式的固有行为。
            gc.collect()

        return results, sampled

    def _decode_and_merge(
        self, sampled: list[tuple[str, int, int, str]], ctx: TaskContext
    ) -> tuple[Any, dict]:
        """解码合并：获取要解码的 latent → VAE 解码 → 帧/音频拼接为最终视频。

        流式合并：预分配总帧数大小的合并 tensor，逐段解码后直接写入并释放
        该段（不累积所有段帧），峰值 = 单段解码 + 合并 tensor，而非 N 倍全量。
        每段解码前后经 ctx.progress 广播 phase='decoding' 进度，前端卡片显示绿框。
        """
        from .sampling import decode_av_latent

        if not sampled:
            raise ValueError("创意工作台：没有可解码的已采样片段")

        total_frames = sum(export for _, _, export, _ in sampled)
        merged: Any = None
        merged_audio: dict | None = None
        offset = 0
        total = len(sampled)
        for index, (clip_id, sample_len, export_frames, fpath) in enumerate(sampled):
            # 解码中广播：卡片绿框指示当前处理的 clip（段级进度）
            if ctx.progress:
                try:
                    ctx.progress(clip_id, "decoding", index / total)
                except Exception:  # noqa: BLE001
                    pass
            lat = load_av_latent(fpath)
            if lat is None:
                raise RuntimeError(f"段 latent 缓存丢失: {fpath}")
            images, audio = decode_av_latent(lat, ctx.video_vae, ctx.audio_vae)
            del lat
            # 段间引导的上下文前缀在解码后裁掉
            trim = sample_len - export_frames
            if trim > 0:
                from .motion_context import trim_context_prefix

                images, audio = trim_context_prefix(
                    images, audio, trim, fps=float(ctx.canvas.fps or 24)
                )
            if merged is None:
                # 首段确定帧尺寸后预分配合并 tensor（一次性，避免逐段 cat 双份）
                h, w = int(images.shape[1]), int(images.shape[2])
                merged = torch.empty((total_frames, h, w, 3), dtype=torch.float32)
            n = int(images.shape[0])
            merged[offset : offset + n] = images
            offset += n
            del images  # 本段帧已写入，立即释放
            if ctx.progress:
                try:
                    ctx.progress(clip_id, "decoding", (index + 1) / total)
                except Exception:  # noqa: BLE001
                    pass
            # 音频量小（每段 ~5MB），累积后统一合并
            if isinstance(audio, dict) and audio.get("waveform") is not None:
                if merged_audio is None:
                    merged_audio = {"waveform": [], "sample_rate": audio.get("sample_rate", 32000)}
                merged_audio["waveform"].append(audio["waveform"])
                del audio

        if merged is None:
            raise ValueError("创意工作台：没有解码出任何帧")
        if merged_audio is None:
            final_audio = merge_audios([])
        else:
            from .segment_cache import merge_audios as _merge

            final_audio = _merge(
                [
                    {"waveform": w, "sample_rate": merged_audio["sample_rate"]}
                    for w in merged_audio["waveform"]
                ]
            )
        return merged, final_audio
