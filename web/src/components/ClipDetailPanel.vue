<script setup lang="ts">
import { computed } from "vue";
import { storeToRefs } from "pinia";
import { useTimelineStore } from "@/stores/timeline";
import UploadSlot from "./UploadSlot.vue";
import RefGrid from "./RefGrid.vue";
import PromptEditor from "./PromptEditor.vue";
import PromptHistoryPanel from "./PromptHistoryPanel.vue";
import { palette } from "@/styles/theme";
import {
  DURATION_LIMITS,
  REFERENCE_LIMITS,
  type MediaKind,
  type PromptPickerItem,
  type ReferenceMedia,
  type Clip,
  type ClipMode,
} from "@/types/timeline";

const store = useTimelineStore();
const { selectedClip } = storeToRefs(store);

const modeOptions = [
  { label: "文生视频 (t2v)", value: "t2v" },
  { label: "图生视频 (i2v)", value: "i2v" },
  { label: "首尾帧 (fl2v)", value: "fl2v" },
  { label: "参考主体 (r2v)", value: "r2v" },
  { label: "视频转视频 (v2v)", value: "v2v" },
  { label: "参考改视频 (rv2v)", value: "rv2v" },
];

function patch(p: Record<string, unknown>) {
  const seg = selectedClip.value;
  if (!seg) return;
  store.updateClip(seg.id, p);
}

// ---------- 提示词 @ 引用素材（结构化 token 快捷插入，编辑器组件 PromptEditor） ----------

/** 素材缩略图：优先已缓存的 preview（上传/反序列化生成，已是 webp 小图）；无 preview 时兜底按 path 重建 */
function pickerThumb(media?: ReferenceMedia): string | undefined {
  if (!media) return undefined;
  if (media.preview) return media.preview;
  if (media.kind === "image" && media.path) {
    const parts = media.path.split("/");
    const filename = parts.pop() ?? media.path;
    const params = new URLSearchParams({ filename, type: "input" });
    params.set("preview", "webp");
    if (parts.length) params.set("subfolder", parts.join("/"));
    return `/view?${params.toString()}`;
  }
  return undefined;
}

/** 当前模式可用素材（编号规则与后端 ref_*_i 键、官方 <Picture N> 语法对齐） */
const pickerItems = computed<PromptPickerItem[]>(() => {
  const seg = selectedClip.value;
  if (!seg) return [];
  const items: PromptPickerItem[] = [];
  const push = (token: string, label: string, kind: MediaKind, media?: ReferenceMedia) =>
    items.push({ token, label, kind, media, thumb: pickerThumb(media) });
  if (seg.mode === "i2v" || seg.mode === "fl2v") {
    if (seg.firstFrame) push("<Picture 1>", "首帧图", "image", seg.firstFrame);
    if (seg.mode === "fl2v" && seg.lastFrame) push("<Picture 2>", "尾帧图", "image", seg.lastFrame);
  }
  if (seg.mode === "r2v" || seg.mode === "rv2v") {
    // 紧凑模型：编号 = 下标 + 1（官方 <Picture N> = 第 N 张），每张图都可引用
    seg.refImages?.forEach((m, i) => push(`<Picture ${i + 1}>`, `参考图 ${i + 1}`, "image", m));
  }
  if (seg.mode === "r2v") {
    seg.refVideos?.forEach((m, i) => push(`<Video ${i + 1}>`, `参考视频 ${i + 1}`, "video", m));
  }
  if (seg.mode === "r2v" || seg.mode === "rv2v") {
    seg.refAudios?.forEach((m, i) => push(`<Audio ${i + 1}>`, `参考音频 ${i + 1}`, "audio", m));
  }
  if ((seg.mode === "v2v" || seg.mode === "rv2v") && seg.sourceVideo) {
    push("<Video 1>", "源视频", "video", seg.sourceVideo);
  }
  return items;
});

