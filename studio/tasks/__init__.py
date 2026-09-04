"""任务注册表与工厂 —— 反序列化注入的入口。

- TASK_REGISTRY: mode → Task 子类（新增模式 = 新增子类 + 在此登记一行）
- create_task(): 根据 segment.mode 注入对应子类实例（数据 + 运行时上下文）
"""

from __future__ import annotations

from ..payload import ClipPayload
from .base import (
    BaseTask,
    ConditioningResult,
    SamplingConfig,
    SegmentResult,
    TaskContext,
    build_common_conditioning,
    validate_audio_deps,
)
from .t2v import T2VTask
from .i2v import I2VTask
from .fl2v import FL2VTask
from .r2v import R2VTask
from .v2v import V2VTask
from .rv2v import RV2VTask

__all__ = [
    "BaseTask",
    "ConditioningResult",
    "SamplingConfig",
    "SegmentResult",
    "TaskContext",
    "TASK_REGISTRY",
    "create_task",
]

TASK_REGISTRY: dict[str, type[BaseTask]] = {
    cls.mode: cls
    for cls in (T2VTask, I2VTask, FL2VTask, R2VTask, V2VTask, RV2VTask)
}


def create_task(segment: ClipPayload, ctx: TaskContext) -> BaseTask:
    """按 segment.mode 从注册表取出子类并注入数据与上下文。"""
    task_cls = TASK_REGISTRY.get(segment.mode)
    if task_cls is None:
        raise ValueError(f"不支持的生成模式: {segment.mode}")
    return task_cls(segment, ctx)
