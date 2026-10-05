import { defineStore } from "pinia";
import {
  type CanvasConfig,
  type StudioPayload,
  type ReferenceMedia,
  type Clip,
  type ClipHistory,
  type ClipPayload,
  type ClipPipelinePayload,
  type PipelineLibraryEntry,
  type PromptSnapshot,
  type VersionSample,
  DURATION_LIMITS,
} from "@/types/timeline";
import { cloneClipPipeline, flattenClipPipeline, newClipPipeline, openClipPipeline } from "@/utils/subgraph";

/** 采样流程导出文件标记（导入时校验；与任务导出 `.studio-task.json` 同风格） */
const PIPELINE_EXPORT_TYPE = "minimax-h3-studio-pipeline";
const PIPELINE_EXPORT_VERSION = 1;

/** 生成片段 id（避免与随机碰撞） */
function createId(): string {
  return `clip_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

/** ---------- 会话内时间线快照（切工作流 tab / 写库失败时的兜底） ----------
 *  时间线唯一持久源仍是 SQLite；这里只做「同一浏览器会话内」的内存兜底：
 *  - 切工作流 tab（节点销毁重建）或写库未落地/失败时，恢复走这份快照，
 *    保证「正在编辑、还没采样过的提示词」不会因为换 tab 而丢。
 *  - 键是 taskId 本身：恢复时已经确定要打开哪个任务，不存在张冠李戴。
 *  - 页面刷新/关闭即失效（那时以 DB 为准）。 */
const SESSION_SNAPSHOT_LIMIT = 20;
/** 会话内时间线快照：payload + 采样流程库（库不进 StudioPayload，但兜底恢复时要一起留着） */
type TimelineSnapshot = StudioPayload & { pipelines?: PipelineLibraryEntry[] };

const sessionTimelines = new Map<string, TimelineSnapshot>();

/** 记录某任务的时间线快照（落库前调用，覆盖式） */
function rememberSessionTimeline(taskId: string, payload: TimelineSnapshot): void {
  sessionTimelines.delete(taskId); // 重插 → Map 迭代顺序按最近使用排列
  sessionTimelines.set(taskId, payload);
  while (sessionTimelines.size > SESSION_SNAPSHOT_LIMIT) {
    const oldest = sessionTimelines.keys().next().value;
    if (oldest === undefined) break;
    sessionTimelines.delete(oldest);
  }
}

function sessionTimelineOf(taskId: string): TimelineSnapshot | null {
  return sessionTimelines.get(taskId) ?? null;
}

/** ComfyUI fetchApi（自动加 /api 前缀） */
function fetchApi(url: string, init?: RequestInit): Promise<Response> {
  const api = (window as { app?: { api?: { fetchApi: (u: string, i?: RequestInit) => Promise<Response> } } })
    .app?.api;
  if (!api?.fetchApi) return Promise.reject(new Error("ComfyUI fetchApi 不可用"));
  return api.fetchApi(url, init);
}

export const useTimelineStore = defineStore("timeline", {
  state: () => ({
    clips: [] as Clip[],
    /** 任务级采样流程库（一份定义可被多张卡片按 pipelineId 引用；随任务时间线存 DB） */
    pipelines: [] as PipelineLibraryEntry[],
    canvas: {
      fps: 24,
      width: 864,
      height: 480,
    } as CanvasConfig,
    selectedId: null as string | null,
    /** 每秒像素宽度（时间线缩放） */
    zoom: 64 as number,
    /** 当前加载的任务 id（任务库模式：时间线唯一数据源在 SQLite，工作流 json 只存此 id） */
    taskId: null as string | null,
    /** ★ 时间线**已就绪**的任务 id：只有 loadTask 成功（或新建任务）后才等于 taskId。
     *  不变式 I2：未就绪时禁止把（此刻还空着的）clips 写回 DB —— 切 tab 后恢复期间
     *  任何保存（含面板挂载时 setZoom 触发的防抖保存）都会用空时间线覆盖 DB。 */
    loadedTaskId: null as string | null,
    /** ★ 用户显式卸载/删除任务时为 true：只有此时才允许清空工作流载体里的 taskId。
     *  不变式 I1 的载体侧：瞬时 taskId=null 绝不写空载体（否则工作流永久失绑）。 */
    bindingReleased: false as boolean,
    /** 最近一次任务加载失败（网络/5xx/超时，非"任务不存在"）：内容仍保留本地，UI 可提示 */
    loadFailed: false as boolean,
    /** 最近一次落库失败（网络/服务异常）：UI 可提示"改动还在内存里，尚未写入 DB" */
    saveFailed: false as boolean,
    /** 任务在服务端确实不存在（404，跨机工作流/库被清）：只有这种情况才回未加载态 */
    taskMissing: false as boolean,
    /** 当前任务可读名称（列表/标题展示） */
    taskName: "" as string,
    /** 所属 ComfyUI 节点 id（创建任务用） */
    nodeId: "" as string,
    /** 卡片历史（按 clip_id 身份索引；加载任务后由 fetchHistory 拉取，片段容器模型） */
    historyByClipId: {} as Record<string, ClipHistory>,
    /** 当前采样片段的进度（executor 经 WebSocket 广播，卡片底部进度条用）；null=无采样中 */
    samplingProgress: null as {
      clipId: string;
      phase: string;
      value: number;
      step?: number;
      stepsTotal?: number;
      /** live 预览（采样过程动画 WebP 的 base64，直接作卡片背景；非采样中不存在） */
      preview?: string;
    } | null,
    /** 恢复历史片段面板开关（UI 状态：工具栏/空态区共用入口） */
    showRestoreModal: false as boolean,
    /** 片段历史弹窗开关（UI 状态：卡片历史图标 / 详情面板共用入口） */
    showHistoryPanel: false as boolean,
  }),

  getters: {
    totalDurationSec(state): number {
      return state.clips.reduce((acc, seg) => acc + seg.durationSec, 0);
    },

    /** 按 id 取流程库条目（未绑定/已被删除返回 null → 该片段走内置官方流程） */
    pipelineById(state): (id: string | null | undefined) => PipelineLibraryEntry | null {
      return (id) => (id ? state.pipelines.find((p) => p.id === id) ?? null : null);
    },

    /** 片段当前生效的采样流程（null = 内置官方流程） */
    pipelineOf(state) {
      return (clip: Clip | null | undefined): PipelineLibraryEntry | null => {
        const id = clip?.pipelineId;
        return id ? state.pipelines.find((p) => p.id === id) ?? null : null;
      };
    },

    /** 某流程被多少张卡片引用（删除确认 / 共享提示用） */
    pipelineUsage(state): (id: string) => number {
      return (id) => state.clips.filter((c) => c.pipelineId === id).length;
    },
    selectedClip(state): Clip | null {
      return state.clips.find((s) => s.id === state.selectedId) ?? null;
    },
  },

  actions: {
    /** 添加片段（默认追加到末尾，可指定插入位置） */
    addClip(partial: Partial<Clip> = {}, index?: number): Clip {
      const seg: Clip = {
        id: createId(),
        mode: "t2v",
        prompt: "",
        durationSec: 4,
        enabled: true,
        continuity: false,
        ...partial,
      };
      const target = index ?? this.clips.length;
      this.clips.splice(target, 0, seg);
      this.selectedId = seg.id;
      return seg;
    },

    removeClip(id: string): void {
      const idx = this.clips.findIndex((s) => s.id === id);
      if (idx === -1) return;
      this.clips.splice(idx, 1);
      if (this.selectedId === id) this.selectedId = null;
    },

    duplicateClip(id: string): void {
      const idx = this.clips.findIndex((s) => s.id === id);
      if (idx === -1) return;
      const src = this.clips[idx];
      const copy: Clip = {
        ...src,
        id: createId(),
        prompt: src.prompt,
        // 采样流程是**引用**：副本沿用同一份流程（流程库的意义就是一份挂多张卡片）；
        // 要独立改一份，用流程库的「复制为新流程」。
      };
      this.clips.splice(idx + 1, 0, copy);
      this.selectedId = copy.id;
    },

    /** 移动片段（拖拽排序用） */
    moveClip(fromIndex: number, toIndex: number): void {
      if (fromIndex === toIndex) return;
      const [seg] = this.clips.splice(fromIndex, 1);
      this.clips.splice(toIndex, 0, seg);
    },

    // ---------- 采样流程库（任务级；一份定义可被多张卡片引用） ----------

    /** 生成不与现有流程重名的名称（"X" → "X（2）"） */
    uniquePipelineName(base: string): string {
      const name = base.trim() || "未命名流程";
      if (!this.pipelines.some((p) => p.name === name)) return name;
      for (let i = 2; i < 1000; i += 1) {
        const candidate = `${name}（${i}）`;
        if (!this.pipelines.some((p) => p.name === candidate)) return candidate;
      }
      return `${name}（${Date.now()}）`;
    },

    /** 新建流程（默认给一份空白骨架槽：6 进 / 1 出） */
    createPipeline(label = ""): PipelineLibraryEntry {
      const entry = newClipPipeline(this.uniquePipelineName(label || `流程 ${this.pipelines.length + 1}`));
      this.pipelines.push(entry);
      void this.saveToDb();
      return entry;
    },

    /** 打开某流程的原生子图编辑器（工具栏库管理入口：不依赖卡片；编辑内容回写库） */
    openPipelineEditor(id: string): { ok: boolean; message: string } {
      const entry = this.pipelines.find((p) => p.id === id);
      if (!entry) return { ok: false, message: "流程不存在（可能已被删除）" };
      return openClipPipeline(entry, (def) => {
        // 回写前确认流程还在库里（删除后的迟到回写必须丢弃）
        if (this.pipelines.some((p) => p.id === entry.id)) this.updatePipelineDef(entry.id, def);
      });
    },

    /** 导出单份流程为可移植 JSON（前端直接下载；不含任何本机路径/缓存） */
    exportPipeline(id: string): boolean {
      const entry = this.pipelines.find((p) => p.id === id);
      if (!entry) return false;
      const payload = {
        type: PIPELINE_EXPORT_TYPE,
        version: PIPELINE_EXPORT_VERSION,
        name: entry.name,
        def: entry.def,
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${entry.name.replace(/[\\/:*?"<>|]/g, "_")}.studio-pipeline.json`;
      a.click();
      URL.revokeObjectURL(url);
      return true;
    },

    /** 导入流程文件（别人分享的流程）：校验标记 → 以**新 id** 收进流程库 */
    async importPipelineFile(file: File): Promise<{ ok: boolean; message: string }> {
      let data: unknown;
      try {
        data = JSON.parse(await file.text());
      } catch {
        return { ok: false, message: "不是合法的 JSON 文件" };
      }
      const obj = data as { type?: string; version?: number; name?: string; def?: unknown };
      if (obj?.type !== PIPELINE_EXPORT_TYPE) {
        return { ok: false, message: "不是采样流程文件（缺少 type 标记）" };
      }
      if (obj.version !== PIPELINE_EXPORT_VERSION) {
        return { ok: false, message: `不支持的流程文件版本：${String(obj.version)}` };
      }
      if (!obj.def || typeof obj.def !== "object") {
        return { ok: false, message: "流程文件缺少 def 定义" };
      }
      const name = this.uniquePipelineName(String(obj.name || "导入的流程"));
      // 新 id（copy 内会 JSON 深拷贝并改写 def 里的 id/name），避免与现有流程撞 id / 子图冲突
      const entry = cloneClipPipeline({ id: "", name, def: obj.def }, name);
      this.pipelines.push(entry);
      void this.saveToDb();
      return { ok: true, message: `已导入流程：${entry.name}` };
    },

    /** 回写流程定义（子图编辑器轮询回写；引用它的所有片段一起生效） */
    updatePipelineDef(id: string, def: unknown): void {
      const entry = this.pipelines.find((p) => p.id === id);
      if (!entry) return;
      entry.def = def;
    },

    renamePipeline(id: string, name: string): void {
      const entry = this.pipelines.find((p) => p.id === id);
      const trimmed = name.trim();
      if (!entry || !trimmed || entry.name === trimmed) return;
      entry.name = trimmed;
    },

    /** 复制为新流程（要独立改一份时的出口：新 id + 内容照抄） */
    duplicatePipeline(id: string): PipelineLibraryEntry | null {
      const src = this.pipelines.find((p) => p.id === id);
      if (!src) return null;
      const copy = cloneClipPipeline(src, `${src.name} 副本`);
      this.pipelines.push(copy);
      return copy;
    },

    /**
     * 删除流程：引用它的片段全部回到内置官方流程（**定义不可恢复**，调用方需二次确认）。
     * @returns 受影响的片段数
     */
    deletePipeline(id: string): number {
      const idx = this.pipelines.findIndex((p) => p.id === id);
      if (idx === -1) return 0;
      this.pipelines.splice(idx, 1);
      let affected = 0;
      for (const seg of this.clips) {
        if (seg.pipelineId === id) {
          delete seg.pipelineId;
          affected += 1;
        }
      }
      return affected;
    },

    /** 绑定/解绑片段与流程（null = 回到内置官方流程） */
    setClipPipeline(clipId: string, pipelineId: string | null): void {
      const seg = this.clips.find((s) => s.id === clipId);
      if (!seg) return;
      if (pipelineId) seg.pipelineId = pipelineId;
      else delete seg.pipelineId;
    },

    updateClip(id: string, patch: Partial<Clip>): void {
      const seg = this.clips.find((s) => s.id === id);
      if (!seg) return;
      Object.assign(seg, patch);
      if (patch.durationSec !== undefined) {
        seg.durationSec = Math.min(
          DURATION_LIMITS.maxSec,
          Math.max(DURATION_LIMITS.minSec, patch.durationSec),
        );
      }
    },

    select(id: string | null): void {
      this.selectedId = id;
    },

    /** 更新画布配置（必须走 action，外部订阅靠 $onAction 感知变化）。
     *  画布全局变更 → 所有旧 latent 缓存失效：清除全部勾选并持久化。
     *  返回是否清除了勾选（供 UI 提示「缓存将失效，请重新采样」）。 */
    updateCanvas(patch: Partial<CanvasConfig>): boolean {
      const changed =
        (patch.width !== undefined && patch.width !== this.canvas.width) ||
        (patch.height !== undefined && patch.height !== this.canvas.height) ||
        (patch.fps !== undefined && patch.fps !== this.canvas.fps);
      Object.assign(this.canvas, patch);
      if (!changed) return false;
      let cleared = false;
      for (const c of this.clips) {
        if (c.sampleFp) {
          delete c.sampleFp;
          cleared = true;
        }
      }
      if (cleared) void this.saveToDb(); // 持久化清除勾选
      return cleared;
    },

    /** 设置音频模式（解码阶段参数，暂不由前端提供；保留 action 供后续扩展） */
    setAudioMode(_mode: string): void {
      // no-op：audioMode 非采样参数，解码链路实现后再启用
    },

    setZoom(zoom: number): void {
      this.zoom = Math.min(256, Math.max(24, zoom));
    },

    /** 序列化为发给后端的数据负载（数据契约出口）。
     *  这里把片段的流程引用**解析**成 `{id, name, graph}`（方案 A 摊平图），
     *  后端执行器直接把 [__studio__, "槽名"] 换成运行时输入即可；未绑定的片段不带 pipeline。 */
    serialize(): StudioPayload {
      return {
        version: 1,
        canvas: { ...this.canvas },
        clips: this.clips.map((c) => toClipPayload(c, this.pipelines)),
        totalDurationSec: this.totalDurationSec,
      };
    },

    /** 从外部数据加载（集成到 ComfyUI 时用于恢复工作流状态） */
    loadFromPayload(payload: StudioPayload): void {
      this.canvas = { ...payload.canvas };
      this.clips = payload.clips.map(fromClipPayload);
      this.selectedId = this.clips[0]?.id ?? null;
    },

    // ---------- 任务库（时间线唯一数据源在 SQLite） ----------

    setTaskId(taskId: string | null): void {
      this.taskId = taskId;
      // 重新绑定任务 → 载体可继续写（清掉"显式卸载"标记）
      if (taskId) this.bindingReleased = false;
    },

    setNodeId(nodeId: string): void {
      this.nodeId = nodeId;
    },

    /** 新建空任务（清空当前时间线并创建新任务记录），创建后立即落库 */
    async newTask(nodeId: string, name = ""): Promise<string | null> {
      this.clips = [];
      this.pipelines = [];
      this.canvas = { fps: 24, width: 864, height: 480 };
      this.selectedId = null;
      this.historyByClipId = {}; // 新任务无历史
      const tid = await this.createTask(nodeId, name);
      if (tid) {
        this.loadedTaskId = tid; // 新任务：时间线就是空的，允许落库（合法的空写入）
        await this.saveToDb(); // DB 里始终有合法 payload（避免 executor 读到空时间线）
      }
      return tid;
    },

    /** 卸载当前任务（删除后回到"未加载任务"的待加载界面）：
     *  清空时间线 + taskId，不创建任何新任务。
     *  bindingReleased=true 是**唯一**允许清空工作流载体的信号（I1 载体侧）。 */
    unloadTask(): void {
      this.clips = [];
      this.pipelines = [];
      this.canvas = { fps: 24, width: 864, height: 480 };
      this.selectedId = null;
      this.taskId = null;
      this.taskName = "";
      this.historyByClipId = {};
      this.samplingProgress = null;
      this.loadedTaskId = null;
      this.loadFailed = false;
      this.taskMissing = false;
      this.bindingReleased = true;
    },

    /** 从 DB 加载任务：timeline（时间线当前数据 = canvas + clips[]，每 clip 含完整参数草稿）。
     *  片段当前数据独立于历史（草稿不丢，崩溃/刷新恢复）；历史版本仅作反悔来源。
     *
     *  ★ 失败语义（切工作流 tab 会高频走这里，必须严格区分，否则一次抖动 = 丢数据）：
     *    - 404（任务在本地库确实不存在）→ taskMissing=true 且返回 false，由调用方决定回未加载态
     *    - 其它失败（网络/5xx/超时/响应非法）→ loadFailed=true 且返回 false，
     *      **绝不清空当前内存时间线**（调用方不得再调 unloadTask：那里是正在编辑的草稿）
     *
     *  ★ 不变式 I1（空不覆盖非空）：DB 读到空时间线、而本地/会话快照有该任务内容时，
     *    保留非空内容并回写 DB —— 本地才是用户正在编辑的那一份。 */
    async loadTask(taskId: string): Promise<boolean> {
      this.loadFailed = false;
      this.taskMissing = false;

      let res: Response;
      try {
        res = await fetchApi(`/minimax/studio/tasks/${encodeURIComponent(taskId)}`);
      } catch {
        this.loadFailed = true;
        return false;
      }
      if (res.status === 404) {
        this.taskMissing = true;
        return false;
      }
      if (!res.ok) {
        this.loadFailed = true;
        return false;
      }

      let data: { name?: string; timeline?: string };
      try {
        data = (await res.json()) as { name?: string; timeline?: string };
      } catch {
        this.loadFailed = true;
        return false;
      }

      let seq: {
        canvas?: CanvasConfig;
        clips?: Record<string, unknown>[];
        /** 任务级采样流程库（引用模型：片段只存 pipelineId） */
        pipelines?: PipelineLibraryEntry[];
      } = {};
      try {
        seq = JSON.parse(data.timeline || "{}");
      } catch {
        seq = {};
      }

      const rows = seq.clips ?? [];
      const incoming = rows.map((c) => fromClipPayload(c as unknown as ClipPayload));
      // 流程库：新版直接读 `pipelines`；旧数据把定义内联在 clips[].pipeline.def 里
      //   → 收编进库（引用 model 之前的数据无需手工迁移）。
      const library: PipelineLibraryEntry[] = Array.isArray(seq.pipelines)
        ? seq.pipelines.filter((p) => p && typeof p.id === "string")
        : [];
      for (const legacy of legacyPipelinesFromRows(rows)) {
        if (!library.some((p) => p.id === legacy.id)) library.push(legacy);
      }
      // 兜底来源优先级：本地已就绪的同任务内容 > 本会话快照
      const localReady = this.loadedTaskId === taskId && this.clips.length > 0;
      const cached = sessionTimelineOf(taskId);
      if (incoming.length === 0 && (localReady || (cached?.clips.length ?? 0) > 0)) {
        const fallback = localReady ? this.clips : (cached as StudioPayload).clips.map(fromClipPayload);
        if (seq.canvas) this.canvas = { ...seq.canvas };
        else if (!localReady && cached) this.canvas = { ...cached.canvas };
        this.clips = fallback;
        // 流程库：本地已就绪优先保留本地；否则用会话快照里的库（都没有就只能是空的）
        if (!localReady && cached?.pipelines?.length) {
          this.pipelines = cached.pipelines.map((p) => ({ id: p.id, name: p.name, def: p.def }));
        }
        this.selectedId = this.clips[0]?.id ?? null;
        this.taskId = taskId;
        this.loadedTaskId = taskId;
        this.taskName = data.name ?? "";
        console.warn(
          `[StudioConsole] 任务 ${taskId} 的 DB 时间线为空，已保留本地/会话内容并回写（防空覆盖非空）`,
        );
        void this.saveToDb(); // 修复 DB（此刻 loadedTaskId 已就绪，允许写）
        await this.fetchHistory();
        return true;
      }

      if (seq.canvas) this.canvas = { ...seq.canvas };
      // 直接恢复每 clip 的当前参数草稿（timeline 是权威，不指向历史）
      this.clips = incoming;
      this.pipelines = library;
      this.selectedId = this.clips[0]?.id ?? null;
      this.taskId = taskId;
      this.loadedTaskId = taskId;
      this.taskName = data.name ?? "";
      await this.fetchHistory(); // 历史（纯 Model）拉取，供反悔展示
      return true;
    },

    /** 打开/关闭「从历史恢复片段」面板（UI 状态，跨组件共享入口） */
    openRestoreModal(): void {
      this.showRestoreModal = true;
    },
    closeRestoreModal(): void {
      this.showRestoreModal = false;
    },

    /** 打开片段历史弹窗（UI 状态，跨组件共享入口）；打开即刷新历史（删除/过期数据后保持新鲜） */
    openHistoryPanel(): void {
      this.showHistoryPanel = true;
      if (this.taskId) void this.fetchHistory();
    },
    closeHistoryPanel(): void {
      this.showHistoryPanel = false;
    },

    /** 从历史手动挑选恢复卡片：把所选 clip 的指定版本快照追加到时间线末尾。
     *  id 沿用 clip_id（历史继续跟随）；已在时间线的 clip 自动跳过；不覆盖已有片段。
     *  返回实际恢复数。 */
    async addClipsFromHistory(
      selections: { clipId: string; versionId: number }[],
    ): Promise<number> {
      if (!this.taskId || !selections.length) return 0;
      await this.fetchHistory(); // 确保历史最新（本地缓存可能过期）
      const existing = new Set(this.clips.map((c) => c.id));
      let added = 0;
      for (const sel of selections) {
        if (existing.has(sel.clipId)) continue;
        const ver = this.historyByClipId[sel.clipId]?.versions.find(
          (v) => v.versionId === sel.versionId,
        );
        if (!ver?.snapshot) continue;
        // 条目快照无 enabled/时长（执行态/规格不属于内容）：恢复卡片默认参与生成，
        // 时长取该 clip 最近采样规格（无则 4s）
        const durationSec =
          this.historyByClipId[sel.clipId]?.samples[0]?.durationSec ?? 4;
        this.addClip(fromClipPayload({ ...ver.snapshot, enabled: true, durationSec })); // 追加到末尾，id 沿用 clip_id
        existing.add(sel.clipId);
        added++;
      }
      if (added) await this.saveToDb(); // 恢复后的时间线立即持久化（覆盖式草稿保存）
      return added;
    },

    /** 拉取任务历史（纯 Model：历史版本 + 采样记录，独立于片段当前数据） */
    async fetchHistory(): Promise<void> {
      if (!this.taskId) return;
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(this.taskId)}/history`,
        );
        if (!res.ok) return;
        const data = await res.json();
        const map: Record<string, ClipHistory> = {};
        for (const [clipId, h] of Object.entries(data.history ?? {})) {
          const history = h as ClipHistory;
          map[clipId] = {
            versions: history.versions ?? [],
            samples: history.samples ?? [],
          };
        }
        this.historyByClipId = map;
      } catch {
        // 历史是增强信息，失败静默（不阻塞时间线加载）
      }
    },

    /** 取某提示词条目的画面语义快照（历史回填/版本比对用；无则 null） */
    promptSnapshotOf(clipId: string, versionId: number): PromptSnapshot | null {
      const ver = this.historyByClipId[clipId]?.versions.find(
        (v) => v.versionId === versionId,
      );
      return ver?.snapshot ?? null;
    },

    /** 应用提示词条目：画面语义回填编辑面板 + 解锁（不锁定 latent，用于"回到旧内容重新采样"）。
     *  覆盖确认由 UI 层负责（会覆盖当前 prompt/素材）。执行态/编排保持片段当前值。 */
    loadPromptEntry(clipId: string, versionId: number): boolean {
      const seg = this.clips.find((s) => s.id === clipId);
      const snap = this.promptSnapshotOf(clipId, versionId);
      if (!seg || !snap) return false;
      applySnapshotTo(seg, snap);
      delete seg.sampleFp; // 内容切走 → 旧锁定失效（该 latent 基于旧内容）
      void this.saveToDb();
      return true;
    },

    /** 启用采样（抽卡级反悔）：回填该采样所属条目的画面语义 + 恢复片段时长 + 锁定该 latent。
     *  Queue 时该片段跳过采样直接用缓存出片；内容已与 latent 对齐（回填保证），
     *  后端只兑底校验画布。分辨率不匹配时的全局切画布与时长/内容覆盖确认由 UI 层
     *  先完成（切画布会清全部锁定，随后这里重新锁定目标）。 */
    applySample(clipId: string, sample: VersionSample): boolean {
      const seg = this.clips.find((s) => s.id === clipId);
      const snap = this.promptSnapshotOf(clipId, sample.versionId);
      if (!seg || !snap) return false;
      applySnapshotTo(seg, snap);
      // 时长是样本规格：随所选 latent 恢复（该样本出片长度）
      if (sample.durationSec > 0) seg.durationSec = sample.durationSec;
      seg.sampleFp = sample.sampleFp;
      void this.saveToDb();
      return true;
    },

    /** 取消锁定（恢复自动采样：Queue 时按当前内容 + 全局工艺采样/命中缓存）；内容保持当前 */
    releaseSample(clipId: string): void {
      const seg = this.clips.find((s) => s.id === clipId);
      if (!seg || !seg.sampleFp) return;
      delete seg.sampleFp;
      void this.saveToDb();
    },

    /** 更新当前采样片段进度（executor studio_progress 事件）
     *  合并而非替换：preview 由 studio_preview 事件独立维护（TAE 解码异步，晚于进度到达），
     *  若每步进度都替换对象会清掉 preview，导致 hover 中预览闪断。 */
    setSamplingProgress(p: {
      clipId: string;
      phase: string;
      value: number;
      step?: number;
      stepsTotal?: number;
      preview?: string;
    } | null): void {
      if (p === null) {
        this.samplingProgress = null;
        return;
      }
      const prev = this.samplingProgress;
      this.samplingProgress = {
        ...p,
        // 同片段时保留已有 live 预览（新预览帧到来自动替换；换段则丢弃旧段画面）
        ...(prev?.preview && prev.clipId === p.clipId ? { preview: prev.preview } : {}),
      };
    },

    /** 更新当前采样片段的 live 预览（executor studio_preview 事件；仅当前片段生效，旧帧自动被新帧替换） */
    setLivePreview(clipId: string, image: string | null): void {
      if (!this.samplingProgress || this.samplingProgress.clipId !== clipId) return;
      if (image) {
        this.samplingProgress = { ...this.samplingProgress, preview: image };
      } else if (this.samplingProgress.preview) {
        const { preview: _drop, ...rest } = this.samplingProgress;
        this.samplingProgress = rest;
      }
    },


    /** 删除卡片：前端移除 + 同步清后端历史（提示词条目/采样记录/latent 文件，不可逆）
     *  后端删除失败则不移除前端卡片（避免前后端不一致、历史残留成孤儿）。 */
    async deleteClip(clipId: string): Promise<boolean> {
      if (this.taskId) {
        try {
          const res = await fetchApi(
            `/minimax/studio/tasks/${encodeURIComponent(this.taskId)}/clips/${encodeURIComponent(clipId)}`,
            { method: "DELETE" },
          );
          if (!res.ok) return false; // 后端删除失败：保持前端现状，不产生孤儿历史
        } catch {
          return false;
        }
      }
      this.removeClip(clipId);
      delete this.historyByClipId[clipId];
      return true;
    },

    /** 删除单个采样样本（历史区抽卡项）：后端删记录 +（无跨任务引用时）缓存文件。
     *  若删的是当前勾选的 latent → 清勾选并持久化（避免 Queue 时后端校验报文件丢失）。 */
    async deleteSample(clipId: string, sampleFp: string): Promise<boolean> {
      if (!this.taskId) return false;
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(this.taskId)}/clips/${encodeURIComponent(clipId)}/samples/${encodeURIComponent(sampleFp)}`,
          { method: "DELETE" },
        );
        if (!res.ok) return false;
        const seg = this.clips.find((s) => s.id === clipId);
        if (seg?.sampleFp === sampleFp) {
          delete seg.sampleFp;
          void this.saveToDb();
        }
        await this.fetchHistory();
        return true;
      } catch {
        return false;
      }
    },

    /** 删除提示词条目及其全部采样；若该条目含当前锁定的 latent → 清锁定。 */
    async deleteVersion(clipId: string, versionId: number): Promise<boolean> {
      if (!this.taskId) return false;
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(this.taskId)}/clips/${encodeURIComponent(clipId)}/versions/${versionId}`,
          { method: "DELETE" },
        );
        if (!res.ok) return false;
        const seg = this.clips.find((s) => s.id === clipId);
        if (seg?.sampleFp) {
          const hist = this.historyByClipId[clipId];
          const belongs = hist?.samples.some(
            (s) => s.sampleFp === seg.sampleFp && s.versionId === versionId,
          );
          if (belongs) {
            delete seg.sampleFp;
            void this.saveToDb();
          }
        }
        await this.fetchHistory();
        return true;
      } catch {
        return false;
      }
    },

    /** 创建新任务（空时间线），返回 task_id */
    async createTask(nodeId: string, name = ""): Promise<string | null> {
      try {
        const res = await fetchApi("/minimax/studio/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node_id: nodeId, name }),
        });
        if (!res.ok) return null;
        const data = await res.json();
        this.taskId = String(data.task_id);
        this.taskName = name;
        return String(data.task_id);
      } catch {
        return null;
      }
    },

    /** 重命名当前任务 */
    async renameTask(taskId: string, name: string): Promise<boolean> {
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(taskId)}/name`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          },
        );
        if (res.ok && taskId === this.taskId) this.taskName = name;
        return res.ok;
      } catch {
        return false;
      }
    },

    /** 复制当前任务为新任务（后端 DB 深拷贝：时间线 + 提示词历史，不含采样/latent 缓存），
     *  成功后自动加载副本（原任务保持不变）。复制前调用方需先 saveToDb 把最新草稿落库。 */
    async duplicateTask(taskId: string, name: string): Promise<string | null> {
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(taskId)}/duplicate`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ node_id: this.nodeId, name }),
          },
        );
        if (!res.ok) return null;
        const out = await res.json();
        const tid = String(out.task_id);
        const ok = await this.loadTask(tid);
        return ok ? tid : null;
      } catch {
        return null;
      }
    },

    /** 保存时间线当前数据到 DB：canvas + clips[]（每 clip 完整参数草稿，覆盖式自动保存）。
     *  片段当前数据独立于历史（草稿不丢，崩溃/刷新恢复）；历史版本由采样固化。
     *
     *  ★ 不变式 I2（未就绪不得回写）：切工作流 tab 后 store 会短暂处于
     *    「taskId 已设、clips 还空」的恢复窗口，此时任何保存都会用空时间线覆盖 DB
     *    （实测：面板挂载时 setZoom 触发的防抖保存即可在 100ms 内清空整个任务）。
     *    因此只有 loadedTaskId === taskId（时间线已就绪）才允许写库。 */
    async saveToDb(): Promise<boolean> {
      if (!this.taskId) return false;
      if (this.loadedTaskId !== this.taskId) return false; // 恢复未完成：拒绝写库（防空覆盖）
      // 时间线当前数据 = canvas + clips（含流程引用）+ 采样流程库（定义的唯一真相）；
      // 摊平图是派生物（发 payload 时才生成），不落库。
      const seq = {
        version: 1,
        canvas: { ...this.canvas },
        clips: this.clips.map((c) => toClipDbRow(c)),
        pipelines: this.pipelines.map((p) => ({ id: p.id, name: p.name, def: p.def })),
        totalDurationSec: this.totalDurationSec,
      };
      // 会话内快照：先记后发 —— 写库失败/被清空时仍能兜底恢复（I1 的内存侧）；
      // 流程库也一起记（库不在 StudioPayload 里，兜底恢复缺它就全变默认流程）
      rememberSessionTimeline(this.taskId, {
        version: 1,
        canvas: { ...this.canvas },
        clips: this.clips.map((c) => toClipDbRow(c) as unknown as ClipPayload),
        pipelines: this.pipelines.map((p) => ({ id: p.id, name: p.name, def: p.def })),
        totalDurationSec: this.totalDurationSec,
      });
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(this.taskId)}/timeline`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timeline: JSON.stringify(seq) }),
          },
        );
        // 失败可见：改动仍在内存（并有会话快照兜底），但用户需要知道还没落库
        this.saveFailed = !res.ok;
        if (!res.ok) console.error(`[StudioConsole] 时间线落库失败（HTTP ${res.status}）：改动仍在本地`);
        return res.ok;
      } catch (err) {
        this.saveFailed = true;
        console.error("[StudioConsole] 时间线落库失败（网络异常）：改动仍在本地", err);
        return false;
      }
    },

    /** 任务列表（工具栏下拉用；任务库全局，不按节点过滤——节点 id 不稳定） */
    async fetchTaskList(): Promise<Array<Record<string, unknown>>> {
      try {
        const res = await fetchApi("/minimax/studio/tasks");
        if (!res.ok) return [];
        const data = await res.json();
        return data.tasks ?? [];
      } catch {
        return [];
      }
    },

    /** 删除任务（连带 latent 缓存文件） */
    async deleteTask(taskId: string): Promise<boolean> {
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(taskId)}`,
          { method: "DELETE" },
        );
        if (res.ok) sessionTimelines.delete(taskId); // 任务没了，会话快照一并清掉
        return res.ok;
      } catch {
        return false;
      }
    },

    /** 导出当前任务：GET export → 下载可移植 JSON（时间线 + 提示词历史，不含素材/latent 文件） */
    async exportTask(taskId: string): Promise<boolean> {
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(taskId)}/export`,
        );
        if (!res.ok) return false;
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const safe = (this.taskName || `task_${taskId}`).replace(/[\\/:*?"<>|]/g, "_");
        a.download = `${safe}.studio-task.json`;
        a.click();
        URL.revokeObjectURL(url);
        return true;
      } catch {
        return false;
      }
    },

    /** 导入任务文件：校验导出标记 → POST import 新建任务 → 加载（时间线 + 历史提示词恢复）。
     *  返回新 task_id；文件非法/请求失败返回 null。 */
    async importTaskFile(file: File): Promise<string | null> {
      try {
        const data = JSON.parse(await file.text());
        if (!data || data.type !== "minimax-h3-studio-task") return null;
        const res = await fetchApi("/minimax/studio/tasks/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ node_id: this.nodeId, data }),
        });
        if (!res.ok) return null;
        const out = await res.json();
        const tid = String(out.task_id);
        const ok = await this.loadTask(tid);
        return ok ? tid : null;
      } catch {
        return null;
      }
    },
  },
});