/** 按 token 编号查找对应素材（渲染高亮缩略图用；未引用到素材则无图） */
function mediaForToken(kind: "Picture" | "Video" | "Audio", n: number): ReferenceMedia | undefined {
  const seg = selectedClip.value;
  if (!seg) return undefined;
  if (kind === "Picture") {
    if (n === 1 && seg.firstFrame) return seg.firstFrame;
    if (n === 2 && seg.lastFrame) return seg.lastFrame;
    return seg.refImages?.[n - 1]; // 紧凑列表：第 N 张（不存在则无素材）
  }
  if (kind === "Video") {
    if (n === 1 && seg.sourceVideo) return seg.sourceVideo;
    return seg.refVideos?.[n - 1];
  }
  return seg.refAudios?.[n - 1];
}

// ---------- contenteditable 原子编辑器（已迁移至 PromptEditor.vue） ----------

/** 切换模式时清理不再使用的素材字段，避免脏数据 */
function onModeChange(mode: ClipMode) {
  const seg = selectedClip.value;
  if (!seg) return;
  const patchObj: Record<string, unknown> = { mode };
  if (mode !== "i2v" && mode !== "fl2v") patchObj.firstFrame = undefined;
  if (mode !== "fl2v") patchObj.lastFrame = undefined;
  if (mode !== "r2v" && mode !== "rv2v") {
    patchObj.refImages = undefined;
    patchObj.refVideos = undefined;
  }
  if (mode !== "r2v" && mode !== "rv2v") patchObj.refAudios = undefined;
  if (mode !== "v2v" && mode !== "rv2v") patchObj.sourceVideo = undefined;
  store.updateClip(seg.id, patchObj);
}

// ---------- 素材操作 ----------
// 紧凑模型：参考素材 = 有序实体列表，编号 = 下标 + 1（官方 <Picture N> = 第 N 张）。
// 删除 = 直接移除并自动前移补位；添加 = 末尾追加；拖拽排序由 RefGrid 整表重排。

type RefListKey = "refImages" | "refVideos" | "refAudios";

/** 列表原位替换指定项（编号不变） */
function replaceListItem(seg: Clip | null, key: RefListKey, index: number, media: ReferenceMedia) {
  if (!seg) return;
  const list = [...(seg[key] ?? [])];
  list[index] = media;
  patch({ [key]: list });
}

/** 列表末尾追加（新编号 = 当前长度 + 1） */
function pushListItem(seg: Clip | null, key: RefListKey, media: ReferenceMedia) {
  if (!seg) return;
  const list = [...(seg[key] ?? [])];
  list.push(media);
  patch({ [key]: list });
}

/** 删除指定素材（列表自动前移补位，编号连续） */
function removeListItem(seg: Clip | null, key: RefListKey, index: number) {
  if (!seg) return;
  const list = [...(seg[key] ?? [])];
  if (index >= list.length) return;
  list.splice(index, 1);
  patch({ [key]: list });
}

/** 拖拽排序：按 RefGrid 重建的新顺序整表替换 */
function reorderList(seg: Clip | null, key: RefListKey, list: ReferenceMedia[]) {
  if (!seg) return;
  patch({ [key]: list });
}

/** 模式是否使用对应素材区 */
const hasFrames = () => ["i2v", "fl2v"].includes(selectedClip.value?.mode ?? "");
const hasRefImages = () => ["r2v", "rv2v"].includes(selectedClip.value?.mode ?? "");
const hasRefVideos = () => selectedClip.value?.mode === "r2v";
const hasRefAudios = () => ["r2v", "rv2v"].includes(selectedClip.value?.mode ?? "");
const hasSourceVideo = () => ["v2v", "rv2v"].includes(selectedClip.value?.mode ?? "");

/** 历史弹窗开关/刷新统一在 store（卡片历史图标与详情面板共用入口） */


</script>

