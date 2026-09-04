<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { MediaKind, ReferenceMedia } from "@/types/timeline";
import { palette } from "@/styles/theme";

const props = defineProps<{
  media?: ReferenceMedia;
  /** 槽位名称，如「首帧图」「参考视频」 */
  label: string;
  /** 文件类型限制 */
  kind: MediaKind;
  /** 附加说明，如数量上限 */
  hint?: string;
  /** 素材序号（编号 = 列表位置；参考素材网格用，无则隐藏） */
  indexLabel?: number;
}>();

const emit = defineEmits<{
  (e: "change", media: ReferenceMedia): void;
  (e: "remove"): void;
}>();

const fileInput = ref<HTMLInputElement | null>(null);
const pickerError = ref("");
/** 缩略图加载失败 = 引用的文件在 input 目录已不存在/被改名（如 webp 被替换成 jpg） */
const previewError = ref(false);
watch(
  () => props.media?.preview,
  () => {
    previewError.value = false; // 素材更换后复位，重新探测
  },
);

const KIND_META: Record<MediaKind, { icon: string; accept: string }> = {
  image: { icon: "🖼️", accept: "image/*" },
  video: { icon: "🎞️", accept: "video/*" },
  audio: { icon: "🎵", accept: "audio/*,video/*" },
};

/** 是否运行在 ComfyUI 中（有 window.app.api 则为集成模式；独立预览无） */
const inComfyUI = typeof window !== "undefined" && !!(window as { app?: { api?: unknown } }).app?.api;

/** ComfyUI 素材查看 URL（input 目录内文件）。UI 预览统一走 preview=webp 缩略图（≤512px），
 *  避免 15px 级小图却下载原图；原图始终可通过 media.path 单独访问。 */
function viewUrl(name: string, subfolder = ""): string {
  const params = new URLSearchParams({ filename: name, type: "input", preview: "webp" });
  if (subfolder) params.set("subfolder", subfolder);
  return `/view?${params.toString()}`;
}

/**
 * 上传到 ComfyUI input 目录（type=input），返回相对路径。
 * 独立预览模式（无 ComfyUI）返回本地文件名，不真正上传。
 */
async function uploadFile(file: File): Promise<{ path: string; preview?: string }> {
  if (!inComfyUI) {
    if (props.kind === "image") {
      const preview = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      return { path: file.name, preview };
    }
    return { path: file.name };
  }

  const api = (window as { app?: { api: { fetchApi: (url: string, init?: RequestInit) => Promise<Response> } } }).app!.api!;
  const endpoint = props.kind === "image" ? "/upload/image" : props.kind === "video" ? "/upload/video" : "/upload/audio";
  const body = new FormData();
  body.append("image", file, file.name);
  body.append("type", "input");
  body.append("overwrite", "false");

  const resp = await api.fetchApi(endpoint, { method: "POST", body });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(text || `上传失败 (${resp.status})`);
  }
  const data = (await resp.json()) as { name?: string; subfolder?: string; type?: string };
  const name = data.name || file.name;
  const subfolder = data.subfolder || "";
  const path = subfolder ? `${subfolder}/${name}` : name;
  return { path, preview: props.kind === "image" ? viewUrl(name, subfolder) : undefined };
}

function openPicker() {
  fileInput.value?.click();
}

async function onPick(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  pickerError.value = "";
  try {
    const { path, preview } = await uploadFile(file);
    emit("change", { name: path, kind: props.kind, path, ...(preview ? { preview } : {}) });
  } catch (err) {
    pickerError.value = err instanceof Error ? err.message : String(err);
  }
  input.value = "";
}

// ---------- 选已有素材 ----------

interface ExistingItem {
  name: string;
  relPath: string;
  subfolder: string;
  mediaKind: string;
  width?: number;
  height?: number;
}

const showPicker = ref(false);
const existingItems = ref<ExistingItem[]>([]);
const loadingItems = ref(false);

const listKind = computed(() => (props.kind === "audio" ? "reference_audio" : props.kind));

async function openExistingPicker() {
  showPicker.value = true;
  loadingItems.value = true;
  existingItems.value = [];
  try {
    const api = (window as { app?: { api: { fetchApi: (url: string) => Promise<Response> } } }).app!.api!;
    const resp = await api.fetchApi(`/minimax/studio/list_input_media?kind=${listKind.value}`);
    if (!resp.ok) throw new Error(`列表接口失败 (${resp.status})`);
    const data = (await resp.json()) as { items: ExistingItem[] };
    existingItems.value = data.items ?? [];
  } catch (err) {
    pickerError.value = err instanceof Error ? err.message : String(err);
  } finally {
    loadingItems.value = false;
  }
}

