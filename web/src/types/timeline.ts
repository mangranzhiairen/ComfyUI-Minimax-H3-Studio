/**
 * 时间线数据模型 —— 前后端数据契约
 *
 * 前端 Pinia store 中维护的数据，最终通过 serialize() 序列化为
 * StudioPayload 传给 Python 后端。此文件是前后端对齐的唯一依据。
 */

/** 片段生成模式（t2v / i2v / fl2v / r2v / v2v / rv2v 全模式） */
export type ClipMode = "t2v" | "i2v" | "fl2v" | "r2v" | "v2v" | "rv2v";

export const CLIP_MODES: ClipMode[] = ["t2v", "i2v", "fl2v", "r2v", "v2v", "rv2v"];

/** 参考媒体类型 */
export type MediaKind = "image" | "video" | "audio";

/**
 * 引用素材（图片 / 视频 / 音频通用）
 * - name：文件名（预览阶段）
 * - path：ComfyUI input 目录相对路径（集成后由前端上传回填，后端凭此读取）
 * - preview：仅前端预览用（dataURL / 缩略图），不进数据契约
 */
export interface ReferenceMedia {
  name: string;
  kind: MediaKind;
  path?: string;
  preview?: string;
}

/** 提示词编辑器 @ 素材选择器候选（token + 展示信息，UI 层，不进数据契约） */
export interface PromptPickerItem {
  /** 插入的 token（<Picture N> / <Video N> / <Audio N>） */
  token: string;
  /** 选择器显示名（如“首帧图”“参考图 1”） */
  label: string;
  kind: MediaKind;
  /** 对应素材（渲染缩略图；无则显示类型图标） */
  media?: ReferenceMedia;
  /** 缩略图 URL（preview=webp，无图则不显示） */
  thumb?: string;
}

/** 各模式素材数量上限 */
export const REFERENCE_LIMITS = {
  /** r2v / rv2v 参考图上限 */
  images: 9,
  /** r2v 参考视频上限 */
  videos: 3,
  /** r2v / rv2v 参考音频上限 */
  audios: 3,
} as const;

/** 单个时间线片段 */
export interface Clip {
  /** 片段唯一 ID（前端生成，保证稳定引用） */
  id: string;
  /** 生成模式 */
  mode: ClipMode;
  /** 分镜提示词 */
  prompt: string;
  /** 片段时长（秒），最小 1s */
  durationSec: number;
  /** 是否参与生成（选择运行） */
  enabled: boolean;
  /** 段间续接：把上一段采样 latent 尾部钉入本段（运动/音频连贯，解码后裁掉前缀） */
  continuity?: boolean;
  /** 首帧图（i2v / fl2v） */
  firstFrame?: ReferenceMedia;
  /** 尾帧图（fl2v，官方支持只传尾帧） */
  lastFrame?: ReferenceMedia;
  /** 参考图列表（r2v，≤9）。紧凑模型：编号 = 下标 + 1（官方 <Picture N> 即第 N 张），
   *  删除素材后列表自动前移补位，编号始终连续、无空位概念。 */
  refImages?: ReferenceMedia[];
  /** 参考视频列表（r2v，≤3），同紧凑模型 */
  refVideos?: ReferenceMedia[];
  /** 参考音频列表（r2v / rv2v，≤3），同紧凑模型 */
  refAudios?: ReferenceMedia[];
  /** 源视频（v2v / rv2v，自动绑定为 <Video 1>） */
  sourceVideo?: ReferenceMedia;
  /** 片段卡缩略图（仅前端预览用，不上传后端） */
  thumb?: string;
  /** 抽卡级反悔：显式指定的历史采样指纹（16 位 hex）。Queue 时该片段跳过采样，
   *  直接用这份 latent 出片（seed 等取历史记录，不受节点全局 seed 影响）。
   *  缺省 = 自动（同参数同 seed 命中复用，否则重新采样）。 */
  sampleFp?: string;
}

/** 画布配置（与后端 CanvasConfig 对齐） */
export interface CanvasConfig {
  /** 全局帧率 */
  fps: number;
  /** 画布宽 */
  width: number;
  /** 画布高 */
  height: number;
}

/** 后端最终收到的负载（数据契约版本化，便于后端校验与演进） */
export interface StudioPayload {
  version: 1;
  canvas: CanvasConfig;
  clips: ClipPayload[];
  /** 总时长（秒），由各段累加，后端可交叉校验 */
  totalDurationSec: number;
}

