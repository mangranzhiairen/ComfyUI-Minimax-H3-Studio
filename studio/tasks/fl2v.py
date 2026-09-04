"""fl2v 首尾帧生视频：首帧/尾帧可只传其一（官方支持只传尾帧），ImageToVideo + first/last。"""

from __future__ import annotations

from .base import BaseTask, ConditioningResult, build_common_conditioning


class FL2VTask(BaseTask):
    mode = "fl2v"

    def validate(self) -> None:
        if self.segment.first_frame is None and self.segment.last_frame is None:
            raise ValueError(f"段 {self.segment.id} (fl2v): 首帧与尾帧至少提供其一")

    def build_conditioning(self) -> ConditioningResult:
        return build_common_conditioning(
            self.ctx,
            self.segment,
            use_reference=False,
            first_frame=self.segment.first_frame.path if self.segment.first_frame else None,
            last_frame=self.segment.last_frame.path if self.segment.last_frame else None,
        )