function pickExisting(item: ExistingItem) {
  const preview = item.mediaKind === "image" ? viewUrl(item.name, item.subfolder) : undefined;
  emit("change", {
    name: item.relPath,
    kind: item.mediaKind === "video" ? "video" : item.mediaKind === "audio" ? "audio" : "image",
    path: item.relPath,
    ...(preview ? { preview } : {}),
  });
  showPicker.value = false;
}

/** 替换：ComfyUI 内从 input 素材库挑选（不重复上传文件）；独立预览模式退回本地上传 */
function onReplace() {
  if (inComfyUI) openExistingPicker();
  else openPicker();
}

function onRemove(e: MouseEvent) {
  e.stopPropagation();
  emit("remove");
}

function itemThumb(item: ExistingItem): string | undefined {
  if (item.mediaKind !== "image") return undefined;
  return viewUrl(item.name, item.subfolder);
}

function itemIcon(item: ExistingItem): string {
  if (item.mediaKind === "video") return "🎞️";
  if (item.mediaKind === "audio") return "🎵";
  return "🖼️";
}
</script>

<template>
  <div class="slot" @click="openPicker">
    <input
      ref="fileInput"
      type="file"
      :accept="KIND_META[kind].accept"
      hidden
      @change="onPick"
    />

    <!-- 空槽位：方块 + 图标/文字 + 选已有 -->
    <div v-if="!media" class="slot-empty">
      <span class="slot-icon">{{ KIND_META[kind].icon }}</span>
      <span class="slot-label">{{ label }}</span>
      <span v-if="hint" class="slot-hint">{{ hint }}</span>
      <button
        v-if="inComfyUI"
        class="slot-existing"
        title="从 ComfyUI/input/ 选择已有素材"
        @click.stop="openExistingPicker"
      >
        选已有
      </button>
    </div>

    <!-- 已有素材：缩略图/图标 + 名字（截断，悬停显示全名） -->
    <!-- draggable=false：卡片拖动排序用 Pointer Events 实现（不走浏览器原生拖拽，原生拖拽会打断 pointer 序列） -->
    <div v-else class="slot-filled" :title="media.name" draggable="false">
      <div class="slot-thumb">
        <span v-if="indexLabel != null" class="slot-idx">{{ indexLabel }}</span>
        <img v-if="kind === 'image' && media.preview" :src="media.preview" class="slot-img" alt="" loading="lazy" draggable="false" @error="previewError = true" />
        <span v-else class="slot-icon big">{{ KIND_META[kind].icon }}</span>
        <!-- 缩略图加载失败 = 引用文件已不存在（被删除/改名）；⟳ 可从素材库重新选择 -->
        <div
          v-if="previewError"
          class="slot-broken"
          title="素材文件不存在（可能已在 input 目录被删除/改名），点 ⟳ 从素材库重新选择"
        >
          <span>文件缺失</span>
        </div>
        <div class="slot-actions" @click.stop>
          <button class="slot-btn" title="从素材库替换" @click="onReplace">⟳</button>
          <button class="slot-btn danger" title="移除素材（列表自动前移补位）" @click="onRemove">✕</button>
        </div>
      </div>
      <span class="slot-name">{{ media.name }}</span>
    </div>
  </div>

  <!-- 错误提示 -->
  <div v-if="pickerError" class="slot-error">{{ pickerError }}</div>

  <!-- 已有素材选择弹窗：方块格 -->
  <n-modal
    v-model:show="showPicker"
    preset="card"
    title="选择已有素材（ComfyUI/input/）"
    style="width: 600px; max-width: 94vw"
  >
    <div v-if="loadingItems" class="existing-loading">加载中…</div>
    <div v-else-if="!existingItems.length" class="existing-loading">input 目录暂无匹配素材</div>
    <div v-else class="existing-list">
      <div
        v-for="item in existingItems"
        :key="item.relPath"
        class="existing-item"
        :title="item.relPath"
        @click="pickExisting(item)"
      >
        <div class="existing-thumb">
          <img v-if="itemThumb(item)" :src="itemThumb(item)" alt="" loading="lazy" />
          <span v-else class="existing-icon">{{ itemIcon(item) }}</span>
        </div>
        <span class="existing-name">{{ item.relPath }}</span>
        <span v-if="item.width" class="existing-dims">{{ item.width }}×{{ item.height }}</span>
      </div>
    </div>
  </n-modal>