/** 发给后端的片段（素材序列化：只保留 path，去掉 preview）。
 *  参考列表紧凑无空位：编号 = 下标 + 1，与官方 <Picture N>（第 N 张）语义对齐。 */
export interface ClipPayload {
  id: string;
  mode: ClipMode;
  prompt: string;
  durationSec: number;
  enabled: boolean;
  /** 段间续接（前端开关，随契约传递） */
  continuity?: boolean;
  firstFrame?: { path: string; kind: MediaKind };
  lastFrame?: { path: string; kind: MediaKind };
  refImages?: { path: string; kind: MediaKind }[];
  refVideos?: { path: string; kind: MediaKind }[];
  refAudios?: { path: string; kind: MediaKind }[];
  sourceVideo?: { path: string; kind: MediaKind };
  /** 抽卡级反悔：显式指定的历史采样指纹（后端据此跳过采样直接用该 latent） */
  sampleFp?: string;
}

/** 时长限制 */
export const DURATION_LIMITS = {
  minSec: 1,
  maxSec: 30,
  /** 拖拽步长（秒） */
  stepSec: 0.5,
} as const;

/** 时间线视图设置（仅前端 UI 状态，不进数据契约） */
export interface TimelineViewState {
  /** 选中片段 id */
  selectedId: string | null;
  /** 缩放：每秒钟的像素宽度 */
  zoom: number;
}

// ---------- 卡片历史（GET /tasks/{id}/history，统一参数模型） ----------
// 历史 = 提示词条目（纯画面语义快照，采样固化）→ 其下采样记录（latent 结果）。
// 执行态（enabled/continuity）、采样工艺（seed/steps/…）不参与条目身份；
// 画布是采样的环境属性（样本 usable 判定），不是历史组织维度。

/** 提示词条目快照（纯画面语义）：历史里一个"我试过的内容"。
 *  不含 enabled/continuity（当前编排）、不含 sampleFp（锁定指针）、不含规格
 *  ——时长/分辨率随采样记录（样本属性，启用 latent 时从样本恢复）；
 *  素材随 prompt 保存（token 槽位 ↔ 实际文件）。 */
export interface PromptSnapshot {
  id: string;
  mode: ClipMode;
  prompt: string;
  firstFrame?: { path: string; kind: MediaKind };
  lastFrame?: { path: string; kind: MediaKind };
  refImages?: { path: string; kind: MediaKind }[];
  refVideos?: { path: string; kind: MediaKind }[];
  refAudios?: { path: string; kind: MediaKind }[];
  sourceVideo?: { path: string; kind: MediaKind };
}

/** 历史采样记录（后端 version_samples 行，latent 私属于片段）
 *  携带「当时怎么拍的规格」：canvas（分辨率）+ durationSec（时长），锁定出片时
 *  前端据此恢复任务分辨率与片段时长（不匹配分别提示覆盖）；seed 作抽卡标识。
 *  采样工艺（steps/sampler/cfg…）不记录——无法从 latent 恢复。 */
export interface VersionSample {
  /** 所属提示词条目（clip_versions.id，采样固化时归属） */
  versionId: number;
  contentFp: string;
  /** 采样时的画布（"{width}x{height}@{fps}"）。与当前画布不一致 = 需切换分辨率才能锁定 */
  canvas?: string;
  /** 采样时的片段时长（秒）——锁定出片时据此恢复片段时长 */
  durationSec: number;
  sampleFp: string;
  /** 抽卡 seed（抽卡标识；工艺参数不再记录） */
  seed: number;
  continuity: boolean;
  frames: number;
  sampleLen: number;
  createdAt: number;
  /** latent 文件是否仍存在 */
  exists: boolean;
  /** 采样预览动画 WebP 的 /view URL（与 latent 缓存绑定，不存在为空串） */
  previewUrl?: string;
}

/** 提示词条目（clip_versions 行，纯 Model——采样固化，无"当前"标记） */
export interface ClipVersion {
  versionId: number;
  contentFp: string;
  /** 该条目最近一次采样的画布（参考信息，样本按各自 canvas 判 usable） */
  canvas?: string;
  /** 画面语义快照（恢复/启用采样回填编辑面板用） */
  snapshot: PromptSnapshot;
  createdAt: number;
}

/** 单张卡片的历史（按 clip_id 身份，纯 Model） */
export interface ClipHistory {
  /** 提示词条目（采样固化，按时间倒序） */
  versions: ClipVersion[];
  /** 全部采样记录（按时间倒序，含参数） */
  samples: VersionSample[];
}