<template>
  <div class="detail">
    <!-- 未选中片段 -->
    <div v-if="!selectedClip" class="detail-empty">
      <span>🎞️</span>
      <p>点击时间线上的片段进行编辑</p>
    </div>

    <!-- 选中片段编辑 -->
    <div v-else class="detail-body">
      <!-- ===== 顶部菜单栏：模式 / 时长 / 续接 / 历史记录 ===== -->
      <div class="detail-toolbar">
        <div class="tb-item">
          <span class="tb-tab">模式</span>
          <n-select
            :value="selectedClip.mode"
            :options="modeOptions"
            size="small"
            class="tb-mode"
            @update:value="(v: ClipMode) => onModeChange(v)"
          />
        </div>
        <div class="tb-item">
          <span class="tb-tab">时长</span>
          <n-input-number
            :value="selectedClip.durationSec"
            :min="DURATION_LIMITS.minSec"
            :max="DURATION_LIMITS.maxSec"
            :step="DURATION_LIMITS.stepSec"
            size="small"
            class="tb-duration"
            @update:value="(v: number | null) => v != null && patch({ durationSec: v })"
          />
        </div>
        <div class="tb-item">
          <span class="tb-tab">续接</span>
          <n-switch
            :value="!!selectedClip.continuity"
            size="small"
            @update:value="(v: boolean) => patch({ continuity: v })"
          />
        </div>
        <button class="tb-history" title="打开该片段的提示词历史（版本比对 + 采样结果，可锁定缓存出片）" @click="store.openHistoryPanel()">
          🕘 历史记录
        </button>
      </div>

      <!-- ===== 主体：提示词(3/5) + 参考(2/5) 左右排列 ===== -->
      <div class="detail-main">
        <!-- 提示词栏：独立编辑器组件（contenteditable + @ 素材引用 + / 结构化符号补全） -->
        <PromptEditor
          :model-value="selectedClip.prompt ?? ''"
          :items="pickerItems"
          :resolve-media="mediaForToken"
          @update:model-value="(v: string) => patch({ prompt: v })"
        />

        <!-- 参考素材栏（按模式显示：首帧/尾帧/参考图/参考视频/参考音频/源视频） -->
        <div class="refs-pane">
          <!-- 首帧图（i2v / fl2v） -->
          <div v-if="hasFrames()" class="ref-slot">
            <div class="pane-title">首帧图</div>
            <UploadSlot
              :media="selectedClip.firstFrame"
              label="选择首帧图"
              kind="image"
              @change="(m: ReferenceMedia) => patch({ firstFrame: m })"
              @remove="patch({ firstFrame: undefined })"
            />
          </div>

          <!-- 尾帧图（fl2v） -->
          <div v-if="selectedClip.mode === 'fl2v'" class="ref-slot">
            <div class="pane-title">尾帧图 <span class="pane-hint">官方支持只传尾帧</span></div>
            <UploadSlot
              :media="selectedClip.lastFrame"
              label="选择尾帧图"
              kind="image"
              @change="(m: ReferenceMedia) => patch({ lastFrame: m })"
              @remove="patch({ lastFrame: undefined })"
            />
          </div>

          <!-- 参考图（r2v / rv2v）：编号 = 位置，可拖拽排序 -->
          <div v-if="hasRefImages()" class="ref-slot">
            <div class="pane-title">参考图 <span class="pane-hint">{{ selectedClip.refImages?.length ?? 0 }}/{{ REFERENCE_LIMITS.images }} · 用 &lt;Picture N&gt; · 拖动排序</span></div>
            <RefGrid
              :list="selectedClip.refImages ?? []"
              :max="REFERENCE_LIMITS.images"
              name="参考图"
              kind="image"
              @change="(i: number, mm: ReferenceMedia) => replaceListItem(selectedClip, 'refImages', i, mm)"
              @remove="(i: number) => removeListItem(selectedClip, 'refImages', i)"
              @add="(mm: ReferenceMedia) => pushListItem(selectedClip, 'refImages', mm)"
              @reorder="(l: ReferenceMedia[]) => reorderList(selectedClip, 'refImages', l)"
            />
          </div>

          <!-- 参考视频（r2v）：编号 = 位置，可拖拽排序 -->
          <div v-if="hasRefVideos()" class="ref-slot">
            <div class="pane-title">参考视频 <span class="pane-hint">{{ selectedClip.refVideos?.length ?? 0 }}/{{ REFERENCE_LIMITS.videos }} · 用 &lt;Video N&gt; · 拖动排序</span></div>
            <RefGrid
              :list="selectedClip.refVideos ?? []"
              :max="REFERENCE_LIMITS.videos"
              name="参考视频"
              kind="video"
              @change="(i: number, mm: ReferenceMedia) => replaceListItem(selectedClip, 'refVideos', i, mm)"
              @remove="(i: number) => removeListItem(selectedClip, 'refVideos', i)"
              @add="(mm: ReferenceMedia) => pushListItem(selectedClip, 'refVideos', mm)"
              @reorder="(l: ReferenceMedia[]) => reorderList(selectedClip, 'refVideos', l)"
            />
          </div>

          <!-- 参考音频（r2v / rv2v）：编号 = 位置，可拖拽排序 -->
          <div v-if="hasRefAudios()" class="ref-slot">
            <div class="pane-title">参考音频 <span class="pane-hint">{{ selectedClip.refAudios?.length ?? 0 }}/{{ REFERENCE_LIMITS.audios }} · 用 &lt;Audio N&gt; · 拖动排序</span></div>
            <RefGrid
              :list="selectedClip.refAudios ?? []"
              :max="REFERENCE_LIMITS.audios"
              name="参考音频"
              kind="audio"
              @change="(i: number, mm: ReferenceMedia) => replaceListItem(selectedClip, 'refAudios', i, mm)"
              @remove="(i: number) => removeListItem(selectedClip, 'refAudios', i)"
              @add="(mm: ReferenceMedia) => pushListItem(selectedClip, 'refAudios', mm)"
              @reorder="(l: ReferenceMedia[]) => reorderList(selectedClip, 'refAudios', l)"
            />
          </div>

          <!-- 源视频（v2v / rv2v） -->
          <div v-if="hasSourceVideo()" class="ref-slot">
            <div class="pane-title">源视频 <span class="pane-hint">自动绑定为 &lt;Video 1&gt;</span></div>
            <UploadSlot
              :media="selectedClip.sourceVideo"
              label="选择源视频"
              kind="video"
              @change="(m: ReferenceMedia) => patch({ sourceVideo: m })"
              @remove="patch({ sourceVideo: undefined })"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 历史记录弹窗（提示词历史 + 采样结果墙 + 版本比对；PromptHistoryPanel） ===== -->
    <n-modal
      :show="store.showHistoryPanel"
      preset="card"
      title="历史记录"
      :bordered="false"
      class="history-modal"
      style="width: 940px"
      @update:show="(v: boolean) => v || store.closeHistoryPanel()"
    >
      <PromptHistoryPanel v-if="selectedClip" :clip="selectedClip" />
    </n-modal>
  </div>
