/**
 * 片段级「采样流程」子图（片段卡片 ⊞ 按钮）。
 *
 * 产品形态（见 doc/custom-sampling-plan.md）：**每个片段卡片绑定自己的子图**，
 * 用 ComfyUI 原生子图编辑器编辑；子图**不进工作流画布**——不在画布上放实例节点，
 * 定义随卡片草稿（时间线 → DB）走，工作流 json 里只有 {taskId}，因此画布与工作流
 * 里都看不到它。
 *
 * 为什么不能只丢给前端存：`LGraph.asSerialisable()` 只序列化**被实例节点引用**的
 * 子图（`findUsedSubgraphIds`），没有实例的孤立定义在保存工作流时会被丢弃。所以我们
 * 自己把定义存进片段草稿，并在打开时按需重新注册回 `rootGraph.subgraphs`。
 *
 * 依赖 ComfyUI 前端（comfyui_frontend_package，实测 1.51.9）的原生子图能力：
 * - `LGraph.createSubgraph(exportedSubgraph)`  注册子图定义（litegraph/src/LGraph.ts）
 * - `LGraphCanvas.openSubgraph(subgraph)`      进入子图编辑（节点头部「打开子图」同款）
 * - `Subgraph.asSerialisable()`                把编辑结果取回（ExportedSubgraph）
 *
 * ⚠️ 脆弱点：手工拼的 `ExportedSubgraph` 形态对齐当前前端内部实现
 * （`LGraph.convertToSubgraph` 里的 `satisfies ExportedSubgraph` + `Subgraph.asSerialisable`），
 * 属于**前端内部格式**，前端升级可能变化；因此全部走特性检测 + 分步报错。
 *
 * 注：不 `import { app } from "../../scripts/app.js"`——相对路径 import 在
 * `web/src/utils/` 这层目录下，dev（vite 要真去解析 web/scripts/）与 lib 构建
 * （rollup 对相对 external 的重写）都有额外风险；`window.comfyAPI.app.app` 是官方
 * 文档给出的等价访问方式，且浏览器独立预览下只是拿不到（返回可读错误）。
 */
import type {
  PipelineLibraryEntry,
  PipelineGraph,
  PipelineGraphInput,
  PipelineGraphNode,
} from "@/types/timeline";

/** litegraph/src/constants.ts：SUBGRAPH_INPUT_ID / SUBGRAPH_OUTPUT_ID */
const SUBGRAPH_INPUT_ID = -10;
const SUBGRAPH_OUTPUT_ID = -20;
/** LGraph.serialisedSchemaVersion（当前前端 schema 版本为 1） */
const LGRAPH_SCHEMA_VERSION = 1;
/** 骨架输入/输出节点尺寸（与前端 convertToSubgraph 一致） */
const IO_BOUNDING: [number, number, number, number] = [0, 0, 75, 100];
/** 输出 IO 节点错开摆放（留出中间搭链的空间），避免与输入 IO 节点重叠 */
const IO_OUTPUT_BOUNDING: [number, number, number, number] = [760, 0, 75, 100];
/** 编辑内容回写轮询间隔（只在内容有变化时才回写） */
const SYNC_INTERVAL_MS = 1000;

/**
 * 骨架槽：**进 = 条件 / latent / noise / model / sampler / scheduler，出 = latent**。
 * studio 执行时按**名字**喂这些运行时值（摊平图里写成 `[__studio__, "<槽名>"]`），
 * 顺序只影响骨架摆放，不影响绑定。
 */
const SKELETON_INPUTS: { name: string; type: string }[] = [
  { name: "conditioning", type: "CONDITIONING" },
  { name: "latent", type: "LATENT" },
  { name: "noise", type: "NOISE" },
  { name: "model", type: "MODEL" },
  { name: "sampler", type: "SAMPLER" },
  // 调度器以「sigma 序列」形态提供（对齐 SamplerCustomAdvanced.sigmas 的 SIGMAS 槽）
  { name: "scheduler", type: "SIGMAS" },
];
const SKELETON_OUTPUTS: { name: string; type: string }[] = [
  { name: "latent", type: "LATENT" },
];