/** 素材序列化：只保留后端需要的 path + kind（预览数据不进契约）。
 *  参考列表紧凑无空位：编号 = 下标 + 1，直接逐项映射。 */
function toMediaPayload(m: ReferenceMedia): { path: string; kind: ReferenceMedia["kind"] } {
  return { path: m.path || m.name, kind: m.kind };
}

/** 片段基础字段（DB 行与后端 payload 共用） */
function clipBaseFields(s: Clip): Omit<ClipPayload, "pipeline"> {
  const refImages = (s.refImages ?? []).map(toMediaPayload);
  const refVideos = (s.refVideos ?? []).map(toMediaPayload);
  const refAudios = (s.refAudios ?? []).map(toMediaPayload);
  return {
    id: s.id,
    mode: s.mode,
    prompt: s.prompt,
    durationSec: s.durationSec,
    enabled: s.enabled,
    ...(s.continuity ? { continuity: s.continuity } : {}),
    ...(s.sampleFp ? { sampleFp: s.sampleFp } : {}),
    ...(s.firstFrame ? { firstFrame: toMediaPayload(s.firstFrame) } : {}),
    ...(s.lastFrame ? { lastFrame: toMediaPayload(s.lastFrame) } : {}),
    ...(refImages.length ? { refImages } : {}),
    ...(refVideos.length ? { refVideos } : {}),
    ...(refAudios.length ? { refAudios } : {}),
    ...(s.sourceVideo ? { sourceVideo: toMediaPayload(s.sourceVideo) } : {}),
  };
}

