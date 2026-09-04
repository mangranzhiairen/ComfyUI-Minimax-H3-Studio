"""r2v 参考主体生视频：参考图/视频/音频 → ReferenceToVideo。"""

from __future__ import annotations

from .base import BaseTask, ConditioningResult, build_common_conditioning


class R2VTask(BaseTask):
    mode = "r2v"

    def validate(self) -> None:
        if not (self.segment.ref_images or self.segment.ref_videos or self.segment.ref_audios):
            raise ValueError(f"段 {self.segment.id} (r2v): 至少需要一张参考图或一个参考视频/音频")

    def build_conditioning(self) -> ConditioningResult:
        return build_common_conditioning(
            self.ctx,
            self.segment,
            use_reference=True,
            ref_images={
                f"ref_image_{i}": m.path for i, m in enumerate(self.segment.ref_images)
            },
            ref_videos={
                f"ref_video_{i}": m.path for i, m in enumerate(self.segment.ref_videos)
            },
            ref_audios={
                f"ref_audio_{i}": m.path for i, m in enumerate(self.segment.ref_audios)
            },
        )
