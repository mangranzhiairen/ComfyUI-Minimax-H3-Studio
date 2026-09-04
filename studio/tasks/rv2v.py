"""rv2v 参考素材改视频：源视频 <Video 1> + 参考图/音频 → ReferenceToVideo。"""

from __future__ import annotations

from .base import BaseTask, ConditioningResult, build_common_conditioning, validate_audio_deps


class RV2VTask(BaseTask):
    mode = "rv2v"

    def validate(self) -> None:
        if self.segment.source_video is None:
            raise ValueError(f"段 {self.segment.id} (rv2v): 缺少源视频")
        validate_audio_deps(self.ctx, self.segment, needs_audio=True)

    def build_conditioning(self) -> ConditioningResult:
        return build_common_conditioning(
            self.ctx,
            self.segment,
            use_reference=True,
            ref_videos={"ref_video_0": self.segment.source_video.path},
            ref_images={
                f"ref_image_{i}": m.path for i, m in enumerate(self.segment.ref_images)
            },
            ref_audios={
                f"ref_audio_{i}": m.path for i, m in enumerate(self.segment.ref_audios)
            },
        )
