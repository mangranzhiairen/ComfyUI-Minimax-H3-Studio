// 提示词结构化符号补全：符号表 + 智能编号（纯 TS 模块，无 UI 依赖）。
// 符号来源 MiniMax H3 官方提示词指南：
//  - docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md（T2VA / I2VA / FL2VA / L2VA）
//  - docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md（全参考改写模式）

export interface SnippetGroup {
  key: string;
  title: string;
  icon: string;
}

/** 补全菜单分组（顺序即展示顺序） */
export const SNIPPET_GROUPS: SnippetGroup[] = [
  { key: "shot", title: "镜头", icon: "🎬" },
  { key: "speaker", title: "说话者", icon: "🗣️" },
  { key: "dialogue", title: "对话", icon: "💬" },
  { key: "field", title: "字段", icon: "📝" },
  { key: "task", title: "任务类型", icon: "🏷️" },
  { key: "relation", title: "关系标记", icon: "🔗" },
  { key: "camera", title: "相机运动", icon: "🎥" },
  { key: "label", title: "引用标签", icon: "🔖" },
];

/** 智能编号类别：插入时按编辑器已用编号 max+1 计算 */
export type SmartKind = "shot" | "speaker" | "subject";

export interface SnippetDef {
  /** 所属分组 key（SNIPPET_GROUPS 之一） */
  group: string;
  /** 插入文本；{n}=智能编号占位，{cursor}=光标停留处 */
  token: string;
  /** 菜单显示名 */
  label: string;
  /** 悬停说明（指南语义） */
  hint?: string;
  /** 需要智能编号 */
  smart?: SmartKind;
  /** 搜索关键词（token/label/hint 之外的别名） */
  keywords?: string;
}

