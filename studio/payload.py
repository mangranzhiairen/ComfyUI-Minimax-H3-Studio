"""StudioPayload 数据模型与反序列化校验 —— 前后端数据契约的 Python 端。

对应前端 web/src/types/timeline.ts：
- 前端 store.serialize() 输出 JSON → 本模块 load() 反序列化为 StudioPayload
- 校验失败抛出 PayloadValidationError，由节点在 report 中回显
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ---------- 常量（与前端契约对齐） ----------

SUPPORTED_MODES = ("t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v")
PAYLOAD_VERSION = 1

MAX_REF_IMAGES = 9
MAX_REF_VIDEOS = 3
MAX_REF_AUDIOS = 3

MIN_DURATION_SEC = 1.0
MAX_DURATION_SEC = 30.0

# MiniMax H3 帧网格：17k + 5（124 = 17*7 + 5）
MIN_FRAMES = 5


class PayloadValidationError(ValueError):
    """数据契约校验失败。"""


# ---------- 帧网格 ----------

def align_frame_count(frame_count: int) -> int:
    """向上取整到 MiniMax H3 17k+5 帧网格（5, 22, 39, …）。"""
    n = max(MIN_FRAMES, int(frame_count))
    while n % 17 != 5:
        n += 1
    return n


def frames_for_duration(duration_sec: float, fps: float) -> int:
    """时长（秒）→ 对齐后的采样帧数。"""
    return align_frame_count(round(duration_sec * fps))


# ---------- 数据模型 ----------

@dataclass(frozen=True)
class MediaRef:
    """素材引用（图片/视频/音频通用）。path 为 ComfyUI input 目录相对路径。"""

    path: str
    kind: str  # "image" | "video" | "audio"


@dataclass(frozen=True)
class CanvasConfig:
    fps: int = 24
    width: int = 864
    height: int = 480


@dataclass
class ClipPayload:
    id: str
    mode: str
    prompt: str
    duration_sec: float
    enabled: bool = True
    first_frame: MediaRef | None = None
    last_frame: MediaRef | None = None
    ref_images: list[MediaRef] = field(default_factory=list)
    ref_videos: list[MediaRef] = field(default_factory=list)
    ref_audios: list[MediaRef] = field(default_factory=list)
    source_video: MediaRef | None = None
    # 段间续接：是否把上一段采样 latent 尾部钉入本段（运动/音频连贯，解码后裁掉前缀）
    continuity: bool = False
    # 抽卡级反悔：用户显式指定的历史采样指纹（16 位 hex）。指定后该片段跳过采样，
    # 直接用这份 latent 出片（seed 等采样参数取历史记录，不受当前节点 widget 影响）
    sample_fp: str | None = None

    def frames(self, fps: float) -> int:
        """对齐 17k+5 网格后的采样帧数。"""
        return frames_for_duration(self.duration_sec, fps)


@dataclass
class StudioPayload:
    version: int
    canvas: CanvasConfig
    clips: list[ClipPayload]
    total_duration_sec: float = 0.0

    def __post_init__(self) -> None:
        self.total_duration_sec = round(sum(c.duration_sec for c in self.clips), 3)


# ---------- 反序列化与校验 ----------

def _parse_media(raw: Any, kind: str, field_name: str) -> MediaRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PayloadValidationError(f"{field_name}: 素材必须是对象，收到 {type(raw).__name__}")
    path = str(raw.get("path") or raw.get("name") or "").strip()
    if not path:
        raise PayloadValidationError(f"{field_name}: 缺少素材路径 (path/name)")
    return MediaRef(path=path, kind=kind)


def _parse_media_list(raw: Any, kind: str, field_name: str, limit: int) -> list[MediaRef]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PayloadValidationError(f"{field_name}: 必须是数组，收到 {type(raw).__name__}")
    if len(raw) > limit:
        raise PayloadValidationError(f"{field_name}: 最多 {limit} 个，收到 {len(raw)}")
    out: list[MediaRef] = []
    for i, item in enumerate(raw):
        media = _parse_media(item, kind, f"{field_name}[{i}]")
        if media is not None:
            out.append(media)
    return out


def _parse_clip(raw: Any, index: int) -> ClipPayload:
    if not isinstance(raw, dict):
        raise PayloadValidationError(f"clips[{index}]: 必须是对象")

    clip_id = str(raw.get("id") or f"clip{index}")
    mode = str(raw.get("mode") or "").strip()
    if mode not in SUPPORTED_MODES:
        raise PayloadValidationError(
            f"clips[{index}] ({clip_id}): 不支持的生成模式 '{mode}'，"
            f"可选: {', '.join(SUPPORTED_MODES)}"
        )

    duration = float(raw.get("durationSec", 4.0))
    if not (MIN_DURATION_SEC <= duration <= MAX_DURATION_SEC):
        raise PayloadValidationError(
            f"clips[{index}] ({clip_id}): 时长 {duration}s 超出范围 "
            f"[{MIN_DURATION_SEC}, {MAX_DURATION_SEC}]"
        )

    return ClipPayload(
        id=clip_id,
        mode=mode,
        prompt=str(raw.get("prompt") or ""),
        duration_sec=duration,
        enabled=bool(raw.get("enabled", True)),
        first_frame=_parse_media(raw.get("firstFrame"), "image", f"clips[{index}].firstFrame"),
        last_frame=_parse_media(raw.get("lastFrame"), "image", f"clips[{index}].lastFrame"),
        ref_images=_parse_media_list(
            raw.get("refImages"), "image", f"clips[{index}].refImages", MAX_REF_IMAGES
        ),
        ref_videos=_parse_media_list(
            raw.get("refVideos"), "video", f"clips[{index}].refVideos", MAX_REF_VIDEOS
        ),
        ref_audios=_parse_media_list(
            raw.get("refAudios"), "audio", f"clips[{index}].refAudios", MAX_REF_AUDIOS
        ),
        source_video=_parse_media(raw.get("sourceVideo"), "video", f"clips[{index}].sourceVideo"),
        continuity=bool(raw.get("continuity", False)),
        sample_fp=_parse_fingerprint(
            raw.get("sampleFp"), index, clip_id, field="sampleFp"
        ),
    )


def _parse_fingerprint(raw: Any, index: int, clip_id: str, *, field: str) -> str | None:
    """解析可选指纹（16 位小写 hex），空串视为未指定。"""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if len(text) != 16 or not all(c in "0123456789abcdef" for c in text):
        raise PayloadValidationError(
            f"clips[{index}] ({clip_id}): {field} 必须是 16 位指纹 hash"
        )
    return text


def clip_to_snapshot(clip: ClipPayload) -> dict:
    """片段对象 → 提示词条目快照（纯画面语义）：id/mode/prompt/素材。

    执行态（enabled/continuity）、采样指纹（sampleFp）与规格（时长/画布）不进快照——
    时长/分辨率随采样记录（样本属性），启用 latent 时从样本恢复；历史身份只代表
    "这段画面以什么内容采过"。素材以引用形式保存（prompt 中的 token 位置 ↔ 实际文件）。
    """
    media = lambda m: {"path": m.path, "kind": m.kind}
    return {
        "id": clip.id,
        "mode": clip.mode,
        "prompt": clip.prompt,
        **({"firstFrame": media(clip.first_frame)} if clip.first_frame else {}),
        **({"lastFrame": media(clip.last_frame)} if clip.last_frame else {}),
        **({"refImages": [media(m) for m in clip.ref_images]} if clip.ref_images else {}),
        **({"refVideos": [media(m) for m in clip.ref_videos]} if clip.ref_videos else {}),
        **({"refAudios": [media(m) for m in clip.ref_audios]} if clip.ref_audios else {}),
        **({"sourceVideo": media(clip.source_video)} if clip.source_video else {}),
    }


def _parse_canvas(raw: Any) -> CanvasConfig:
    if not isinstance(raw, dict):
        raise PayloadValidationError("canvas: 必须是对象")
    return CanvasConfig(
        fps=int(raw.get("fps", 24)),
        width=int(raw.get("width", 864)),
        height=int(raw.get("height", 480)),
    )


def load(timeline_data: str | dict) -> StudioPayload:
    """反序列化前端 serialize() 输出的 StudioPayload JSON，并做契约校验。"""
    if isinstance(timeline_data, dict):
        raw = timeline_data
    else:
        text = (timeline_data or "").strip()
        if not text:
            raise PayloadValidationError("timeline_data 为空")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PayloadValidationError(f"timeline_data 不是合法 JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise PayloadValidationError("timeline_data 顶层必须是对象")

    version = int(raw.get("version", 0))
    if version != PAYLOAD_VERSION:
        raise PayloadValidationError(
            f"数据契约版本不匹配：收到 v{version}，期望 v{PAYLOAD_VERSION}（请刷新页面）"
        )

    # audioMode 已移除：非采样参数（解码阶段控制），当前阶段由后端默认处理
    clips_raw = raw.get("clips")
    if not isinstance(clips_raw, list):
        raise PayloadValidationError("clips: 必须是数组")
    if not clips_raw:
        raise PayloadValidationError("clips: 时间线为空，至少需要 1 个片段")

    clips = [_parse_clip(item, i) for i, item in enumerate(clips_raw)]
    return StudioPayload(
        version=version,
        canvas=_parse_canvas(raw.get("canvas")),
        clips=clips,
    )
