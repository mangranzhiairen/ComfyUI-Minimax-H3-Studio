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


# ---------- 片段级采样流程（子图） ----------
# 前端在原生子图编辑器里搭采样链，摊平（方案 A：前端自己展开）后随 payload 发来：
#   clips[].pipeline = { id, name, graph: {version, inputs, nodes, output, warnings} }
# 后端只消费摊平图（不认识前端子图定义），执行见 studio/pipeline.py。


@dataclass(frozen=True)
class PipelineNode:
    """平铺图里的一个节点（ComfyUI API 格式）。"""

    class_type: str
    inputs: dict[str, Any]


@dataclass(frozen=True)
class PipelineGraph:
    """摊平后的可执行图。

    - inputs：骨架输入槽（顺序 = 前端槽顺序；运行时按**名字**喂值）
    - nodes：`{节点id: {class_type, inputs}}`，输入值可以是字面量，也可以是
      `[上游节点id, 输出槽]` 连线；`["__studio__", "<槽名>"]` 表示 studio 运行时提供
    - output：`[节点id, 输出槽]`，即子图输出槽接的是谁
    - warnings：前端摊平时发现的软问题（缺输出连线等），执行报错时一并回显
    """

    version: int
    inputs: tuple[dict[str, str], ...]
    nodes: dict[str, PipelineNode]
    output: tuple[str, int] | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ClipPipeline:
    """片段绑定的采样流程（未绑定时 ClipPayload.pipeline 为 None）。"""

    id: str
    name: str
    graph: PipelineGraph | None


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
    # 片段级采样流程（子图）：有则用它的摊平图采样，没有则走内置官方链
    pipeline: ClipPipeline | None = None

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
        pipeline=_parse_pipeline(raw.get("pipeline"), index, clip_id),
    )


def _parse_pipeline(raw: Any, index: int, clip_id: str) -> ClipPipeline | None:
    """解析片段级采样流程（子图）。

    **结构校验**在这里（形状/类型）；**语义校验**（节点是否存在、骨架槽是否合法、
    有没有环）在执行时做（见 studio/pipeline.py）——那需要 ComfyUI 的节点表，
    而本模块要保持"纯函数、可离线单测"。
    """
    where = f"clips[{index}] ({clip_id}).pipeline"
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PayloadValidationError(f"{where}: 必须是对象")

    pid = str(raw.get("id") or "").strip()
    if not pid:
        raise PayloadValidationError(f"{where}: 缺少子图 id")
    name = str(raw.get("name") or "").strip() or pid

    graph_raw = raw.get("graph")
    if graph_raw is None:
        # 前端没带摊平图（旧产物）：契约上允许，执行时会给出可读报错而不是静默降级
        return ClipPipeline(id=pid, name=name, graph=None)
    if not isinstance(graph_raw, dict):
        raise PayloadValidationError(f"{where}.graph: 必须是对象")

    version = graph_raw.get("version")
    if version != 1:
        raise PayloadValidationError(
            f"{where}.graph: 不支持的摊平图版本 {version!r}（当前只支持 1）"
        )

    inputs_raw = graph_raw.get("inputs")
    if not isinstance(inputs_raw, list):
        raise PayloadValidationError(f"{where}.graph.inputs: 必须是数组")
    inputs: list[dict[str, str]] = []
    for slot_index, slot in enumerate(inputs_raw):
        if not isinstance(slot, dict):
            raise PayloadValidationError(f"{where}.graph.inputs[{slot_index}]: 必须是对象")
        inputs.append(
            {"name": str(slot.get("name") or ""), "type": str(slot.get("type") or "")}
        )

    nodes_raw = graph_raw.get("nodes")
    if not isinstance(nodes_raw, dict):
        raise PayloadValidationError(f"{where}.graph.nodes: 必须是对象")
    nodes: dict[str, PipelineNode] = {}
    for node_id, node in nodes_raw.items():
        if not isinstance(node, dict):
            raise PayloadValidationError(f"{where}.graph.nodes[{node_id}]: 必须是对象")
        class_type = str(node.get("class_type") or "").strip()
        if not class_type:
            raise PayloadValidationError(
                f"{where}.graph.nodes[{node_id}]: 缺少 class_type"
            )
        node_inputs = node.get("inputs")
        if node_inputs is not None and not isinstance(node_inputs, dict):
            raise PayloadValidationError(
                f"{where}.graph.nodes[{node_id}].inputs: 必须是对象"
            )
        nodes[str(node_id)] = PipelineNode(
            class_type=class_type, inputs=dict(node_inputs or {})
        )

    output: tuple[str, int] | None = None
    output_raw = graph_raw.get("output")
    if output_raw is not None:
        if not isinstance(output_raw, (list, tuple)) or len(output_raw) != 2:
            raise PayloadValidationError(
                f"{where}.graph.output: 必须是 [节点id, 输出槽] 或 null"
            )
        try:
            output = (str(output_raw[0]), int(output_raw[1]))
        except (TypeError, ValueError) as exc:
            raise PayloadValidationError(
                f"{where}.graph.output: 必须是 [节点id, 输出槽] 或 null"
            ) from exc

    warnings_raw = graph_raw.get("warnings") or []
    if not isinstance(warnings_raw, list):
        raise PayloadValidationError(f"{where}.graph.warnings: 必须是数组")
    warnings = tuple(str(w) for w in warnings_raw)

    return ClipPipeline(
        id=pid,
        name=name,
        graph=PipelineGraph(
            version=int(version),
            inputs=tuple(inputs),
            nodes=nodes,
            output=output,
            warnings=warnings,
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