export const SNIPPETS: SnippetDef[] = [
  // ---------- 镜头 ----------
  {
    group: "shot",
    token: "[Shot {n}] ",
    label: "镜头 [Shot N]",
    hint: "镜头标记，编号自动递增；时间戳（At MM:SS.mmm）自行补充",
    smart: "shot",
    keywords: "shot 镜头 分镜",
  },
  // ---------- 说话者 ----------
  {
    group: "speaker",
    token: "(S{n})",
    label: "说话者 ID",
    hint: "稳定说话者 ID，编号自动递增；同一说话者跨镜头保持同一 ID",
    smart: "speaker",
    keywords: "speaker 说话者 人声 S1",
  },
  {
    group: "speaker",
    token: "(S1,S2)",
    label: "复合说话者 ID",
    hint: "多人同时发声，如 (S1,S2)；编号按实际使用调整",
    keywords: "compound 复合 同时 多人",
  },
  // ---------- 对话 ----------
  {
    group: "dialogue",
    token: "<d>[English] {cursor}</d>",
    label: "对话块 (English)",
    hint: "台词/歌词写在 <d> 内，原语原词保留；语言标签可改为 [中文] 等",
    keywords: "dialogue 对话 台词 歌词 English",
  },
  {
    group: "dialogue",
    token: "<d>[中文] {cursor}</d>",
    label: "对话块 (中文)",
    hint: "台词/歌词写在 <d> 内，原语原词保留；语言标签可改为 [English] 等",
    keywords: "dialogue 对话 台词 歌词 中文",
  },
  {
    group: "dialogue",
    token: "<scenetrans>",
    label: "跨镜头连接 <scenetrans>",
    hint: "同一句台词跨镜头时在两侧切点标记，并说明音频跨切继续",
    keywords: "scenetrans 跨镜头 切点 连接",
  },
  {
    group: "dialogue",
    token: "<cutoff>",
    label: "截断 <cutoff>",
    hint: "台词被视频结尾截断时标记",
    keywords: "cutoff 截断 结尾",
  },
  // ---------- 字段（三核心 + ref 六段式，去重合并） ----------
  {
    group: "field",
    token: "integrated_multimodal_description: ",
    label: "核心字段 · 多模态描述",
    hint: "沿时间线描述视觉/动作/镜头/台词/画内音",
    keywords: "multimodal 核心 字段 描述",
  },
  {
    group: "field",
    token: "overall_soundscape: ",
    label: "核心字段 · 环境音",
    hint: "全片环境声/物理动作声/非语言人声（1-4 句）",
    keywords: "soundscape 环境音 音效",
  },
  {
    group: "field",
    token: "non_diegetic_music: ",
    label: "核心字段 · 画外音乐",
    hint: "角色听不到、只有观众听到的背景乐（1-3 句）；无则写 N/A",
    keywords: "music 音乐 背景乐 diegetic",
  },
  {
    group: "field",
    token: "subject_definitions: ",
    label: "ref 字段 · 标签定义",
    hint: "逐行定义 <Subject N>/<Picture N>/<Video N>/<Audio N> 及其来源",
    keywords: "subject definitions 标签 定义 ref",
  },
  {
    group: "field",
    token: "summary: ",
    label: "ref 字段 · 摘要",
    hint: "[任务类型] + 目标视频与引用关系的一句话摘要",
    keywords: "summary 摘要 ref",
  },
  {
    group: "field",
    token: "retention_analysis: ",
    label: "ref 字段 · 保留分析",
    hint: "每个引用标签一行，说明保留/转移/复制/弱引用",
    keywords: "retention 保留 分析 ref",
  },
  {
    group: "field",
    token: "detailed_description: ",
    label: "ref 字段 · 逐镜头正文",
    hint: "全参考模式主正文，逐镜头描述（生成任务 350-500 词）",
    keywords: "detailed 正文 描述 ref",
  },
  // ---------- 任务类型（ref summary 前缀，可 + 组合） ----------
  {
    group: "task",
    token: "[reference generation]",
    label: "参考生成",
    hint: "素材仅提供生成引导，不作为具体帧锚点或编辑/续接源",
    keywords: "reference generation 参考生成",
  },
  {
    group: "task",
    token: "[keyframe completion]",
    label: "关键帧补全",
    hint: "图片作为首帧/关键帧/尾帧等具体帧锚点",
    keywords: "keyframe completion 关键帧 补全",
  },
  {
    group: "task",
    token: "[video editing]",
    label: "视频编辑",
    hint: "直接修改已有源视频",
    keywords: "video editing 编辑 改视频",
  },
  {
    group: "task",
    token: "[video continuation]",
    label: "视频续接",
    hint: "从源视频续接/延伸/过渡新内容",
    keywords: "video continuation 续接 延伸",
  },
  {
    group: "task",
    token: "[audio reuse]",
    label: "音频复用",
    hint: "同一音频信号整体或部分复用",
    keywords: "audio reuse 音频复用",
  },
  {
    group: "task",
    token: "[audio reference]",
    label: "音频参考",
    hint: "不复制信号，只参考风格/音色/节奏/内容",
    keywords: "audio reference 音频参考",
  },
  // ---------- 关系标记（可见内容 + 音频两组，去重） ----------
  {
    group: "relation",
    token: "fully_preserved",
    label: "完全保留",
    hint: "引用内容的既定角色完全保留",
    keywords: "fully preserved 完全保留",
  },
  {
    group: "relation",
    token: "partially_preserved",
    label: "部分保留",
    hint: "仍被使用，但部分特征被改变或只保留一部分",
    keywords: "partially preserved 部分保留",
  },
  {
    group: "relation",
    token: "attribute_transfer",
    label: "属性转移",
    hint: "引用特征转移到另一个可辨识的目标主体",
    keywords: "attribute transfer 属性 转移",
  },
  {
    group: "relation",
    token: "weak_reference",
    label: "弱引用",
    hint: "仅保留风格/类别/构图/氛围的宽泛相似",
    keywords: "weak reference 弱引用",
  },
  {
    group: "relation",
    token: "fully_copy",
    label: "音频完全复制",
    hint: "源音频 1:1 复用为目标视频完整音轨",
    keywords: "fully copy 音频 复制",
  },
  {
    group: "relation",
    token: "partially_copy",
    label: "音频部分复制",
    hint: "只复制部分时间线/音轨，或复制后增删替换其他声音",
    keywords: "partially copy 音频 部分",
  },
  {
    group: "relation",
    token: "reference",
    label: "音频参考",
    hint: "不复制信号，只参考音色/节奏/风格/音效质感",
    keywords: "reference 音频 参考",
  },
  // ---------- 相机运动（类型 + 幅度 + 速度） ----------
  ...(
    [
      ["Zoom In", "焦距拉近，机身不动"],
      ["Zoom Out", "焦距拉远，机身不动"],
      ["Push In", "镜头前推"],
      ["Pull Out", "镜头后拉"],
      ["Pan Left", "机身不动，镜头水平左摇"],
      ["Pan Right", "机身不动，镜头水平右摇"],
      ["Truck Left", "机身水平左移"],
      ["Truck Right", "机身水平右移"],
      ["Tilt Up", "机身不动，镜头垂直上摇"],
      ["Tilt Down", "机身不动，镜头垂直下摇"],
      ["Pedestal Up", "整机垂直上移"],
      ["Pedestal Down", "整机垂直下移"],
      ["Arc Shot", "绕主体弧形运动"],
      ["Tracking Shot", "跟随移动主体"],
      ["Static Shot", "机身与镜头保持静止"],
      ["Shake Slightly", "轻微晃动"],
      ["Shake Strongly", "强烈晃动"],
      ["POV", "主体视角"],
      ["Roll Clockwise", "绕镜头轴顺时针滚动"],
      ["Roll Counterclockwise", "绕镜头轴逆时针滚动"],
    ] as const
  ).map(([name, hint]) => ({
    group: "camera",
    token: name,
    label: name,
    hint,
    keywords: `camera 相机 运镜 ${name.toLowerCase()}`,
  })),
  {
    group: "camera",
    token: "with small amplitude",
    label: "幅度 · 小",
    hint: "构图变化范围小（配合运动类型使用）",
    keywords: "amplitude 幅度 small 小",
  },
  {
    group: "camera",
    token: "with large amplitude",
    label: "幅度 · 大",
    hint: "构图变化范围大（配合运动类型使用）",
    keywords: "amplitude 幅度 large 大",
  },
  {
    group: "camera",
    token: "at slow speed",
    label: "速度 · 慢",
    hint: "缓慢移动（配合运动类型使用）",
    keywords: "speed 速度 slow 慢",
  },
  {
    group: "camera",
    token: "at fast speed",
    label: "速度 · 快",
    hint: "快速移动（配合运动类型使用）",
    keywords: "speed 速度 fast 快",
  },
  // ---------- 引用标签（ref 全参考模式；Picture/Video/Audio 已由 @ 素材引用覆盖） ----------
  {
    group: "label",
    token: "<Subject {n}>",
    label: "主体标签 <Subject N>",
    hint: "可复用/可修改的可见内容（人物/场景/服装/风格/动作等），编号自动递增",
    smart: "subject",
    keywords: "subject 主体 标签 引用",
  },
];