</template>

<style scoped>
/* ===== 方块素材槽 ===== */
.slot {
  position: relative;
  width: 104px;
  height: 116px;
  display: flex;
  flex-direction: column;
  border: 1px dashed var(--dc-border-strong);
  border-radius: 8px;
  cursor: pointer;
  overflow: hidden;
  background: var(--dc-panel);
  flex-shrink: 0;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.slot:hover {
  border-color: v-bind("palette.accent");
  background: v-bind("palette.accentDim");
}

/* 空槽 */
.slot-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  padding: 6px;
  min-height: 0;
  text-align: center;
}
.slot-icon {
  font-size: 16px;
  line-height: 1;
}
.slot-icon.big {
  font-size: 24px;
}
.slot-label {
  font-size: 11px;
  font-weight: 500;
  color: var(--dc-text);
  line-height: 1.3;
  word-break: break-all;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.slot-hint {
  font-size: 9px;
  color: var(--dc-text-faint);
  line-height: 1.2;
}
.slot-existing {
  margin-top: 4px;
  border: 1px solid var(--dc-border);
  background: transparent;
  color: v-bind("palette.accentHover");
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 4px;
  cursor: pointer;
  line-height: 1.5;
}
.slot-existing:hover {
  border-color: v-bind("palette.accent");
  background: v-bind("palette.accentDim");
}

/* 已有素材 */
.slot-filled {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.slot-thumb {
  flex: 1;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--dc-bg);
  min-height: 0;
}
.slot-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
/* 素材序号角标：编号 = 列表位置（紧跟排序/删除补位变化），区别于 hover 操作按钮 */
.slot-idx {
  position: absolute;
  top: 3px;
  left: 3px;
  z-index: 3;
  min-width: 15px;
  height: 15px;
  padding: 0 3px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1;
  color: #93c5fd;
  background: rgba(15, 23, 42, 0.82);
  border: 1px solid rgba(96, 165, 250, 0.35);
  border-radius: 4px;
  pointer-events: none;
}
/* 引用文件缺失：缩略图加载失败时遮罩提示（文件被删/改名，如 webp 换成了 jpg） */
.slot-broken {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(15, 23, 42, 0.78);
  pointer-events: none;
}
.slot-broken span {
  font-size: 11px;
  font-weight: 600;
  color: var(--dc-danger);
  border: 1px solid rgba(248, 113, 113, 0.5);
  border-radius: 4px;
  padding: 2px 6px;
  background: rgba(127, 29, 29, 0.35);
}
.slot-actions {
  position: absolute;
  top: 4px;
  right: 4px;
  display: flex;
  gap: 3px;
  opacity: 0;
  transition: opacity 0.12s ease;
}
.slot:hover .slot-actions {
  opacity: 1;
}
.slot-btn {
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 4px;
  background: rgba(15, 23, 42, 0.78);
  color: var(--dc-text);
  font-size: 10px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.slot-btn:hover {
  background: v-bind("palette.accent");
  color: #0f172a;
}
.slot-btn.danger:hover {
  background: v-bind("palette.danger");
  color: #fff;
}
.slot-name {
  padding: 4px 6px;
  font-size: 10px;
  color: var(--dc-text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
  border-top: 1px solid var(--dc-border);
}

.slot-error {
  font-size: 11px;
  color: var(--dc-danger);
  margin-top: 2px;
}

/* ===== 选已有弹窗：方块格 ===== */
.existing-loading {
  padding: 24px 0;
  text-align: center;
  color: var(--dc-text-faint);
  font-size: 13px;
}

.existing-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, 104px);
  gap: 8px;
  max-height: 46vh;
  overflow: auto;
}
.existing-item {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.existing-item:hover {
  border-color: v-bind("palette.accent");
  background: v-bind("palette.accentDim");
}
.existing-thumb {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--dc-bg);
  position: relative;
}
.existing-thumb img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.existing-icon {
  font-size: 22px;
}
.existing-name {
  padding: 3px 5px 1px;
  font-size: 10px;
  color: var(--dc-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
.existing-dims {
  padding: 0 5px 4px;
  font-size: 9px;
  color: var(--dc-text-faint);
}
</style>
