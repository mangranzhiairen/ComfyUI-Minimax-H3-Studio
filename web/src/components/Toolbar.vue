<script setup lang="ts">
import { computed, h, ref } from "vue";
import { useMessage } from "naive-ui";
import { storeToRefs } from "pinia";
import { useTimelineStore } from "@/stores/timeline";
import ResolutionParam from "./ResolutionParam.vue";
import { palette } from "@/styles/theme";

const store = useTimelineStore();
const message = useMessage();
const { clips, totalDurationSec, canvas, taskId, loadFailed, saveFailed } = storeToRefs(store);

/** 数据同步异常提示（加载失败=保留了本地内容；落库失败=改动还在内存，未写入 DB） */
const syncWarning = computed(() => {
  if (loadFailed.value) return "任务加载失败，已保留本地内容（可稍后重选任务）";
  if (saveFailed.value) return "时间线尚未写入数据库（改动仍在本地），请检查后端连接";
  return "";
});

function formatTotal(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}分${String(s).padStart(2, "0")}秒`;
}

function onAdd() {
  // 未加载任务：先弹新建任务（输入名称）→ 成功后自动添加片段；
  // 已加载任务（含刚新建的空任务）：直接添加，taskId 已绑定可正常落库。
  if (!store.taskId) {
    addAfterCreate = true;
    openNewTask();
    return;
  }
  store.addClip();
}

// ---------- 任务库（加载/新建/删除/切换，时间线唯一数据源在 SQLite） ----------

/** 任务条目（来自 SQLite 任务库） */
type TaskItem = { key: string; label: string };

const taskList = ref<TaskItem[]>([]);
const taskLabel = computed(() =>
  taskId.value ? store.taskName || `任务 ${taskId.value.slice(-6)}` : "选择任务",
);

// ---------- 统一图标（内联 SVG：造型一致、宽度固定，功能项文字自动对齐） ----------

/** Lucide 风格线性图标（24 视口，currentColor 描边） */
const ICON_BODY: Record<string, string> = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  pencil: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
  upload: '<path d="M12 15V3"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
  trash:
    '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  history: '<path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5"/>',
  layers: '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  clapper:
    '<path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
};

/** 功能项：图标名 + 文案（图标盒宽度统一 → 文案成列对齐） */
type TaskAction = {
  key: "new" | "rename" | "copy" | "export" | "import" | "delete";
  label: string;
  icon: string;
  danger: boolean;
  disabled: boolean;
};

const taskActions = computed<TaskAction[]>(() => [
  { key: "new", label: "新建任务", icon: "plus", danger: false, disabled: false },
  { key: "rename", label: "重命名当前任务", icon: "pencil", danger: false, disabled: !taskId.value },
  { key: "copy", label: "复制当前任务", icon: "copy", danger: false, disabled: !taskId.value },
  { key: "export", label: "导出当前任务", icon: "download", danger: false, disabled: !taskId.value },
  { key: "import", label: "导入任务（时间线 + 历史）", icon: "upload", danger: false, disabled: false },
  { key: "delete", label: "删除当前任务", icon: "trash", danger: true, disabled: !taskId.value },
]);

/** 内联 SVG 图标组件（15px，盒宽写死 → 同一容器内文字起点一致） */
const TbIcon = (
  props: { name?: string; class?: string },
  ctx?: { attrs?: Record<string, unknown> },
) => {
  // 兼容函数式组件的两种传参：无声明 props 时 name/class 落在 attrs 里
  const p = { ...(props ?? {}), ...((ctx?.attrs as Record<string, unknown>) ?? {}) } as {
    name?: string;
    class?: string;
  };
  return h("svg", {
    viewBox: "0 0 24 24",
    width: 15,
    height: 15,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.9,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    class: ["tb-ico", p.class ?? ""],
    innerHTML: ICON_BODY[p.name ?? ""] ?? "",
  });
};

/** 时间戳 → 可读时间（任务列表展示） */
function fmtTime(ts: unknown): string {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  const d = new Date(n * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function refreshTasks() {
  const list = await store.fetchTaskList();
  taskList.value = list.map((t) => ({
    key: String(t.task_id),
    label: (t.name as string) || fmtTime(t.created_at) || "未命名",
  }));
}

// ---------- 采样流程库（工具栏统一管理：新建/编辑/重命名/复制/删除 + 导入导出） ----------
// 片段上的 ⊞ 下拉只负责**选择**（默认官方 / 库里某一份）；这里管流程本身。

const showPipelineManager = ref(false);
const pipelineFileInput = ref<HTMLInputElement | null>(null);

/** 重命名：待改的流程 id + 输入值 */
const renamingPipelineId = ref<string | null>(null);
const pipelineNameInput = ref("");

/** 删除：待删的流程 id（连带引用它的卡片回默认流程） */
const deletingPipelineId = ref<string | null>(null);
const deletingPipeline = computed(() =>
  deletingPipelineId.value ? store.pipelines.find((p) => p.id === deletingPipelineId.value) ?? null : null,
);

function onNewPipeline() {
  const entry = store.createPipeline();
  showPipelineManager.value = false;
  const res = store.openPipelineEditor(entry.id);
  if (res.ok) message.success(`已新建流程：${entry.name}（在子图里搭好采样链后保存）`);
  else message.error(res.message);
}

function onEditPipeline(id: string) {
  showPipelineManager.value = false;
  const res = store.openPipelineEditor(id);
  if (res.ok) message.success(res.message);
  else message.error(res.message);
}

function startRenamePipeline(id: string) {
  renamingPipelineId.value = id;
  pipelineNameInput.value = store.pipelines.find((p) => p.id === id)?.name ?? "";
}

function confirmRenamePipeline() {
  const id = renamingPipelineId.value;
  const name = pipelineNameInput.value.trim();
  if (id && name) store.renamePipeline(id, store.uniquePipelineName(name));
  renamingPipelineId.value = null;
  void store.saveToDb();
}

function onDuplicatePipeline(id: string) {
  const copy = store.duplicatePipeline(id);
  if (copy) message.success(`已复制为新流程：${copy.name}`);
}

function onExportPipeline(id: string) {
  if (store.exportPipeline(id)) message.success("流程已导出（可分享给别人导入）");
  else message.error("导出失败：流程不存在");
}

function onImportPipelineFile(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = ""; // 允许重复导入同一个文件
  if (!file) return;
  void store.importPipelineFile(file).then((res) => {
    if (res.ok) message.success(res.message);
    else message.error(res.message);
  });
}

function confirmDeletePipeline() {
  const entry = deletingPipeline.value;
  deletingPipelineId.value = null;
  if (!entry) return;
  const affected = store.deletePipeline(entry.id);
  void store.saveToDb();
  message.warning(
    affected > 0
      ? `已删除流程「${entry.name}」，${affected} 张卡片回到默认官方流程`
      : `已删除流程「${entry.name}」`,
  );
}

// ---------- 一键把某份流程应用到所有卡片（批量绑定） ----------
// 引用模型下只改卡片的 pipelineId：定义仍只有一份，后续编辑该流程所有卡片一起生效。

/** 待应用的流程 id（先弹确认——会覆盖卡片已有的绑定，属于批量改写） */
const applyingPipelineId = ref<string | null>(null);
const applyingPipeline = computed(() =>
  applyingPipelineId.value
    ? store.pipelines.find((p) => p.id === applyingPipelineId.value) ?? null
    : null,
);

/** 影响面：卡片总数 / 绑定会被改写的张数（确认文案用） */
const applyImpact = computed(() => {
  const id = applyingPipelineId.value;
  const total = store.clips.length;
  if (!id) return { total, changed: 0 };
  return { total, changed: store.clips.filter((c) => (c.pipelineId ?? null) !== id).length };
});

function startApplyPipelineToAll(id: string) {
  if (!store.clips.length) {
    message.warning("当前任务还没有卡片——先在时间线上添加片段");
    return;
  }
  applyingPipelineId.value = id;
}

function confirmApplyPipelineToAll() {
  const entry = applyingPipeline.value;
  applyingPipelineId.value = null;
  if (!entry) return;
  const { changed, total } = store.applyPipelineToAll(entry.id);
  if (!changed) {
    message.info(`全部 ${total} 张卡片本就在用「${entry.name}」`);
    return;
  }
  message.success(`已把「${entry.name}」挂到全部 ${total} 张卡片（改写 ${changed} 张绑定）`);
}

// ---------- 全部取消：所有卡片回退默认官方流程（清引用，不删流程定义） ----------

/** 当前任务里**绑定了自定义流程**的卡片数（决定「全部取消」是否可用 / 文案） */
const boundClipCount = computed(() => store.clips.filter((c) => !!c.pipelineId).length);
const clearAllEnabled = computed(() => store.clips.length > 0 && boundClipCount.value > 0);
const clearAllTitle = computed(() => {
  if (!store.clips.length) return "当前任务还没有卡片";
  if (!boundClipCount.value) return "当前任务没有卡片在用自定义流程";
  return `清除当前任务全部卡片的流程绑定（${boundClipCount.value} 张），回退到默认官方采样流程（流程定义仍保留在库里）`;
});

/** 全部取消：二次确认（批量清绑定，属于批量改写） */
const showClearAllConfirm = ref(false);

function confirmClearAll() {
  showClearAllConfirm.value = false;
  const bound = boundClipCount.value;
  const { changed, total } = store.applyPipelineToAll(null); // null = 全回默认官方流程
  if (!changed) {
    message.info(`${total} 张卡片已全部是默认官方采样流程`);
    return;
  }
  message.success(`已取消 ${changed} 张卡片的流程绑定，全部回退到默认官方采样流程`);
  console.info(`[StudioConsole] 全部取消采样流程：${changed}/${total} 张卡片回默认（原有绑定 ${bound} 张）`);
}

// ---------- 新建 / 重命名 / 复制（弹名称输入，强制非空） ----------

const showNameModal = ref(false);
const nameMode = ref<"new" | "rename" | "copy">("new");
const nameInput = ref("");
/** 新建任务确认成功后是否自动添加一个片段（未加载任务时点「＋ 片段」进入） */
let addAfterCreate = false;

/** 取消/关闭名称弹窗：清掉待添加标志（不自动添加片段） */
function cancelNameModal() {
  addAfterCreate = false;
  showNameModal.value = false;
}

function openNewTask() {
  nameMode.value = "new";
  nameInput.value = "";
  showNameModal.value = true;
}

function openRename() {
  nameMode.value = "rename";
  nameInput.value = store.taskName || "";
  showNameModal.value = true;
}

/** 复制当前任务：预填「原名 副本」并保证总长 ≤ 18（输入框 maxlength） */
function openCopy() {
  nameMode.value = "copy";
  const suffix = " 副本";
  const base = store.taskName || "";
  nameInput.value = base.slice(0, 18 - suffix.length);
  if (nameInput.value) nameInput.value += suffix;
  showNameModal.value = true;
}

async function confirmName() {
  const name = nameInput.value.trim();
  if (!name) return; // 强制：空名称不允许创建/重命名/复制
  if (nameMode.value === "new") {
    const tid = await store.newTask(store.nodeId, name); // 清空时间线 + 创建新任务
    if (tid && addAfterCreate) store.addClip(); // 新建成功后自动添加片段（编辑保存现在有任务可落库）
  } else if (nameMode.value === "copy" && store.taskId) {
    await store.saveToDb(); // 复制读 DB 源任务：先把最新草稿落库
    const tid = await store.duplicateTask(store.taskId, name); // DB 深拷贝，成功后自动加载副本
    if (tid) message.success(`已复制为「${store.taskName || tid}」`);
    else message.warning("复制失败（后端不可用或任务异常）");
  } else if (store.taskId) {
    await store.renameTask(store.taskId, name);
  }
  addAfterCreate = false;
  showNameModal.value = false;
  void refreshTasks(); // 操作后刷新下拉列表（新建/重命名/复制立即反映）
}

// ---------- 删除任务（二次确认，支持列表任意任务） ----------

const showDeleteConfirm = ref(false);
/** 待删除的任务 id（null = 删除当前任务） */
const pendingDeleteId = ref<string | null>(null);

async function confirmDelete() {
  showDeleteConfirm.value = false;
  const tid = pendingDeleteId.value ?? store.taskId;
  pendingDeleteId.value = null;
  if (!tid) return;
  const ok = await store.deleteTask(tid);
  if (ok) {
    if (store.taskId === tid) store.unloadTask(); // 删除的是当前任务 → 回待加载
    void refreshTasks(); // 立即刷新下拉列表（删除的任务不再出现）
  }
}

// ---------- 任务导入导出（导出当前任务 / 导入任务文件为新任务） ----------

const fileInput = ref<HTMLInputElement | null>(null);

async function onExport() {
  if (!store.taskId) return;
  const ok = await store.exportTask(store.taskId);
  if (!ok) message.warning("导出失败（后端不可用或任务异常）");
}

/** 选择导出文件 → 导入为新任务并加载 */
async function onImportFile(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = ""; // 允许重复选择同一文件
  if (!file) return;
  const tid = await store.importTaskFile(file);
  if (tid) {
    message.success(`已导入并加载「${store.taskName || tid}」`);
    void refreshTasks();
  } else {
    message.warning("导入失败：文件不是有效的创意工作台任务导出（或后端不可用）");
  }
}

/** 任务面板显示状态（自定义面板：任务列表 + 功能菜单） */
const showTaskMenu = ref(false);

/** 选择任务：切换前自动保存当前任务，切换后刷新当前标记 */
async function selectTask(key: string) {
  showTaskMenu.value = false;
  if (key === store.taskId) return;
  await store.saveToDb(); // 切换前自动保存当前任务
  await store.loadTask(key);
  void refreshTasks(); // 刷新（当前标记更新）
}

/** 功能菜单：新建/重命名/复制/导出/导入/删除 */
async function runTaskAction(key: TaskAction["key"]) {
  if (key === "new") openNewTask();
  else if (key === "rename") openRename();
  else if (key === "copy") openCopy();
  else if (key === "export") await onExport();
  else if (key === "import") fileInput.value?.click(); // 触发系统文件框，面板继续关闭
  else if (key === "delete" && store.taskId) {
    pendingDeleteId.value = null; // 删除当前任务
    showDeleteConfirm.value = true;
  }
  showTaskMenu.value = false;
}

/** 任务条目内的删除按钮（可删除列表中任意任务） */
function askDeleteTask(key: string) {
  pendingDeleteId.value = key;
  showDeleteConfirm.value = true;
}

function onTaskShow(show: boolean) {
  if (show) void refreshTasks();
}

// ---------- 画布参数（fps / 分辨率） ----------

function patchCanvas(p: Record<string, number>) {
  const cleared = store.updateCanvas(p);
  if (cleared) message.warning("画布已变，已勾选的 latent 缓存将失效，请重新采样");
}
</script>

<template>
  <div class="toolbar">
    <div class="toolbar-left">
      <span class="toolbar-title"><TbIcon name="clapper" /> 创意工作台</span>
      <span class="toolbar-sub">{{ clips.length }} 个片段</span>
    </div>

    <div class="toolbar-right">
      <!-- 数据同步异常提示（不阻断编辑：内容始终保留在本地/会话快照里） -->
      <span v-if="syncWarning" class="tb-warn" :title="syncWarning">⚠ 数据同步异常</span>

      <!-- 任务库：任务列表（最多 5 行，超出内部滚动）+ 功能菜单（数据源在 SQLite） -->
      <n-popover
        v-model:show="showTaskMenu"
        trigger="click"
        placement="bottom-end"
        :show-arrow="true"
        @update:show="onTaskShow"
      >
        <template #trigger>
          <button
            class="tb-btn ghost"
            :title="store.taskName || taskId || '选择任务'"
          >
            <TbIcon name="film" />
            {{ taskLabel }}
            <TbIcon name="chevron" class="tb-chevron" />
          </button>
        </template>

        <div class="tk-panel">
          <div class="tk-head">
            <span>任务库</span>
            <span class="tk-count">{{ taskList.length }} 个</span>
          </div>

          <!-- 任务列表：最多显示 5 行，超过则容器内滚动，不再撑高整个面板 -->
          <div class="tk-list" :class="{ 'tk-list--scroll': taskList.length > 5 }">
            <div v-if="!taskList.length" class="tk-empty">（暂无任务）</div>
            <button
              v-for="t in taskList"
              :key="t.key"
              class="tk-item"
              :class="{ active: t.key === taskId }"
              :title="t.label"
              @click="selectTask(t.key)"
            >
              <TbIcon name="film" class="tk-item-ico" />
              <span class="tk-item-name">{{ t.label }}</span>
              <span class="tk-del" title="删除该任务" @click.stop="askDeleteTask(t.key)">
                <TbIcon name="trash" />
              </span>
            </button>
          </div>

          <div class="tk-divider"></div>

          <!-- 功能菜单：图标盒宽度统一 → 文案同一列对齐 -->
          <div class="tk-actions">
            <button
              v-for="a in taskActions"
              :key="a.key"
              class="tk-action"
              :class="{ danger: a.danger }"
              :disabled="a.disabled"
              @click="runTaskAction(a.key)"
            >
              <TbIcon :name="a.icon" class="tk-action-ico" />
              <span class="tk-action-label">{{ a.label }}</span>
            </button>
          </div>
        </div>
      </n-popover>
      <!-- 导入任务文件选择（隐藏 input，由菜单项触发） -->
      <input ref="fileInput" type="file" accept=".json,application/json" style="display: none" @change="onImportFile" />

      <!-- 从历史恢复片段：手动挑选（有任务时可打开面板，向时间线追加恢复卡片） -->
      <button
        v-if="taskId"
        class="tb-btn ghost"
        title="从当前任务的历史版本快照中手动挑选片段恢复到时间线"
        @click="store.openRestoreModal()"
      ><TbIcon name="history" /> 恢复片段</button>

      <!-- 采样流程库：新建/编辑/重命名/复制/删除 + 导入导出
           （片段的流程选择在卡片 ⊞ 下拉里） -->
      <n-popover
        v-model:show="showPipelineManager"
        trigger="click"
        placement="bottom-end"
        :show-arrow="true"
      >
        <template #trigger>
          <button
            class="tb-btn ghost"
            :title="`自定义采样流程（${store.pipelines.length} 份）：新建/编辑/导入导出；片段上点 ⊞ 选用`"
          ><TbIcon name="layers" /> 自定义采样流程{{ store.pipelines.length ? ` ${store.pipelines.length}` : "" }}</button>
        </template>

        <div class="pl-panel">
          <div class="pl-head">
            自定义采样流程
            <span class="pl-hint">片段上点 ⊞ 选择，「全用」挂到所有卡片</span>
          </div>

          <div v-if="!store.pipelines.length" class="pl-empty">
            还没有流程 —— 点「＋ 新建流程」搭一份，或「导入…」用别人分享的
          </div>

          <div v-else class="pl-list">
            <div v-for="p in store.pipelines" :key="p.id" class="pl-row">
              <div class="pl-row-main">
                <span class="pl-name" :title="p.name">{{ p.name }}</span>
                <span class="pl-use">{{ store.pipelineUsage(p.id) }} 张卡片引用</span>
              </div>
              <div class="pl-row-actions">
                <button
                  class="pl-btn apply"
                  :disabled="!store.clips.length"
                  :title="
                    store.clips.length
                      ? `把「${p.name}」挂到当前任务的全部 ${store.clips.length} 张卡片（覆盖已有绑定）`
                      : '当前任务还没有卡片'
                  "
                  @click="startApplyPipelineToAll(p.id)"
                >全用</button>
                <button class="pl-btn" title="打开原生子图编辑器" @click="onEditPipeline(p.id)">编辑</button>
                <button class="pl-btn" title="重命名" @click="startRenamePipeline(p.id)">改名</button>
                <button class="pl-btn" title="复制为新流程（要独立改一份时用）" @click="onDuplicatePipeline(p.id)">复制</button>
                <button class="pl-btn" title="导出为流程文件（别人可导入）" @click="onExportPipeline(p.id)">导出</button>
                <button
                  class="pl-btn danger"
                  title="删除（引用它的卡片回到默认官方流程）"
                  @click="deletingPipelineId = p.id"
                >删除</button>
              </div>
            </div>
          </div>

          <div class="pl-footer">
            <button class="pl-btn primary" @click="onNewPipeline">＋ 新建流程</button>
            <button class="pl-btn" title="导入流程文件（.studio-pipeline.json）" @click="pipelineFileInput?.click()">
              导入…
            </button>
            <button
              class="pl-btn danger clear-all"
              :disabled="!clearAllEnabled"
              :title="clearAllTitle"
              @click="showClearAllConfirm = true"
            >全部取消</button>
          </div>
        </div>
      </n-popover>
      <input
        ref="pipelineFileInput"
        type="file"
        accept=".json,application/json"
        style="display: none"
        @change="onImportPipelineFile"
      />

      <!-- 画布参数（分辨率/帧率）选择器：按钮显示 WxH@fps，点击弹出选择框 -->
      <ResolutionParam
        :width="canvas.width"
        :height="canvas.height"
        :fps="canvas.fps"
        @apply="(v: { width: number; height: number; fps: number }) => patchCanvas(v)"
      />

      <span class="tb-total">总时长 {{ formatTotal(totalDurationSec) }}</span>
      <button class="tb-btn accent" title="添加片段" @click="onAdd"><TbIcon name="plus" /> 片段</button>
    </div>

    <!-- 新建/重命名任务：名称输入（强制非空，空名称禁用确定） -->
    <n-modal
      :show="showNameModal"
      preset="dialog"
      :title="nameMode === 'new' ? '新建任务' : nameMode === 'rename' ? '重命名任务' : '复制任务'"
      :positive-text="'确定'"
      :negative-text="'取消'"
      :positive-button-props="{ disabled: !nameInput.trim() }"
      @positive-click="confirmName"
      @negative-click="cancelNameModal"
      @close="cancelNameModal"
    >
      <p v-if="nameMode === 'new' && addAfterCreate" class="name-hint">
        当前未加载任务——创建后会自动添加一个片段到时间线
      </p>
      <p v-else-if="nameMode === 'copy'" class="name-hint">
        复制为独立新任务（时间线 + 提示词历史），不携带 latent 缓存，需重新采样；原任务保持不变
      </p>
      <n-input
        v-model:value="nameInput"
        placeholder="请输入任务名称（必填，最多 18 字符）"
        maxlength="18"
        show-count
        @keydown.enter="confirmName"
      />
    </n-modal>

    <!-- 重命名自定义采样流程 -->
    <n-modal
      :show="!!renamingPipelineId"
      preset="dialog"
      title="重命名自定义采样流程"
      positive-text="保存"
      negative-text="取消"
      @positive-click="confirmRenamePipeline"
      @negative-click="renamingPipelineId = null"
      @close="renamingPipelineId = null"
    >
      <n-input
        v-model:value="pipelineNameInput"
        placeholder="流程名称"
        @keyup.enter="confirmRenamePipeline"
      />
    </n-modal>

    <!-- 删除自定义采样流程：二次确认（引用它的卡片会回默认官方流程） -->
    <n-modal
      :show="!!deletingPipelineId"
      preset="dialog"
      title="删除自定义采样流程"
      :content="`将删除流程「${deletingPipeline?.name ?? ''}」的定义（${
        deletingPipeline ? store.pipelineUsage(deletingPipeline.id) : 0
      } 张卡片正在引用），这些片段会回到默认官方采样流程。不可恢复。确定删除？`"
      positive-text="删除"
      negative-text="取消"
      @positive-click="confirmDeletePipeline"
      @negative-click="deletingPipelineId = null"
      @close="deletingPipelineId = null"
    />

    <!-- 一键把流程应用到所有卡片：二次确认（会覆盖卡片已有的绑定） -->
    <n-modal
      :show="!!applyingPipelineId"
      preset="dialog"
      title="应用到所有卡片"
      :content="`将把流程「${applyingPipeline?.name ?? ''}」挂到当前任务的全部 ${
        applyImpact.total
      } 张卡片（其中 ${applyImpact.changed} 张的绑定会被改写）。定义只有一份，之后编辑该流程所有卡片一起生效。确定？`"
      positive-text="全部应用"
      negative-text="取消"
      @positive-click="confirmApplyPipelineToAll"
      @negative-click="applyingPipelineId = null"
      @close="applyingPipelineId = null"
    />

    <!-- 全部取消：所有卡片回退默认官方流程（只清绑定，不删流程定义） -->
    <n-modal
      :show="showClearAllConfirm"
      preset="dialog"
      title="全部取消采样流程"
      :content="`将清除当前任务全部卡片的流程绑定（${boundClipCount} 张在用自定义流程），回退到默认官方采样流程。流程定义仍保留在库里，之后可以再挂上。确定？`"
      positive-text="全部取消"
      negative-text="返回"
      @positive-click="confirmClearAll"
      @negative-click="showClearAllConfirm = false"
      @close="showClearAllConfirm = false"
    />

    <!-- 删除任务：二次确认 -->
    <n-modal
      :show="showDeleteConfirm"
      preset="dialog"
      title="删除任务"
      content="将删除该任务的时间线与所有 latent 缓存，不可恢复。确定删除？"
      :positive-text="'删除'"
      :negative-text="'取消'"
      @positive-click="confirmDelete"
      @negative-click="showDeleteConfirm = false"
      @close="showDeleteConfirm = false"
    />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: var(--dc-panel);
  border-radius: 8px;
  border: 1px solid var(--dc-border);
  gap: 12px;
}

.toolbar-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.toolbar-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--dc-text);
  white-space: nowrap;
}
.toolbar-sub {
  font-size: 12px;
  color: var(--dc-text-faint);
  white-space: nowrap;
}

.toolbar-center {
  display: none;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tb-total {
  font-size: 12px;
  color: var(--dc-text-dim);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* 数据同步异常提示（加载/落库失败；悬停看原因） */
.tb-warn {
  font-size: 12px;
  line-height: 1;
  padding: 5px 8px;
  border-radius: 6px;
  color: #fcd34d;
  background: rgba(251, 191, 36, 0.12);
  border: 1px solid rgba(251, 191, 36, 0.35);
  white-space: nowrap;
  cursor: help;
}

.tb-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--dc-border);
  background: var(--dc-bg);
  color: var(--dc-text);
  font-size: 13px;
  line-height: 1;
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.12s ease;
  white-space: nowrap;
}
/* 统一图标：固定 15px，收缩不参与压缩 → 同一容器内文字起点一致 */
.tb-ico {
  flex: 0 0 auto;
  display: block;
  opacity: 0.9;
}
.tb-chevron {
  opacity: 0.55;
}
.tb-btn:hover {
  border-color: v-bind("palette.accent");
  color: v-bind("palette.accentHover");
}
.tb-btn.accent {
  background: v-bind("palette.accent");
  border-color: v-bind("palette.accent");
  color: #0f172a;
  font-weight: 600;
}
.tb-btn.accent:hover {
  background: v-bind("palette.accentHover");
  color: #0f172a;
}
/* 新建任务前提示（未加载任务时点＋片段进入） */
.name-hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--dc-text-dim);
}
.tb-btn.ghost {
  background: transparent;
}

/* ---------- 任务库面板（任务列表 + 功能菜单；列表最多 5 行后内部滚动） ---------- */
.tk-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px;
  width: 280px;
}
.tk-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 12px;
  color: var(--dc-text);
}
.tk-count {
  font-size: 10px;
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
}
/* 任务区：最多 5 行，超出只滚动列表，不把面板顶下去 */
.tk-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: calc(5 * 34px);
  overflow-y: auto;
  overflow-x: hidden;
}
.tk-list--scroll {
  padding-right: 2px;
}
.tk-empty {
  padding: 8px;
  font-size: 11px;
  color: var(--dc-text-dim);
  text-align: center;
}
.tk-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 32px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--dc-text-dim);
  font-size: 12px;
  line-height: 1;
  text-align: left;
  cursor: pointer;
}
.tk-item:hover {
  background: rgba(255, 255, 255, 0.06);
  color: var(--dc-text);
}
.tk-item.active {
  background: var(--dc-accent-dim);
  border-color: var(--dc-accent);
  color: var(--dc-text);
}
.tk-item-ico {
  flex: 0 0 auto;
  opacity: 0.65;
}
.tk-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 删除按钮：默认隐藏，悬停任务行才浮现，避免列表噪音 */
.tk-del {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 4px;
  opacity: 0;
  transition: opacity 0.12s ease, color 0.12s ease, background 0.12s ease;
}
.tk-item:hover .tk-del {
  opacity: 0.6;
}
.tk-del:hover {
  opacity: 1;
  color: var(--dc-danger);
  background: rgba(248, 113, 113, 0.14);
}
.tk-divider {
  height: 1px;
  margin: 0 -2px;
  background: var(--dc-border);
}
.tk-actions {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tk-action {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--dc-text-dim);
  font-size: 12px;
  line-height: 1;
  text-align: left;
  cursor: pointer;
}
.tk-action:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.06);
  color: var(--dc-text);
}
.tk-action:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.tk-action.danger:hover:not(:disabled) {
  color: var(--dc-danger);
  background: rgba(248, 113, 113, 0.12);
}
/* 图标盒统一宽度（所有功能项文字从同一列开始） */
.tk-action-ico {
  flex: 0 0 15px;
  opacity: 0.8;
}
.tk-action-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 采样流程库面板（工具栏弹出；样式对齐 ResolutionParam 的 .res-pop 一套） ---------- */
.pl-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 2px;
  width: 400px;
}
.pl-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--dc-text);
}
.pl-hint {
  margin-left: auto;
  font-size: 10px;
  color: var(--dc-text-faint);
}
.pl-empty {
  padding: 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--dc-text-dim);
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--dc-border);
  border-radius: 5px;
}
.pl-list {
  display: flex;
  flex-direction: column;
  max-height: 46vh;
  overflow-y: auto;
}
.pl-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-top: 1px solid var(--dc-border);
}
.pl-list .pl-row:first-child {
  border-top: none;
}
.pl-row-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-width: 0;
}
.pl-name {
  font-size: 12px;
  color: var(--dc-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pl-use {
  font-size: 10px;
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
}
.pl-row-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}
.pl-btn {
  height: 22px;
  padding: 0 8px;
  border: 1px solid var(--dc-border);
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--dc-text-dim);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  white-space: nowrap;
}
.pl-btn:hover {
  border-color: var(--dc-accent);
  background: rgba(255, 255, 255, 0.08);
  color: var(--dc-text);
}
/* 批量应用：主要动作，视觉上略强（但不用 primary 实心，避免与「＋ 新建流程」抢焦点） */
.pl-btn.apply {
  border-color: var(--dc-accent);
  color: var(--dc-text);
}
.pl-btn:disabled,
.pl-btn:disabled:hover {
  opacity: 0.4;
  cursor: not-allowed;
  border-color: var(--dc-border);
  background: rgba(255, 255, 255, 0.04);
  color: var(--dc-text-faint);
}
.pl-btn.primary {
  border-color: transparent;
  background: v-bind("palette.accent");
  color: #fff;
}
.pl-btn.primary:hover {
  background: v-bind("palette.accentHover");
  opacity: 0.9;
}
.pl-btn.danger:hover {
  border-color: var(--dc-danger);
  color: var(--dc-danger);
}
.pl-footer {
  display: flex;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--dc-border);
}
/* 全部取消：批量撤销动作，靠右与「新建/导入」拉开距离（视觉上不易误点） */
.pl-btn.clear-all {
  margin-left: auto;
}
.pl-btn.clear-all:not(:disabled):hover {
  border-color: var(--dc-danger);
  color: var(--dc-danger);
}
</style>
