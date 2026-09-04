"""MiniMax H3 创意工作台 —— 后端执行核心。

架构：Executor（调度） + Task 子类（各生成模式），参考 studio/tasks/base.py。
"""

from .executor import StudioExecutor, ExecutionResult
from .payload import (
    ClipPayload,
    StudioPayload,
    MediaRef,
    PayloadValidationError,
    align_frame_count,
    frames_for_duration,
    load as load_payload,
)

__all__ = [
    "ClipPayload",
    "StudioExecutor",
    "StudioPayload",
    "ExecutionResult",
    "MediaRef",
    "PayloadValidationError",
    "align_frame_count",
    "frames_for_duration",
    "load_payload",
]
