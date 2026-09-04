"""v2v 视频转视频：源视频作为 <Video 1> → ReferenceToVideo。"""

from __future__ import annotations

from .base import BaseTask, ConditioningResult, build_common_conditioning, validate_audio_deps


class V2VTask(BaseTask):
    mode = "v2v"

    def validate(self) -> None:
        if self.segment.source_video is None:
            raise ValueError(f"段 {self.segment.id} (v2v): 缺少源视频")
        validate_audio_deps(self.ctx, self.segment, needs_audio=True)

    def build_conditioning(self) -> ConditioningResult:
        return build_common_conditioning(
            self.ctx,
            self.segment,
            use_reference=True,
            ref_videos={"ref_video_0": self.segment.source_video.path},
        )
