import { defineStore } from "pinia";
import {
  type CanvasConfig,
  type StudioPayload,
  type ReferenceMedia,
  type Clip,
  type ClipHistory,
  type ClipPayload,
  type PromptSnapshot,
  type VersionSample,
  DURATION_LIMITS,
} from "@/types/timeline";

/** 生成片段 id（避免与随机碰撞） */
function createId(): string {
  return `clip_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
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

    /** 序列化为发给后端的数据负载（数据契约出口） */
    serialize(): StudioPayload {
      return {
        version: 1,
        canvas: { ...this.canvas },
        clips: this.clips.map(toClipPayload),
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
    },

    setNodeId(nodeId: string): void {
      this.nodeId = nodeId;
    },

    /** 新建空任务（清空当前时间线并创建新任务记录），创建后立即落库 */
    async newTask(nodeId: string, name = ""): Promise<string | null> {
      this.clips = [];
      this.canvas = { fps: 24, width: 864, height: 480 };
      this.selectedId = null;
      this.historyByClipId = {}; // 新任务无历史
      const tid = await this.createTask(nodeId, name);
      if (tid) await this.saveToDb(); // DB 里始终有合法 payload（避免 executor 读到空时间线）
      return tid;
    },

    /** 卸载当前任务（删除后回到"未加载任务"的待加载界面）：
     *  清空时间线 + taskId，不创建任何新任务。 */
    unloadTask(): void {
      this.clips = [];
      this.canvas = { fps: 24, width: 864, height: 480 };
      this.selectedId = null;
      this.taskId = null;
      this.taskName = "";
      this.historyByClipId = {};
      this.samplingProgress = null;
    },

    /** 从 DB 加载任务：timeline（时间线当前数据 = canvas + clips[]，每 clip 含完整参数草稿）。
     *  片段当前数据独立于历史（草稿不丢，崩溃/刷新恢复）；历史版本仅作反悔来源。 */
    async loadTask(taskId: string): Promise<boolean> {
      try {
        const res = await fetchApi(`/minimax/studio/tasks/${encodeURIComponent(taskId)}`);
        if (!res.ok) return false;
        const data = await res.json();
        let seq: {
          canvas?: CanvasConfig;
          clips?: Record<string, unknown>[];
        } = {};
        try {
          seq = JSON.parse(data.timeline || "{}");
        } catch {
          seq = {};
        }
        if (seq.canvas) this.canvas = { ...seq.canvas };
        // 直接恢复每 clip 的当前参数草稿（timeline 是权威，不指向历史）
        this.clips = (seq.clips ?? []).map((c) => fromClipPayload(c as unknown as ClipPayload));
        this.selectedId = this.clips[0]?.id ?? null;
        this.taskId = taskId;
        this.taskName = data.name ?? "";
        await this.fetchHistory(); // 历史（纯 Model）拉取，供反悔展示
        return true;
      } catch {
        return false;
      }
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
     *  片段当前数据独立于历史（草稿不丢，崩溃/刷新恢复）；历史版本由采样固化。 */
    async saveToDb(): Promise<boolean> {
      if (!this.taskId) return false;
      const seq = {
        version: 1,
        canvas: { ...this.canvas },
        clips: this.clips.map((c) => toClipPayload(c)),
        totalDurationSec: this.totalDurationSec,
      };
      try {
        const res = await fetchApi(
          `/minimax/studio/tasks/${encodeURIComponent(this.taskId)}/timeline`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ timeline: JSON.stringify(seq) }),
          },
        );
        return res.ok;
      } catch {
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

function toClipPayload(s: Clip): ClipPayload {
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

function fromClipPayload(s: ClipPayload): Clip {
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
    ...(s.firstFrame ? { firstFrame: fromMediaPayload(s.firstFrame) } : {}),
    ...(s.lastFrame ? { lastFrame: fromMediaPayload(s.lastFrame) } : {}),
    ...(s.refImages?.length ? { refImages: s.refImages.map(fromMediaPayload) } : {}),
    ...(s.refVideos?.length ? { refVideos: s.refVideos.map(fromMediaPayload) } : {}),
    ...(s.refAudios?.length ? { refAudios: s.refAudios.map(fromMediaPayload) } : {}),
    ...(s.sourceVideo ? { sourceVideo: fromMediaPayload(s.sourceVideo) } : {}),
  };
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
