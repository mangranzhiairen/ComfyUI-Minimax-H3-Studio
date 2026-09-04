/**
 * 提示词版本比对（git diff 风格）：
 * - 文本 diff 按「token 原子 + 单字符」粒度：<Picture N> 等引用素材 token 整体
 *   参与比较（内部空格不拆开、不会被半删除），其余文本逐字符比较（中文提示词
 *   逐字才能看到最小差异，词级对中文退化成整句红绿没有意义）。
 * - 素材槽差异独立提取：两个条目 prompt 文本可能相同但槽位文件已换（换参考图
 *   也是新内容），纯文本 diff 会误报「无差异」，需另行列出。
 * 纯函数无 DOM/无依赖，可独立单测。
 */
import type { MediaKind, PromptSnapshot } from "@/types/timeline";

/** 原子 token（<Picture 1> 等，引用素材在编辑器里是原子 chip） */
const TOKEN_RE = /<(Picture|Video|Audio)\s+\d+>/g;

/** 文本 → 原子序列：token 整体一个原子，其余逐字符 */
function atomize(text: string): string[] {
  const atoms: string[] = [];
  let last = 0;
  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(text))) {
    for (let i = last; i < m.index; i++) atoms.push(text[i]!);
    atoms.push(m[0]);
    last = m.index + m[0].length;
  }
  for (let i = last; i < text.length; i++) atoms.push(text[i]!);
  return atoms;
}

/** diff 结果段：common（共同）/ del（左侧独有，删除）/ add（右侧独有，新增） */
export type DiffRunKind = "common" | "del" | "add";

export interface DiffRun {
  kind: DiffRunKind;
  text: string;
}

/** 连续同类段合并（渲染时减少 span 数量） */
function mergeRuns(runs: DiffRun[]): DiffRun[] {
  const out: DiffRun[] = [];
  for (const r of runs) {
    const last = out[out.length - 1];
    if (last && last.kind === r.kind) last.text += r.text;
    else out.push({ ...r });
  }
  return out;
}

/** 两个提示词文本的字符级 diff（token 原子化），返回合并后的差异段流 */
export function diffPrompts(oldText: string, newText: string): DiffRun[] {
  const a = atomize(oldText);
  const b = atomize(newText);
  const n = a.length;
  const m = b.length;

  // LCS 长度 DP（原子数几百级别，O(nm) 足够）
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i]![j] = a[i] === b[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!);
    }
  }

  // 回溯 → 差异段（直接生成按序段再合并）
  const runs: DiffRun[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      runs.push({ kind: "common", text: a[i]! });
      i++;
      j++;
    } else if (dp[i + 1]![j]! >= dp[i]![j + 1]!) {
      runs.push({ kind: "del", text: a[i]! });
      i++;
    } else {
      runs.push({ kind: "add", text: b[j]! });
      j++;
    }
  }
  while (i < n) {
    runs.push({ kind: "del", text: a[i]! });
    i++;
  }
  while (j < m) {
    runs.push({ kind: "add", text: b[j]! });
    j++;
  }
  return mergeRuns(runs);
}

// ---------- 素材槽差异 ----------

export interface MediaSlotChange {
  /** 槽位名（首帧图 / 参考图 2 / 源视频 …） */
  slot: string;
  kind: MediaKind;
  /** 旧条目该槽文件（可能为 undefined = 旧版没这素材） */
  old?: { path: string };
  /** 新条目该槽文件（可能为 undefined = 新版移除这素材） */
  new?: { path: string };
}

/** 单个素材槽比较项（key 为快照字段；list 槽位带索引） */
interface SlotSpec {
  key: keyof PromptSnapshot;
  kind: MediaKind;
  label: (i: number) => string;
  /** true = 数组槽（refImages 等） */
  list?: boolean;
}

const SLOT_SPECS: SlotSpec[] = [
  { key: "firstFrame", kind: "image", label: () => "首帧图" },
  { key: "lastFrame", kind: "image", label: () => "尾帧图" },
  { key: "sourceVideo", kind: "video", label: () => "源视频" },
  { key: "refImages", kind: "image", label: (i) => `参考图 ${i + 1}`, list: true },
  { key: "refVideos", kind: "video", label: (i) => `参考视频 ${i + 1}`, list: true },
  { key: "refAudios", kind: "audio", label: (i) => `参考音频 ${i + 1}`, list: true },
];

/** 两个画面语义快照的素材槽差异（prompt 相同但换图时文本 diff 不感知，这里补上） */
export function mediaSlotChanges(oldSnap: PromptSnapshot, newSnap: PromptSnapshot): MediaSlotChange[] {
  const changes: MediaSlotChange[] = [];
  for (const spec of SLOT_SPECS) {
    const oldVal = oldSnap[spec.key];
    const newVal = newSnap[spec.key];
    if (spec.list) {
      const oldList = (oldVal ?? []) as { path: string }[];
      const newList = (newVal ?? []) as { path: string }[];
      const len = Math.max(oldList.length, newList.length);
      for (let i = 0; i < len; i++) {
        const oldPath = oldList[i]?.path;
        const newPath = newList[i]?.path;
        if (oldPath !== newPath) {
          changes.push({
            slot: spec.label(i),
            kind: spec.kind,
            ...(oldPath ? { old: { path: oldPath } } : {}),
            ...(newPath ? { new: { path: newPath } } : {}),
          });
        }
      }
    } else {
      const oldItem = (oldVal ?? undefined) as { path: string } | undefined;
      const newItem = (newVal ?? undefined) as { path: string } | undefined;
      if (oldItem?.path !== newItem?.path) {
        changes.push({
          slot: spec.label(0),
          kind: spec.kind,
          ...(oldItem ? { old: { path: oldItem.path } } : {}),
          ...(newItem ? { new: { path: newItem.path } } : {}),
        });
      }
    }
  }
  return changes;
}

/** 条目级差异摘要：mode 是否变化（条目身份含 mode；时长/画布随样本不在此层） */
export function snapshotMetaDiff(a: PromptSnapshot, b: PromptSnapshot): { modeChanged: boolean } {
  return {
    modeChanged: a.mode !== b.mode,
  };
}