/**
 * DB 草稿行：**只存引用**（`pipelineId`），定义在流程库（`pipelines`）里一处；
 * 摊平图是派生物，绝不落草稿。
 */
function toClipDbRow(s: Clip): Record<string, unknown> {
  return {
    ...clipBaseFields(s),
    ...(s.pipelineId ? { pipelineId: s.pipelineId } : {}),
  };
}

/**
 * 发给后端的 payload 行：把引用**解析**成具体流程（`{id, name, graph}`）——
 * 后端只认摊平图，不认识流程库；引用悬空（流程被删）时按"无流程"发（走内置官方链）。
 */
function toClipPayload(s: Clip, pipelines: PipelineLibraryEntry[]): ClipPayload {
  const entry = s.pipelineId ? pipelines.find((p) => p.id === s.pipelineId) : undefined;
  const pipeline: ClipPipelinePayload | undefined = entry
    ? { id: entry.id, name: entry.name, graph: flattenClipPipeline(entry) }
    : undefined;
  return {
    ...clipBaseFields(s),
    ...(pipeline ? { pipeline } : {}),
  } as ClipPayload;
}

/** 反序列化：契约数据 → UI 数据（name 用 path 兜底显示；图片按 path 重建预览 URL） */
function fromMediaPayload(m: { path: string; kind: ReferenceMedia["kind"] }): ReferenceMedia {
  const media: ReferenceMedia = { name: m.path, kind: m.kind, path: m.path };
  // preview 不进数据契约，加载后按 ComfyUI /view 重建（path 可能含子目录）；统一走 webp 缩略图省带宽
  if (m.kind === "image" && m.path) {
    const parts = m.path.split("/");
    const filename = parts.pop() ?? m.path;
    const subfolder = parts.join("/");
    const params = new URLSearchParams({ filename, type: "input", preview: "webp" });
    if (subfolder) params.set("subfolder", subfolder);
    media.preview = `/view?${params.toString()}`;
  }
  return media;
}