// ---------- 智能编号 ----------

function maxIndex(text: string, re: RegExp): number {
  let max = 0;
  re.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) max = Math.max(max, Number(m[1]));
  return max;
}

/** 已用最大 [Shot N] + 1（无则 1） */
export function nextShotIndex(text: string): number {
  return maxIndex(text, /\[Shot\s+(\d+)\]/g) + 1;
}

/** 已用最大 (S N) + 1（复合 ID (S1,S2) 只计首个编号，不影响） */
export function nextSpeakerIndex(text: string): number {
  return maxIndex(text, /\(S(\d+)\)/g) + 1;
}

/** 已用最大 <Subject N> + 1（无则 1） */
export function nextSubjectIndex(text: string): number {
  return maxIndex(text, /<Subject\s+(\d+)>/g) + 1;
}

export function resolveSmartNumber(kind: SmartKind, text: string): number {
  if (kind === "shot") return nextShotIndex(text);
  if (kind === "speaker") return nextSpeakerIndex(text);
  return nextSubjectIndex(text);
}

// ---------- 插入组装 ----------

/** 组装插入文本与光标偏移：{n} 替换为编号（可为 null），{cursor} 标记光标落点 */
export function assembleSnippet(def: SnippetDef, n: number | null): { text: string; cursor: number | null } {
  let text = def.token;
  let cursor: number | null = null;
  const ci = text.indexOf("{cursor}");
  if (ci >= 0) {
    // 光标偏移 = 前缀长度；前缀内的 {n} 需换算成实际编号位数
    const prefix = text.slice(0, ci);
    let len = prefix.length;
    if (n != null) len += String(n).length - "{n}".length;
    cursor = len;
    text = text.replace("{cursor}", "");
  }
  if (n != null) text = text.replace("{n}", String(n));
  return { text, cursor };
}

/** 菜单右侧预览：{n} → N、{cursor} → … */
export function snippetPreview(def: SnippetDef): string {
  return def.token.replace("{cursor}", "…").replace("{n}", "N");
}

// ---------- 过滤 ----------

/** query 过滤（token/label/hint/keywords 小写包含匹配；空 query 返回全量） */
export function filterSnippets(query: string): SnippetDef[] {
  const q = query.trim().toLowerCase();
  if (!q) return SNIPPETS;
  return SNIPPETS.filter(
    (s) =>
      s.token.toLowerCase().includes(q) ||
      s.label.toLowerCase().includes(q) ||
      (s.hint ?? "").toLowerCase().includes(q) ||
      (s.keywords ?? "").toLowerCase().includes(q),
  );
}

// ---------- 动态标签扫描（@ 引用池右侧：复用已创建的 [Shot N] / (S1) / <Subject N>） ----------

/** 动态标签完整匹配（复合说话者 ID (S1,S2) 作为整体一项；排除素材标签 Picture/Video/Audio） */
const DYNAMIC_TAG_RE = /\[Shot\s+\d+\]|\(S\d+(?:,\s*\d+)*\)|<Subject\s+\d+>/g;

/** 标签图标（@ 右栏展示）：镜头 🎬 / 说话者 🗣️ / 主体 🔖 */
export function dynamicTagIcon(tag: string): string {
  if (tag.startsWith("[Shot")) return "🎬";
  if (tag.startsWith("(S")) return "🗣️";
  return "🔖";
}

/** 扫描提示词文本中已存在的动态标签，按出现顺序去重 */
export function scanDynamicTags(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  DYNAMIC_TAG_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = DYNAMIC_TAG_RE.exec(text))) {
    if (!seen.has(m[0])) {
      seen.add(m[0]);
      out.push(m[0]);
    }
  }
  return out;
}