</template>

<style scoped>
.detail {
  background: var(--dc-panel);
  border: 1px solid var(--dc-border);
  border-radius: 12px;
  padding: 14px;
  min-height: 96px;
}

.detail-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--dc-text-faint);
  font-size: 13px;
  height: 72px;
}
.detail-empty span {
  font-size: 20px;
}
.detail-empty p {
  margin: 0;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 480px;
}

/* ---------- 顶部菜单栏（模式 / 时长 / 续接 / 历史记录） ---------- */
.detail-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 10px;
  border: 1px solid var(--dc-border);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.03);
}
.tb-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.tb-tab {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 600;
  color: var(--dc-text-dim);
  padding: 2px 6px;
  background: rgba(148, 163, 184, 0.12);
  border-radius: 5px;
  white-space: nowrap;
}
.tb-mode {
  width: 152px;
}
.tb-duration {
  width: 92px;
}
.tb-history {
  margin-left: auto;
  height: 26px;
  padding: 0 12px;
  border: 1px solid rgba(96, 165, 250, 0.4);
  border-radius: 7px;
  background: rgba(96, 165, 250, 0.14);
  color: #93c5fd;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.12s ease;
  white-space: nowrap;
}
.tb-history:hover {
  background: rgba(96, 165, 250, 0.24);
  border-color: rgba(96, 165, 250, 0.7);
}