/**
 * DB 行 → 片段。流程迁移：旧数据把定义内联在 `pipeline.def` 里（引用模型之前），
 * 这里把它登记进流程库并改成 `pipelineId` 引用（迁移逻辑在调用方，见 `migrateLegacyPipelines`）。
 */
function fromClipPayload(s: ClipPayload & { pipelineId?: string | null }): Clip {
  return {
    id: s.id,
    mode: s.mode,
    prompt: s.prompt,
    // 提示词条目快照无时长（规格随样本）：恢复卡片/回填时默认 4s，锁定样本时由样本时长覆盖
    durationSec: s.durationSec ?? 4,
    // 提示词条目快照无 enabled（执行态不属于内容）：恢复卡片/回填时默认参与生成
    enabled: s.enabled ?? true,
    ...(s.continuity ? { continuity: s.continuity } : {}),
    ...(s.sampleFp ? { sampleFp: s.sampleFp } : {}),
    // 引用形态（新数据）：只保留 id，定义在流程库里
    ...(s.pipelineId ? { pipelineId: String(s.pipelineId) } : {}),
    // 旧内联形态：先按 id 记下引用，定义由 migrateLegacyPipelines() 收进流程库
    ...(!s.pipelineId && s.pipeline?.id
      ? { pipelineId: String(s.pipeline.id) }
      : {}),
    ...(s.firstFrame ? { firstFrame: fromMediaPayload(s.firstFrame) } : {}),
    ...(s.lastFrame ? { lastFrame: fromMediaPayload(s.lastFrame) } : {}),
    ...(s.refImages?.length ? { refImages: s.refImages.map(fromMediaPayload) } : {}),
    ...(s.refVideos?.length ? { refVideos: s.refVideos.map(fromMediaPayload) } : {}),
    ...(s.refAudios?.length ? { refAudios: s.refAudios.map(fromMediaPayload) } : {}),
    ...(s.sourceVideo ? { sourceVideo: fromMediaPayload(s.sourceVideo) } : {}),
  };
}

