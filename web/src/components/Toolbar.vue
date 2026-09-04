<script setup lang="ts">
import { computed, h, ref } from "vue";
import { useMessage } from "naive-ui";
import { storeToRefs } from "pinia";
import { useTimelineStore } from "@/stores/timeline";
import ResolutionParam from "./ResolutionParam.vue";
import { palette } from "@/styles/theme";

const store = useTimelineStore();
const message = useMessage();
const { clips, totalDurationSec, canvas, taskId } = storeToRefs(store);

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

type TaskOption = {
  key: string;
  label: string;
  type?: "divider";
  disabled?: boolean;
  renderLabel?: (o: TaskOption) => unknown;
};

const taskList = ref<TaskOption[]>([]);
const taskLabel = computed(() =>
  taskId.value ? store.taskName || `任务 ${taskId.value.slice(-6)}` : "选择任务",
);

/** 任务项渲染：截断名称 + 删除按钮（点击删除任意任务） */
function renderTaskLabel(t: TaskOption) {
  return h(
    "div",
    { style: "display:flex;align-items:center;gap:8px;width:100%;justify-content:space-between" },
    [
      h(
        "span",
        { style: "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" },
        t.label,
      ),
      h(
        "span",
        {
          style: "cursor:pointer;opacity:.55;flex-shrink:0",
          title: "删除该任务",
          onClick: (e: MouseEvent) => {
            e.stopPropagation();
            pendingDeleteId.value = t.key;
            showDeleteConfirm.value = true;
          },
        },
        "🗑",
      ),
    ],
  );
}

const taskOptions = computed<TaskOption[]>(() => [
  ...(taskList.value.length
    ? taskList.value.map((t) => ({ ...t, renderLabel: renderTaskLabel }))
    : [{ key: "__empty__", label: "（暂无任务）", disabled: true }]),
  { key: "__div__", type: "divider", label: "" },
  { key: "__new__", label: "＋ 新建任务" },
  { key: "__rename__", label: "✏️ 重命名当前任务", disabled: !taskId.value },
  { key: "__copy__", label: "⧉ 复制当前任务", disabled: !taskId.value },
  { key: "__export__", label: "⬇ 导出当前任务", disabled: !taskId.value },
  { key: "__import__", label: "⬆ 导入任务（时间线 + 历史）" },
  { key: "__delete__", label: "🗑 删除当前任务", disabled: !taskId.value },
]);

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

async function onTaskSelect(key: string) {
  if (key === "__new__") {
    openNewTask();
  } else if (key === "__rename__") {
    openRename();
  } else if (key === "__copy__") {
    openCopy();
  } else if (key === "__export__") {
    await onExport();
  } else if (key === "__import__") {
    fileInput.value?.click();
  } else if (key === "__delete__") {
    if (store.taskId) {
      pendingDeleteId.value = null; // 删除当前任务
      showDeleteConfirm.value = true;
    }
  } else if (key !== store.taskId) {
    await store.saveToDb(); // 切换前自动保存当前任务
    await store.loadTask(key);
    void refreshTasks(); // 刷新（当前标记更新）
  }
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
      <span class="toolbar-title">🎬 创意工作台</span>
      <span class="toolbar-sub">{{ clips.length }} 个片段</span>
    </div>

    <div class="toolbar-right">
      <!-- 任务库：加载/新建/删除（时间线唯一数据源在 SQLite） -->
      <n-dropdown
        trigger="click"
        :options="taskOptions"
        @select="onTaskSelect"
        @update:show="onTaskShow"
      >
        <button
          class="tb-btn ghost"
          :title="store.taskName || taskId || '选择任务'"
        >{{ taskLabel }} ▾</button>
      </n-dropdown>
      <!-- 导入任务文件选择（隐藏 input，由菜单项触发） -->
      <input ref="fileInput" type="file" accept=".json,application/json" style="display: none" @change="onImportFile" />

      <!-- 从历史恢复片段：手动挑选（有任务时可打开面板，向时间线追加恢复卡片） -->
      <button
        v-if="taskId"
        class="tb-btn ghost"
        title="从当前任务的历史版本快照中手动挑选片段恢复到时间线"
        @click="store.openRestoreModal()"
      >↩ 恢复片段</button>

      <!-- 画布参数（分辨率/帧率）选择器：按钮显示 WxH@fps，点击弹出选择框 -->
      <ResolutionParam
        :width="canvas.width"
        :height="canvas.height"
        :fps="canvas.fps"
        @apply="(v: { width: number; height: number; fps: number }) => patchCanvas(v)"
      />

      <span class="tb-total">总时长 {{ formatTotal(totalDurationSec) }}</span>
      <button class="tb-btn accent" title="添加片段" @click="onAdd">＋ 片段</button>
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

.tb-btn {
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
</style>