/* ---------- 主体：提示词(3/5) + 参考(2/5) 左右（提示词栏样式在 PromptEditor.vue） ----------
 * 统一编辑基准高度：行高固定为 540px（≈ r2v 三行素材区此前撑开的高度），参考面板与
 * 提示词输入框两侧等高、高度不随模式跳变；提示词过长不会再把行向下拉伸——
 * 两侧内容超高时各自在区域内滚动（refs-pane / .prompt-input 均有 overflow）。 */
.detail-main {
  display: flex;
  gap: 12px;
  align-items: stretch;
  height: 540px;
  min-height: 0;
}
.refs-pane {
  flex: 2;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--dc-border);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.02);
  overflow-y: auto;
}

/* 功能区域名：底色框标签（素材区标题；提示词栏内同类样式在 PromptEditor.vue） */
.pane-title {
  font-size: 11px;
  font-weight: 600;
  color: #93c5fd;
  align-self: flex-start;
  padding: 2px 8px;
  background: rgba(96, 165, 250, 0.14);
  border-radius: 6px;
  white-space: nowrap;
}
.pane-hint {
  font-weight: 400;
  font-size: 10px;
  color: var(--dc-text-faint);
  margin-left: 2px;
}

.ref-slot {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  padding: 8px;
  background: rgba(255, 255, 255, 0.015);
}

/* ---------- 历史弹窗（参数状态 → 采样记录，两层反悔） ---------- */
.history-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 60vh;
  overflow-y: auto;
  padding: 2px;
}
.history-empty {
  font-size: 12px;
  color: var(--dc-text-faint);
  padding: 10px 0;
  text-align: center;
}

