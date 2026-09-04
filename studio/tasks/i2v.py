"""i2v 图生视频：首帧图锚定，ImageToVideo + first_frame。"""

from __future__ import annotations

from .base import BaseTask, ConditioningResult, build_common_conditioning


class I2VTask(BaseTask):
    mode = "i2v"

    def validate(self) -> None:
        if self.segment.first_frame is None:
            raise ValueError(f"段 {self.segment.id} (i2v): 缺少首帧图")

    def build_conditioning(self) -> ConditioningResult:
        return build_common_conditioning(
            self.ctx,
            self.segment,
            use_reference=False,
            first_frame=self.segment.first_frame.path if self.segment.first_frame else None,
        )