/** 从 DB 行里捞出旧的内联流程定义（迁移用；返回去重后的条目） */
function legacyPipelinesFromRows(rows: Record<string, unknown>[]): PipelineLibraryEntry[] {
  const out: PipelineLibraryEntry[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const inline = (row as { pipeline?: { id?: string; name?: string; def?: unknown } }).pipeline;
    const id = inline?.id ? String(inline.id) : "";
    if (!id || seen.has(id) || inline?.def === undefined) continue;
    seen.add(id);
    out.push({ id, name: String(inline.name || `流程 ${out.length + 1}`), def: inline.def });
  }
  return out;
}

/** 画面语义快照 → 片段当前内容。执行态/编排（enabled/continuity）与锁定（sampleFp）保持不动
 *  ——它们是当前编排/工艺，不随内容恢复；素材槽整体替换（快照是当时的完整画面语义）。
 *  时长不随条目恢复（规格随采样记录），由启用采样（applySample）按样本时长覆盖。 */
function applySnapshotTo(seg: Clip, snap: PromptSnapshot): void {
  seg.mode = snap.mode;
  seg.prompt = snap.prompt;
  if (snap.firstFrame) seg.firstFrame = fromMediaPayload(snap.firstFrame);
  else delete seg.firstFrame;
  if (snap.lastFrame) seg.lastFrame = fromMediaPayload(snap.lastFrame);
  else delete seg.lastFrame;
  seg.refImages = (snap.refImages ?? []).map(fromMediaPayload);
  seg.refVideos = (snap.refVideos ?? []).map(fromMediaPayload);
  seg.refAudios = (snap.refAudios ?? []).map(fromMediaPayload);
  if (snap.sourceVideo) seg.sourceVideo = fromMediaPayload(snap.sourceVideo);
  else delete seg.sourceVideo;
}