/* 分辨率分组（当前组展开，其他组默认折叠） */
.res-group {
  border: 1px solid var(--dc-border);
  border-radius: 6px;
  overflow: hidden;
}
.res-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  cursor: pointer;
  background: rgba(255, 255, 255, 0.03);
  font-size: 12px;
  user-select: none;
}
.res-group-head:hover {
  background: rgba(255, 255, 255, 0.06);
}
.res-group-name {
  font-weight: 600;
  color: var(--dc-text);
  font-variant-numeric: tabular-nums;
}
.res-group-cur {
  font-size: 10px;
  color: #4ade80;
  border: 1px solid rgba(74, 222, 128, 0.4);
  border-radius: 3px;
  padding: 0 4px;
}
.res-group-count {
  font-size: 11px;
  color: var(--dc-text-dim);
}
.res-group-arrow {
  margin-left: auto;
  color: var(--dc-text-dim);
  font-size: 11px;
}
.res-group-body {
  padding: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.state-item {
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  overflow: hidden;
}
.state-item.current {
  border-color: v-bind("palette.accent");
}
/* 含当前已选用 latent 的参数状态：绿色边框强调（用户显式选定，优先于"最近"蓝框） */
.state-item.has-chosen {
  border-color: rgba(74, 222, 128, 0.7);
  box-shadow: 0 0 0 1px rgba(74, 222, 128, 0.18);
}
.state-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  cursor: pointer;
  background: rgba(255, 255, 255, 0.02);
  min-width: 0;
}
.state-head:hover {
  background: rgba(255, 255, 255, 0.05);
}
.state-time {
  font-size: 11px;
  color: var(--dc-text-dim);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.state-prompt {
  flex: 1;
  font-size: 12px;
  color: var(--dc-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.state-count {
  font-size: 11px;
  color: var(--dc-text-faint);
  flex-shrink: 0;
}
.state-cur {
  font-size: 10px;
  color: v-bind("palette.accent");
  flex-shrink: 0;
}
.state-arrow {
  font-size: 10px;
  color: var(--dc-text-faint);
  flex-shrink: 0;
}
.sample-list {
  border-top: 1px solid var(--dc-border);
  padding: 4px 8px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sample-empty {
  font-size: 12px;
  color: var(--dc-text-faint);
  padding: 6px 0;
}
.sample-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  cursor: pointer;
}
.sample-item.chosen {
  background: rgba(20, 83, 45, 0.16);
  outline: 1px solid rgba(74, 222, 128, 0.75);
}
/* 采样预览缩略图（动画 WebP 静态首帧展示；点击弹窗播放） */
.sample-thumb-wrap {
  flex-shrink: 0;
  cursor: zoom-in;
  line-height: 0;
  border-radius: 4px;
}
.sample-thumb-wrap:hover {
  outline: 1px solid v-bind("palette.accent");
}
.sample-thumb {
  width: 72px;
  height: 40px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(15, 23, 42, 0.55);
  flex-shrink: 0;
}
.sample-thumb-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}
.sample-check {
  width: 13px;
  height: 13px;
  margin: 0;
  cursor: pointer;
  accent-color: v-bind("palette.accent");
  flex-shrink: 0;
}
.sample-check:disabled {
  cursor: not-allowed;
  opacity: 0.4;
}
.sample-seed {
  flex: 1;
  font-size: 12px;
  color: var(--dc-text);
  font-variant-numeric: tabular-nums;
}
.sample-time {
  font-size: 11px;
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
}
.sample-cur {
  font-size: 10px;
  color: #4ade80;
}
.sample-stale {
  font-size: 10px;
  color: #fbbf24;
}
.mini-btn {
  border: 1px solid var(--dc-border);
  background: var(--dc-bg);
  color: var(--dc-text);
  font-size: 11px;
  line-height: 1;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.12s ease;
}
.mini-btn:hover {
  border-color: v-bind("palette.accent");
  color: v-bind("palette.accentHover");
}
.mini-btn.danger:hover {
  border-color: v-bind("palette.danger");
  color: v-bind("palette.danger");
}
/* 样本删除按钮：默认隐藏，hover 显示（避免误触） */
.sample-del {
  border: none;
  background: transparent;
  color: var(--dc-text-faint);
  font-size: 11px;
  line-height: 1;
  padding: 2px 4px;
  border-radius: 4px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease, color 0.12s ease;
  flex-shrink: 0;
}
.sample-item:hover .sample-del {
  opacity: 1;
}
.sample-del:hover {
  color: v-bind("palette.danger");
}
.history-footer {
  border-top: 1px solid var(--dc-border);
  padding-top: 8px;
}
.history-hint {
  font-size: 11px;
  color: var(--dc-text-faint);
}

/* 历史样本预览弹窗（teleport 到 body，遮罩点击关闭） */
.preview-pop-mask {
  position: fixed;
  inset: 0;
  z-index: 3100;
  background: rgba(0, 0, 0, 0.55);
}
.preview-pop {
  position: fixed;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  z-index: 3101;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  background: var(--dc-panel);
  border: 1px solid var(--dc-border-strong);
  border-radius: 12px;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
}
.preview-pop-canvas {
  max-width: 420px;
  max-height: 70vh;
  border-radius: 8px;
  background: #0f172a;
}
.preview-pop-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 12px;
  color: var(--dc-text-dim);
}
</style>