/** 摊平图里代表「studio 运行时提供」的伪节点 id（[__studio__, 槽下标]） */
export const STUDIO_INPUT_NODE = "__studio__";

interface IoNodeLike {
  arrange?: () => void;
  boundingRect?: unknown;
}

interface SubgraphLike {
  id: string;
  name: string;
  asSerialisable?: () => unknown;
  /** 已注册的骨架槽（用于回显数量自检 / 补齐缺失槽） */
  inputs?: { name?: string; type?: string }[];
  outputs?: { name?: string; type?: string }[];
  /** 内部节点（空子图 = 0，前端首次进入不会自动 fit） */
  nodes?: unknown[];
  /** IO 节点（SubgraphIONodeBase：arrange / boundingRect） */
  inputNode?: IoNodeLike;
  outputNode?: IoNodeLike;
  /** LGraph.addInput / addOutput：补齐骨架槽用 */
  addInput?: (name: string, type: string) => unknown;
  addOutput?: (name: string, type: string) => unknown;
  /**
   * Subgraph.configure：**内部节点/连线只有走这里才会被建出来**。
   * `createSubgraph()` 只 `new Subgraph()`（构造函数仅处理 IO 槽），官方 convertToSubgraph
   * 也是 `createSubgraph(data)` 之后紧跟 `subgraph.configure(data)`。
   */
  configure?: (data: unknown, keepOld?: boolean) => unknown;
  /** 所属根图（判断是否已切走工作流） */
  rootGraph?: unknown;
}

interface GraphLike {
  subgraphs?: Map<string, SubgraphLike>;
  createSubgraph?: (data: unknown) => SubgraphLike;
}

interface DragAndScaleLike {
  scale?: number;
  offset?: number[];
  element?: HTMLCanvasElement;
  onChanged?: (scale: number, offset: number[]) => void;
}

interface CanvasLike {
  openSubgraph?: (subgraph: SubgraphLike, fromNode?: unknown) => void;
  /** 画布当前显示的图（在子图里时 = 该 Subgraph） */
  graph?: { id?: string };
  canvas?: HTMLCanvasElement;
  ds?: DragAndScaleLike;
  setDirty?: (...args: unknown[]) => void;
}

/** 运行时取 ComfyUI app 单例（window.comfyAPI.app.app，兼容 window.app） */
function getComfyApp(): { graph?: GraphLike; canvas?: CanvasLike } | undefined {
  const w = window as unknown as {
    comfyAPI?: { app?: { app?: { graph?: GraphLike; canvas?: CanvasLike } } };
    app?: { graph?: GraphLike; canvas?: CanvasLike };
  };
  return w.comfyAPI?.app?.app ?? w.app;
}

/**
 * 补齐缺失的骨架槽（按名字比对）。
 *
 * 只在子图**还没有任何节点**时做：既能给「旧版建的空骨架」补上后来新增的输入，
 * 又不会在用户已经搭好链、故意删掉某个槽之后再把它塞回去。
 *
 * @returns 本次补上的槽名（用于回显）
 */
function ensureSkeletonSlots(subgraph: SubgraphLike): string[] {
  if ((subgraph.nodes?.length ?? 0) > 0) return [];
  const added: string[] = [];
  const names = (slots?: { name?: string }[]) =>
    new Set((slots ?? []).map((s) => s?.name).filter((n): n is string => !!n));

  const hasInput = names(subgraph.inputs);
  for (const slot of SKELETON_INPUTS) {
    if (hasInput.has(slot.name)) continue;
    if (typeof subgraph.addInput !== "function") break;
    subgraph.addInput(slot.name, slot.type);
    added.push(slot.name);
  }
  const hasOutput = names(subgraph.outputs);
  for (const slot of SKELETON_OUTPUTS) {
    if (hasOutput.has(slot.name)) continue;
    if (typeof subgraph.addOutput !== "function") break;
    subgraph.addOutput(slot.name, slot.type);
    added.push(`输出 ${slot.name}`);
  }
  return added;
}

