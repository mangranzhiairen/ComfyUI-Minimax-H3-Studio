"""MiniMax H3 创意工作台节点 —— ComfyUI 入口。

- 时间线数据存 SQLite 任务库，工作流 json 只存 taskId（由前端 MINIMAX_H3_STUDIO_UI widget 传回）
- StudioExecutor 按 taskId 从 DB 读任务 → 工厂注入 Task 子类 → 采样/解码
- report 输出回显每段执行结果
"""

from __future__ import annotations

import json
import logging

from ..studio.executor import StudioExecutor
from ..studio.http_routes import register_routes
from ..studio.payload import PayloadValidationError
from ..studio.tasks import SamplingConfig

_CATEGORY = "MiniMaxH3"

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio")


class MiniMaxH3StudioConsole:
    """创意工作台节点：时间线数据 → 反序列化 → Task 子类注入 → 执行。"""

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers

        return {
            "required": {
                # 时间线数据载体（普通 STRING widget）：前端自动填充 JSON 并隐藏（参考项目同款）
                "timeline_data": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "tooltip": "Internal — 时间线序列化数据（由前端面板自动填充）",
                    },
                ),
                "seed": (
                    "INT",
                    {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True},
                ),
                "steps": ("INT", {"default": 25, "min": 1, "max": 200}),
                "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 30.0, "step": 0.01}),
                "sampler": (comfy.samplers.KSampler.SAMPLERS, {"default": "res_multistep"}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "simple"}),
                "shift_video": ("FLOAT", {"default": 12.0, "min": 0.01, "max": 100.0, "step": 0.01}),
                "shift_audio": ("FLOAT", {"default": 3.0, "min": 0.01, "max": 100.0, "step": 0.01}),
                # 采样完成（解码前）卸载采样模型并清显存缓存：解码显存更宽松，
                # 适合显存紧张环境；UNET/CLIP 后续需重新加载
                "unload_models_after": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": "任务完成后（采样结束进入解码前）卸载采样模型（UNET/CLIP）并清除显存缓存；"
                        "VAE 保留避免重载。显存紧张时建议开启，解码更稳定",
                    },
                ),
                # 时间线面板：放原生参数之后（widget 按 required 顺序渲染，面板显示在底部）
                "studio_console_ui": ("MINIMAX_H3_STUDIO_UI", {}),
            },
            "optional": {
                "model": ("MODEL", {"tooltip": "MiniMax H3 UNET（真实采样时接入）"}),
                "video_vae": ("VAE", {"tooltip": "MiniMax H3 video VAE（真实采样时接入）"}),
                "audio_vae": ("VAE", {"tooltip": "MiniMax H3 audio VAE（r2v/v2v/rv2v 需要）"}),
                "clip": ("CLIP", {"tooltip": "CLIPLoader type=minimax（真实采样时接入）"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING", "IMAGE", "AUDIO", "FLOAT", "INT")
    RETURN_NAMES = ("report", "images", "audio", "fps", "frame_count")
    OUTPUT_IS_LIST = (False, False, False, False, False)
    FUNCTION = "execute"
    CATEGORY = _CATEGORY
    DESCRIPTION = (
        "MiniMax H3 创意工作台：时间线数据反序列化 → Task 子类注入 → "
        "官方 MiniMax H3 采样（SigmaShift + KSampler）→ AV 解码输出成片。"
    )

    def execute(
        self,
        timeline_data="",
        seed=0,
        steps=25,
        cfg=1.0,
        sampler="res_multistep",
        scheduler="simple",
        shift_video=12.0,
        shift_audio=3.0,
        unload_models_after=False,
        model=None,
        video_vae=None,
        audio_vae=None,
        clip=None,
        unique_id=None,
        **kwargs,
    ):
        del kwargs
        # 兜底：确保 HTTP 路由已注册（若 __init__ 阶段 PromptServer 未就绪，这里补上）
        register_routes()

        # 真实采样必须接入模型依赖
        missing = [
            name
            for name, value in (
                ("model", model),
                ("video_vae", video_vae),
                ("clip", clip),
            )
            if value is None
        ]
        if missing:
            return (
                f"执行失败: 真实采样需要接入 {', '.join(missing)}"
                "（model / video_vae / clip）",
                _placeholder_image(480, 864),
                _placeholder_audio(),
                24.0,
                0,
            )

        sampling = SamplingConfig(
            seed=int(seed),
            steps=int(steps),
            cfg=float(cfg),
            sampler=str(sampler),
            scheduler=str(scheduler),
            shift_video=float(shift_video),
            shift_audio=float(shift_audio),
        )
        deps = {
            "model": model,
            "video_vae": video_vae,
            "audio_vae": audio_vae,
            "clip": clip,
        }
        executor = StudioExecutor(
            sampling=sampling,
            deps=deps,
            node_id=str(unique_id or "default"),
            unload_models_after=bool(unload_models_after),
        )

        # timeline_data 由前端面板自动填充（普通 STRING widget 序列化，无需手动）
        if isinstance(timeline_data, (dict, list)):
            timeline_data = json.dumps(timeline_data, ensure_ascii=False)
        else:
            timeline_data = str(timeline_data or "")

        try:
            result = executor.run(timeline_data)
            report = result.report
            fps = float(result.payload.canvas.fps) if result.payload else 24.0
            frame_count = int(result.images.shape[0]) if result.images is not None else 0
            # 合并输出：全部段帧拼接的单条 IMAGE + 拼接 AUDIO
            images = result.images if result.images is not None else _placeholder_image(480, 864)
            audio = result.audio if result.audio is not None else _placeholder_audio()
        except PayloadValidationError as exc:
            log.error("创意工作台数据校验失败: %s", exc)  # 后端控制台可见，便于排查
            report = f"数据校验失败: {exc}"
            images = _placeholder_image(480, 864)
            audio = _placeholder_audio()
            fps, frame_count = 24.0, 0
        except Exception as exc:  # noqa: BLE001 节点层兜底，错误回显到 report
            log.error("创意工作台执行失败: %s: %s", type(exc).__name__, exc)  # 后端控制台可见
            report = f"执行失败: {type(exc).__name__}: {exc}"
            images = _placeholder_image(480, 864)
            audio = _placeholder_audio()
            fps, frame_count = 24.0, 0

        return report, images, audio, fps, frame_count


def _placeholder_image(height: int, width: int):
    """验证模式占位：1 帧灰色图（不冒充真实生成）。"""
    import torch

    return torch.full((1, max(1, int(height)), max(1, int(width)), 3), 0.5, dtype=torch.float32)


def _placeholder_audio() -> dict:
    """验证模式占位：静音 AUDIO。"""
    import torch

    return {
        "waveform": torch.zeros(1, 1, 1, dtype=torch.float32),
        "sample_rate": 32000,
    }
