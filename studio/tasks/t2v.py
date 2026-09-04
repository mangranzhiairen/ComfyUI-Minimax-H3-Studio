"""t2v 文生视频：无素材，直接 ImageToVideo。"""

from __future__ import annotations

from .base import BaseTask, ConditioningResult, build_common_conditioning


class T2VTask(BaseTask):
    mode = "t2v"

    def build_conditioning(self) -> ConditioningResult:
        return build_common_conditioning(
            self.ctx,
            self.segment,
            use_reference=False,
        )