/** 错误 → 可读文本 */
function detail(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** boundingRect → [x, y, w, h]（兼容 Rectangle 实例 / 数组 / pos+size 三种形态） */
function rectOf(node: IoNodeLike | undefined): [number, number, number, number] | null {
  const r = node?.boundingRect as
    | number[]
    | {
        export?: () => number[];
        x?: number;
        y?: number;
        width?: number;
        height?: number;
        pos?: number[];
        size?: number[];
      }
    | undefined;
  if (!r) return null;
  if (Array.isArray(r)) return [r[0], r[1], r[2], r[3]];
  if (typeof r.export === "function") {
    const e = r.export();
    if (Array.isArray(e) && e.length >= 4) return [e[0], e[1], e[2], e[3]];
  }
  if (typeof r.x === "number" && typeof r.width === "number") {
    return [r.x, r.y ?? 0, r.width, r.height ?? 0];
  }
  if (Array.isArray(r.pos) && Array.isArray(r.size)) {
    return [r.pos[0], r.pos[1], r.size[0], r.size[1]];
  }
  return null;
}

/**
 * 把骨架 IO 节点拉进视野。
 *
 * 必要性：空子图内部没有任何节点，前端进入子图时的"首次自动 fit"会因
 * `graph.nodes` 为空而直接返回（subgraphNavigationStore.restoreViewport →
 * litegraphService.fitView），于是画布沿用**根图**的平移/缩放 —— 骨架 IO 节点在
 * 图坐标原点附近，通常正好在视野外，看起来就是"子图里什么都没有"。
 *
 * 这里只做「保留缩放、把 IO 区域居中」；空子图额外把缩放夹到可读区间。
 * 已填内容的子图不用管：前端的 fit 会在下一帧按节点重新取景（缓存过视口的
 * 子图也会恢复用户自己的视角），本函数不会与之冲突。
 */
function focusSubgraphIo(canvas: CanvasLike, subgraph: SubgraphLike): void {
  const ds = canvas.ds;
  const el = ds?.element ?? canvas.canvas;
  if (!ds || !ds.offset || !el) return;

  const rects = [rectOf(subgraph.inputNode), rectOf(subgraph.outputNode)].filter(
    (r): r is [number, number, number, number] => !!r,
  );
  if (!rects.length) return;

  const x0 = Math.min(...rects.map((r) => r[0]));
  const y0 = Math.min(...rects.map((r) => r[1]));
  const x1 = Math.max(...rects.map((r) => r[0] + r[2]));
  const y1 = Math.max(...rects.map((r) => r[1] + r[3]));

  const empty = (subgraph.nodes?.length ?? 0) === 0;
  let scale = ds.scale && ds.scale > 0 ? ds.scale : 1;
  if (empty) scale = Math.min(1, Math.max(0.5, scale));
  const dpr = window.devicePixelRatio || 1;
  const cw = el.width / dpr;
  const ch = el.height / dpr;

  ds.scale = scale;
  ds.offset[0] = cw / 2 / scale - (x0 + x1) / 2;
  ds.offset[1] = ch / 2 / scale - (y0 + y1) / 2;
  try {
    ds.onChanged?.(scale, ds.offset);
  } catch {
    /* 缩放同步钩子失败不影响取景 */
  }
  canvas.setDirty?.(true, true);
}

/**
 * 子图 id 必须是 UUID（前端 `zSubgraphId = z.string().uuid()`，URL hash 定位要用）。
 * `crypto.randomUUID` 只在安全上下文（localhost / https）可用，局域网 http 打开
 * ComfyUI 时不存在，因此用 getRandomValues 兜底手搓 v4。
 */
function newSubgraphId(): string {
  const c = crypto as Crypto & { randomUUID?: () => string };
  if (typeof c.randomUUID === "function") return c.randomUUID();
  const b = new Uint8Array(16);
  c.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * 骨架子图定义（ExportedSubgraph）：无内部节点/连线，但**预留好输入输出槽**
 * —— 槽位形态对齐前端 `Subgraph.asSerialisable()`（inputs/outputs 里的
 * `{id, name, type, linkIds, pos}`；IO 节点本体只有 `{id, bounding}`）。
 */
function buildSkeletonSubgraphDef(id: string, name: string): Record<string, unknown> {
  return {
    id,
    name,
    inputNode: { id: SUBGRAPH_INPUT_ID, bounding: [...IO_BOUNDING] },
    outputNode: { id: SUBGRAPH_OUTPUT_ID, bounding: [...IO_OUTPUT_BOUNDING] },
    inputs: SKELETON_INPUTS.map((slot, i) => ({
      id: newSubgraphId(),
      name: slot.name,
      type: slot.type,
      linkIds: [],
      // 绝对坐标（与 IO 节点 bounding 同一坐标系）；打开时 arrange() 会重排
      pos: [IO_BOUNDING[0] + IO_BOUNDING[2] - 24, IO_BOUNDING[1] + 24 + i * 20],
    })),
    outputs: SKELETON_OUTPUTS.map((slot, i) => ({
      id: newSubgraphId(),
      name: slot.name,
      type: slot.type,
      linkIds: [],
      pos: [IO_OUTPUT_BOUNDING[0] + IO_OUTPUT_BOUNDING[2] - 24, IO_OUTPUT_BOUNDING[1] + 24 + i * 20],
    })),
    widgets: [],
    version: LGRAPH_SCHEMA_VERSION,
    revision: 0,
    config: {},
    extra: {},
    state: { lastNodeId: 0, lastLinkId: 0, lastGroupId: 0, lastRerouteId: 0 },
    links: [],
    nodes: [],
    reroutes: [],
    groups: [],
  };
}

/**
 * 子图定义必须是**可结构化克隆的纯数据**。
 *
 * `LGraph.createSubgraph(data)` 内部 `new Subgraph()` 会 `structuredClone(data)`；
 * 而卡片草稿里的 `pipeline` 住在 Pinia store 里，读出来是 **Vue 响应式 Proxy**，
 * 传进 structuredClone 会抛 `DataCloneError: #<Object> could not be cloned`
 * （典型症状：第一次点能建，之后每次点都失败）。
 * 定义本身就是纯 JSON（ExportedSubgraph），所以统一先做一次 JSON 深拷贝去掉代理，
 * 并强制 id/name 与卡片绑定的一致。
 */
function toPlainSubgraphDef(
  def: unknown,
  id: string,
  name: string,
): Record<string, unknown> | null {
  if (!def || typeof def !== "object") return null;
  try {
    const plain = JSON.parse(JSON.stringify(def)) as Record<string, unknown>;
    plain.id = id;
    plain.name = name;
    return plain;
  } catch (err) {
    console.error("[studio] 采样流程子图定义不是可序列化的纯 JSON", err);
    return null;
  }
}

/** 新建一份属于某片段的采样流程（空白骨架槽：6 进 / 1 出） */
export function newClipPipeline(label: string): PipelineLibraryEntry {
  const id = newSubgraphId();
  const name = `采样流程 · ${label}`;
  return { id, name, def: buildSkeletonSubgraphDef(id, name) };
}

/**
 * 把定义灌进已注册的子图对象。
 *
 * ⚠️ **必须**在 `createSubgraph()` 之后调用：`createSubgraph` 只 `new Subgraph(...)`，
 * 而构造函数只处理 IO 槽（`_configureBase` + `_configureSubgraph`），**内部节点与连线
 * 是在 `configure()`（LGraph.configure）里才被建出来的**。少了这一步，子图永远只有骨架、
 * 存下来的节点全部丢失（典型症状：草稿 def 里节点齐全，打开却是空子图）。
 * 官方 convertToSubgraph 同样是 `createSubgraph(data)` 紧跟 `subgraph.configure(data)`。
 */
function configureRegisteredSubgraph(subgraph: SubgraphLike, def: unknown): void {
  if (typeof subgraph.configure !== "function") {
    console.warn("[studio] 子图不支持 configure：内部节点无法恢复");
    return;
  }
  subgraph.configure(def);
  // 与官方 convertToSubgraph 一致：手工补一次配置回调（子图不在根图的 configure 流程里，
  // 这些钩子不会被根图自动触发）
  const nodes = (subgraph.nodes ?? []) as {
    onGraphConfigured?: () => void;
    onAfterGraphConfigured?: () => void;
  }[];
  for (const node of nodes) node.onGraphConfigured?.();
  for (const node of nodes) node.onAfterGraphConfigured?.();
}

/**
 * 复制一份采样流程给新卡片：**必须换新 id/name**（子图 id 是卡片身份的一部分，
 * 沿用会让两张卡片共享同一份定义）。内容从源流程继续（复制卡片 = 连流程一起复制）。
 */
export function cloneClipPipeline(src: PipelineLibraryEntry, label: string): PipelineLibraryEntry {
  const id = newSubgraphId();
  const name = `采样流程 · ${label}`;
  const def = toPlainSubgraphDef(src.def, id, name) ?? buildSkeletonSubgraphDef(id, name);
  return { id, name, def };
}

export interface PipelineOpenResult {
  ok: boolean;
  message: string;
  /** 本次是否新建了子图定义（false = 复用已注册的） */
  created: boolean;
}

/**
 * 打开某片段的采样流程子图（定义未注册则先按 `pipeline.def` 注册）。
 *
 * `onEdited` 在编辑器内容变化时被回调（离开子图后补一次），调用方据此把定义写回
 * 片段草稿；不传则只打开、不回写。
 *
 * 不抛异常——返回 `{ok, message}` 供 UI 直接回显。
 */
export function openClipPipeline(
  pipeline: PipelineLibraryEntry,
  onEdited?: (def: unknown) => void,
): PipelineOpenResult {
  const comfy = getComfyApp();
  const graph = comfy?.graph;
  const canvas = comfy?.canvas;

  if (!graph || typeof graph.createSubgraph !== "function") {
    return {
      ok: false,
      created: false,
      message:
        "当前 ComfyUI 前端不支持子图 API（graph.createSubgraph 不存在），需升级 ComfyUI 前端",
    };
  }
  if (!canvas || typeof canvas.openSubgraph !== "function") {
    return {
      ok: false,
      created: false,
      message: "当前画布不支持子图导航（canvas.openSubgraph 不存在），需升级 ComfyUI 前端",
    };
  }

  // 1) 确保定义已注册（切工作流 / 刷新后内存里就没有了，按草稿里的 def 重建）
  let subgraph = graph.subgraphs?.get(pipeline.id);
  let created = false;
  let nodeCount = 0;
  if (!subgraph) {
    try {
      // 必须是纯数据（草稿里的 def 是响应式代理，直接传 structuredClone 会抛）
      const def =
        toPlainSubgraphDef(pipeline.def, pipeline.id, pipeline.name) ??
        buildSkeletonSubgraphDef(pipeline.id, pipeline.name);
      subgraph = graph.createSubgraph(def);
      // ★ 关键：createSubgraph 只构造，内部节点/连线要靠 configure 才建出来
      configureRegisteredSubgraph(subgraph, def);
      created = true;
    } catch (err) {
      console.error("[studio] 注册采样流程子图定义失败", err);
      return { ok: false, created: false, message: `注册子图定义失败：${detail(err)}` };
    }
  }
  if (!subgraph) {
    return { ok: false, created, message: "子图定义注册失败（createSubgraph 未返回子图）" };
  }
  nodeCount = subgraph.nodes?.length ?? 0;

  // 空骨架补齐后来新增的槽（旧版建的空子图也能升上来；已有节点的子图不动）
  let addedSlots: string[] = [];
  try {
    addedSlots = ensureSkeletonSlots(subgraph);
  } catch (err) {
    console.warn("[studio] 骨架槽补齐失败（不影响使用）", err);
  }

  // IO 槽按实际数量重排位置/尺寸（内部 API，拿不到就跳过，不影响功能）
  try {
    subgraph.inputNode?.arrange?.();
    subgraph.outputNode?.arrange?.();
  } catch (err) {
    console.warn("[studio] 子图 IO 槽重排失败（不影响使用）", err);
  }

  // 2) 打开原生子图编辑器（画布不放实例节点 → 工作流/画布都看不到它）
  try {
    canvas.openSubgraph(subgraph);
    // 空子图不会被前端自动取景（见 focusSubgraphIo 注释），这里主动把骨架 IO 拉进视野
    focusSubgraphIo(canvas, subgraph);
    canvas.setDirty?.(true, true);
  } catch (err) {
    console.error("[studio] 打开子图编辑器失败", err);
    return { ok: false, created, message: `打开子图编辑器失败：${detail(err)}` };
  }

  if (onEdited) startPipelineSync(pipeline.id, onEdited, subgraph);
  // 自检：把实际注册到的骨架槽 / 内部节点数回显出来（便于一眼看出恢复是否生效）
  const inCount = subgraph.inputs?.length ?? 0;
  const outCount = subgraph.outputs?.length ?? 0;
  const slotText = `${inCount} 进 / ${outCount} 出`;
  const nodeText = nodeCount ? `，${nodeCount} 个节点` : "";
  const addedText = addedSlots.length ? `，补上 ${addedSlots.join("、")}` : "";
  return {
    ok: true,
    created,
    message: created
      ? `已新建并打开该片段的采样流程：${pipeline.name}（骨架 ${slotText}${nodeText}）`
      : `已打开该片段的采样流程：${pipeline.name}（骨架 ${slotText}${addedText}）`,
  };
}

// ---------- 编辑内容回写（子图 → 片段草稿） ----------
// 原生编辑器里的改动不经过我们的 store，因此按固定间隔取一次定义；只在内容真的变了
// 才回写（拖动节点会持续变化 → 交给时间线既有的防抖保存）。离开子图/切走工作流时补写
// 最后一次并停止；卡片卸载（切 tab 销毁节点）走 flushClipPipelineSync()。

let syncTimer: number | undefined;
let syncId: string | null = null;
let syncHandler: ((def: unknown) => void) | null = null;
/**
 * 直接持有子图对象：切工作流后 `rootGraph.subgraphs` 会被重建/清空，靠 id 查表就拿不到
 * 已经编辑好的内容了；持有引用才能在切走前把最后一次改动取出来。
 */
let syncSubgraph: SubgraphLike | null = null;
let lastJson = "";

function stopPipelineSync(): void {
  if (syncTimer !== undefined) window.clearInterval(syncTimer);
  syncTimer = undefined;
  syncId = null;
  syncHandler = null;
  syncSubgraph = null;
}

/** 取当前子图定义 + JSON（不可用返回 null） */
function snapshotSubgraph(): { def: unknown; json: string } | null {
  const subgraph = syncSubgraph;
  if (!subgraph || typeof subgraph.asSerialisable !== "function") return null;
  try {
    const def = subgraph.asSerialisable();
    return { def, json: JSON.stringify(def) };
  } catch (err) {
    console.error("[studio] 读取采样流程子图内容失败", err);
    return null;
  }
}

/** 有变化就把定义回写卡片草稿（返回是否写了） */
function flushPipelineSync(): boolean {
  const handler = syncHandler;
  const snap = snapshotSubgraph();
  if (!handler || !snap) return false;
  if (snap.json === lastJson) return false;
  lastJson = snap.json;
  handler(snap.def);
  return true;
}

function pushPipelineSync(): void {
  if (!syncId || !syncHandler) {
    stopPipelineSync();
    return;
  }
  const comfy = getComfyApp();
  // 切走了工作流：先把最后一次改动写回，再停（此后草稿属于别的任务，不能再写）
  if (syncSubgraph?.rootGraph && comfy?.graph && syncSubgraph.rootGraph !== comfy.graph) {
    flushPipelineSync();
    stopPipelineSync();
    return;
  }
  flushPipelineSync();
  // 已经离开该子图（回到根图/别处）→ 上面已补写最后一次，收工
  if (comfy?.canvas?.graph?.id !== syncId) stopPipelineSync();
}

/**
 * 停止回写并立刻把当前内容落进卡片草稿。
 * 供卡片卸载（切工作流 tab 销毁节点）时调用，避免丢掉最后一次编辑。
 */
export function flushClipPipelineSync(): void {
  flushPipelineSync();
  stopPipelineSync();
}

/**
 * 开始回写轮询。`initial` 为刚注册/刚打开时的子图对象：以它的当前序列化结果作为基线，
 * 这样**打开后第一秒内的编辑也不会被当成基线吞掉**。
 */
function startPipelineSync(
  id: string,
  onEdited: (def: unknown) => void,
  initial: SubgraphLike,
): void {
  stopPipelineSync();
  syncId = id;
  syncHandler = onEdited;
  syncSubgraph = initial;
  lastJson = snapshotSubgraph()?.json ?? "";
  syncTimer = window.setInterval(pushPipelineSync, SYNC_INTERVAL_MS);
}

// ---------- 方案 A：前端摊平（子图定义 → 平铺节点图） ----------
// 后端不认识前端子图格式，所以由前端把「子图定义」摊成 ComfyUI API 格式的平铺节点图。
// 摊平规则（对齐前端 graphToPrompt 的语义）：
//   1. 内部节点：`type` → class_type；连到上游节点的输入 → [上游节点 id, 输出槽]
//   2. 连到子图输入节点(-10, origin_slot=i) 的输入 → [__studio__, "<槽名>"]
//      （按**名字**而不是下标：槽顺序变了绑定依然有效，运行时由 studio 按名字喂值）
//   3. 没连线的 widget 输入：按 `node.inputs` 里 widget 型输入的顺序与 `widgets_values`
//      位置对齐取值（连线优先于 widget 值）
//   4. 输出：target 指向子图输出节点(-20) 的那条连线，取其 origin 作为结果
// 限制（v1）：不支持嵌套子图（遇到会记录 warning 并跳过）；被旁路(mode=4)/静音(mode=2)
// 的节点按 ComfyUI 惯例跳过。

interface SerializedInput {
  name?: string;
  type?: string;
  link?: number | null;
  widget?: { name?: string };
}

interface SerializedNode {
  id?: number | string;
  type?: string;
  mode?: number;
  inputs?: SerializedInput[];
  widgets_values?: unknown;
}

interface SerializedLink {
  id?: number | string;
  origin_id?: number | string;
  origin_slot?: number;
  target_id?: number | string;
  target_slot?: number;
}

interface SubgraphDefLike {
  inputs?: { name?: string; type?: string; linkIds?: (number | string)[] }[];
  outputs?: { name?: string; type?: string; linkIds?: (number | string)[] }[];
  nodes?: SerializedNode[];
  links?: SerializedLink[];
}

/**
 * 把卡片绑定的子图摊平成平铺节点图（方案 A）。
 *
 * 纯函数、不抛异常：结构不对/缺连线都会落到 `warnings` 里，方便卡片上直接回显。
 */
export function flattenClipPipeline(pipeline: PipelineLibraryEntry): PipelineGraph {
  const warnings: string[] = [];
  const def = (pipeline.def ?? {}) as SubgraphDefLike;

  const inputs: PipelineGraphInput[] = (def.inputs ?? []).map((slot, i) => ({
    name: String(slot?.name ?? `input_${i}`),
    type: String(slot?.type ?? ""),
  }));

  const linkById = new Map<string, SerializedLink>();
  for (const link of def.links ?? []) {
    if (link?.id === undefined || link?.id === null) continue;
    linkById.set(String(link.id), link);
  }

  const nodes: Record<string, PipelineGraphNode> = {};
  for (const node of def.nodes ?? []) {
    if (node?.id === undefined || node?.id === null) continue;
    const nodeId = String(node.id);
    if (node.mode === 2 || node.mode === 4) {
      warnings.push(`节点 ${nodeId}（${node.type ?? "?"}）处于静音/旁路状态，已跳过`);
      continue;
    }
    // 嵌套子图：v1 不支持（内部节点 type 也是子图定义 id）
    if (!node.type) {
      warnings.push(`节点 ${nodeId} 缺少类型，已跳过`);
      continue;
    }

    const declared = node.inputs ?? [];
    const nodeInputs: Record<string, unknown> = {};

    // 1) 连线输入（连线优先）
    for (const inp of declared) {
      const name = inp?.name;
      if (!name || inp.link === undefined || inp.link === null) continue;
      const link = linkById.get(String(inp.link));
      if (!link) {
        warnings.push(`节点 ${nodeId} 的输入 ${name} 指向不存在的连线 ${inp.link}`);
        continue;
      }
      const originId = link.origin_id;
      if (originId === SUBGRAPH_INPUT_ID) {
        // 子图骨架输入 → studio 运行时提供（按**槽名**引用）
        const slot = inputs[Number(link.origin_slot ?? 0)];
        if (!slot) {
          warnings.push(
            `节点 ${nodeId} 的输入 ${name} 连到了不存在的骨架输入槽 ${link.origin_slot}`,
          );
          continue;
        }
        nodeInputs[name] = [STUDIO_INPUT_NODE, slot.name];
      } else if (originId === SUBGRAPH_OUTPUT_ID) {
        warnings.push(`节点 ${nodeId} 的输入 ${name} 非法连到子图输出节点`);
      } else {
        nodeInputs[name] = [String(originId), Number(link.origin_slot ?? 0)];
      }
    }

    // 2) widget 字面量：widgets_values 与 widget 型输入**按下标对齐**（含被连线顶替的）
    const widgetInputs = declared.filter((inp) => inp?.widget);
    const values = Array.isArray(node.widgets_values) ? node.widgets_values : [];
    if (node.widgets_values !== undefined && !Array.isArray(node.widgets_values)) {
      warnings.push(`节点 ${nodeId} 的 widgets_values 不是数组，已忽略其字面量输入`);
    }
    widgetInputs.forEach((inp, index) => {
      const name = inp?.widget?.name ?? inp?.name;
      if (!name) return;
      if (inp.link !== undefined && inp.link !== null) return; // 已由连线决定
      if (index >= values.length) return;
      nodeInputs[name] = values[index];
    });

    // 3) 非 widget 且未连线的输入：多半是漏接（可选输入可忽略）
    for (const inp of declared) {
      if (!inp?.name || inp.widget) continue;
      if (inp.link !== undefined && inp.link !== null) continue;
      warnings.push(`节点 ${nodeId}（${node.type}）的输入 ${inp.name} 未连线（可选输入可忽略）`);
    }

    nodes[nodeId] = { class_type: String(node.type), inputs: nodeInputs };
  }

  // 4) 输出：target 指向子图输出节点的连线
  let output: [string, number] | null = null;
  const outLinks = (def.links ?? []).filter((l) => l?.target_id === SUBGRAPH_OUTPUT_ID);
  if (outLinks.length === 0) {
    warnings.push("子图输出槽没有连线：请把采样链最后一个节点的 LATENT 输出连到子图输出");
  } else {
    const link = outLinks.find((l) => Number(l.target_slot ?? 0) === 0) ?? outLinks[0];
    if (outLinks.length > 1) {
      warnings.push(`子图输出槽有 ${outLinks.length} 条连线，只取第 1 个`);
    }
    output = [String(link.origin_id), Number(link.origin_slot ?? 0)];
  }

  // 5) 骨架输入槽没被用到 / 未知槽名 / 节点空 —— 提前给出可读提示
  const usedInputNames = new Set<string>();
  for (const node of Object.values(nodes)) {
    for (const value of Object.values(node.inputs)) {
      if (Array.isArray(value) && value[0] === STUDIO_INPUT_NODE) {
        usedInputNames.add(String(value[1]));
      }
    }
  }
  const providedNames = new Set(SKELETON_INPUTS.map((s) => s.name));
  inputs.forEach((slot) => {
    if (!providedNames.has(slot.name)) {
      warnings.push(
        `骨架输入「${slot.name}」不是 studio 提供的输入（${[...providedNames].join(" / ")}），运行时不会拿到值`,
      );
      return;
    }
    if (!usedInputNames.has(slot.name)) {
      warnings.push(`骨架输入「${slot.name}」没有连到任何节点，运行时会被忽略`);
    }
  });
  if (Object.keys(nodes).length === 0) {
    warnings.push("子图里还没有节点：请在里面拉出采样链");
  }

  // 6) 骨架槽类型契约：名字对上但类型不符时提示（运行时按 studio 的类型喂）
  const expectType = new Map(SKELETON_INPUTS.map((s) => [s.name, s.type]));
  inputs.forEach((slot) => {
    const expectedType = expectType.get(slot.name);
    if (expectedType && slot.type && slot.type !== expectedType) {
      warnings.push(`骨架输入「${slot.name}」类型是 ${slot.type}，运行时按 ${expectedType} 喂入`);
    }
  });

  return { version: 1, inputs, nodes, output, warnings };
}

