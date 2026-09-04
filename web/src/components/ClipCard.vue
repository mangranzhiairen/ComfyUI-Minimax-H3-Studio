<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useMessage } from "naive-ui";
import { useTimelineStore } from "@/stores/timeline";
import { palette } from "@/styles/theme";
import { usePreviewPlayer } from "@/composables/usePreviewPlayer";
import type { Clip, ClipMode } from "@/types/timeline";

const props = defineProps<{
  clip: Clip;
  widthPx: number;
  selected: boolean;
  /** 排序拖拽中（跟随指针） */
  dragging?: boolean;
  /** 排序拖拽位移 */
  dragX?: number;
  /** 排序拖拽事件绑定（由轨道层提供） */
  dragHandlers?: Record<string, unknown>;
  /** 右缘调时长事件绑定（由轨道层提供） */
  resizeHandlers?: Record<string, unknown>;
}>();

const store = useTimelineStore();
const message = useMessage();

/** 模式中文名 */
const MODE_LABELS: Record<ClipMode, string> = {
  t2v: "文生",
  i2v: "图生",
  fl2v: "首尾帧",
  r2v: "参考",
  v2v: "视频",
  rv2v: "参考改",
};

const modeLabel = computed(() => MODE_LABELS[props.clip.mode] ?? props.clip.mode);

/** base64 → Blob（live 预览是 WebSocket base64 动画 WebP） */
function b64ToBlob(b64: string, mime: string): Blob {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

// ---------- 采样预览（单播放器 + 可切换画布） ----------
// 仅采样阶段（phase='sampling'）有 live 帧推送时显示：hover 卡片 → 卡片内小预览；
// 持续 hover 2s → 弹框以鼠标为中心弹出（同一播放器切画布放大，画面无缝、更新实时
// 跟随）；鼠标移出弹框缓冲带 → 关闭并恢复卡片显示。VAE 解码阶段（phase='decoding'）
// 不再推采样帧 → 卡片不显示采样预览（只保留绿框 + 底部解码进度条）。

const previewCanvas = ref<HTMLCanvasElement | null>(null); // 模板 ref：卡片小预览
const bigCanvas = ref<HTMLCanvasElement | null>(null); // 模板 ref：弹框大预览（data-big）
/** 当前绘制目标（播放器逐帧画到它；弹框=同帧源切画布放大） */
const activeCanvas = ref<HTMLCanvasElement | null>(null);
const hasPreview = ref(false);
const { playFrom, stop: stopPlayer } = usePreviewPlayer(activeCanvas, 8, {
  // 大画布（data-big）的视口上限：按视频帧比例无黑边放大
  width: Math.min(920, Math.max(480, (window.innerWidth || 1280) * 0.82)),
  height: (window.innerHeight || 800) * 0.74,
});

/** 采样阶段 live 预览（动画 WebP base64，仅当前采样片段；解码阶段不再推帧 → 置空隐藏） */
const livePreview = computed(() => {
  const p = store.samplingProgress;
  return p?.clipId === props.clip.id && p.phase === "sampling" ? p.preview : undefined;
});

/** 采样阶段但画面帧未到（如刷新后等待下一次帧推送）：hover 显示黑屏等待占位 */
const waitingPreview = computed(
  () =>
    store.samplingProgress?.clipId === props.clip.id &&
    store.samplingProgress?.phase === "sampling" &&
    !hasPreview.value,
);

// ---------- 大预览弹框（以鼠标为中心） ----------

/** 大预览弹框是否打开 */
const bigOpen = ref(false);
/** 弹框锚点（鼠标坐标；CSS translate(-50%,-50%) 使弹框居中于鼠标） */
const bigX = ref(0);
const bigY = ref(0);
let lastMouseX = 0;
let lastMouseY = 0;
let hoverTimer: number | undefined;
let closeBigTimer: number | undefined;

function clearHoverTimer(): void {
  if (hoverTimer) window.clearTimeout(hoverTimer);
  hoverTimer = undefined;
}
function clearCloseBigTimer(): void {
  if (closeBigTimer) window.clearTimeout(closeBigTimer);
  closeBigTimer = undefined;
}

// 播放请求串行化：避免 fetch/decode 并发导致旧画面覆盖新帧
let playChain: Promise<boolean> = Promise.resolve(false);
function enqueuePlay(loader: () => Blob | null, durationSec?: number): Promise<boolean> {
  const run = playChain.then(async () => {
    const blob = loader();
    if (!blob) return false;
    return playFrom(blob, durationSec);
  });
  playChain = run.catch(() => false);
  return run;
}

/** 打开大弹框：绘制目标切到弹框画布——当前帧无缝放大（不停止播放器） */
function openBigPreview(): void {
  if (bigOpen.value || !hasPreview.value) return;
  activeCanvas.value = bigCanvas.value;
  bigOpen.value = true;
  // 弹框居中于鼠标（CSS translate 平移自身一半）；鼠标一移开弹框缓冲带即结束预览
  bigX.value = Math.max(0, Math.min(lastMouseX, window.innerWidth || 1280));
  bigY.value = Math.max(0, Math.min(lastMouseY, window.innerHeight || 800));
  installDocClose();
}

/** 关闭大弹框：绘制目标切回卡片小预览（帧循环未停 → 立即恢复卡片显示） */
function closeBigPreview(): void {
  if (!bigOpen.value) return;
  bigOpen.value = false;
  activeCanvas.value = previewCanvas.value;
  uninstallDocClose();
}

// 弹框打开期间全局监听鼠标：光标离开弹框（±18px 缓冲）即结束，进入弹框可细看
let bigDocMove: ((e: MouseEvent) => void) | null = null;
function installDocClose(): void {
  uninstallDocClose();
  bigDocMove = (e: MouseEvent) => {
    lastMouseX = e.clientX;
    lastMouseY = e.clientY;
    const el = bigCanvas.value?.parentElement;
    if (!bigOpen.value || !el) return;
    const r = el.getBoundingClientRect();
    const pad = 18;
    const inside =
      e.clientX >= r.left - pad &&
      e.clientX <= r.right + pad &&
      e.clientY >= r.top - pad &&
      e.clientY <= r.bottom + pad;
    if (!inside) closeBigPreview();
  };
  window.addEventListener("mousemove", bigDocMove, { passive: true });
}
function uninstallDocClose(): void {
  if (bigDocMove) window.removeEventListener("mousemove", bigDocMove);
  bigDocMove = null;
}

function onCardEnter(): void {
  clearCloseBigTimer();
  if (bigOpen.value) {
    // 从弹框回到卡片：关弹框 → 恢复卡片显示（帧循环未停，小预览立即续播）
    closeBigPreview();
    return;
  }
  // hover 满 2s 且有画面可播（仅采样中 live）→ 弹大框
  clearHoverTimer();
  hoverTimer = window.setTimeout(() => {
    if (hasPreview.value && !bigOpen.value) openBigPreview();
  }, 2000);
}

function onCardMove(e: MouseEvent): void {
  lastMouseX = e.clientX;
  lastMouseY = e.clientY;
}

function onCardLeave(): void {
  clearHoverTimer();
  // 关闭交给 installDocClose 的全局判定（光标离开弹框缓冲带即关）
}

// live 帧变化 → 串行喂给播放器；live 消失（采样结束/换段）→ 停止并复位（含弹框）
watch(livePreview, async (live) => {
  if (!live) {
    hasPreview.value = false;
    stopPlayer();
    bigOpen.value = false;
    activeCanvas.value = previewCanvas.value;
    uninstallDocClose();
    clearHoverTimer();
    clearCloseBigTimer();
    return;
  }
  hasPreview.value = await enqueuePlay(() => b64ToBlob(live, "image/webp"));
});

onBeforeUnmount(() => {
  clearHoverTimer();
  clearCloseBigTimer();
  uninstallDocClose();
  stopPlayer();
  resizeObserver?.disconnect();
});

/** 缩略图/占位背景：素材图优先，无图用模式对应的渐变占位（采样预览由 canvas 层绘制） */
const bgStyle = computed(() => {
  if (props.clip.thumb) {
    return {
      backgroundImage: `url(${props.clip.thumb})`,
      backgroundSize: "cover",
      backgroundPosition: "center",
    };
  }
  const gradients: Record<ClipMode, string> = {
    t2v: "linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%)",
    i2v: "linear-gradient(135deg, #164e63 0%, #0f172a 100%)",
    fl2v: "linear-gradient(135deg, #3b0764 0%, #0f172a 100%)",
    r2v: "linear-gradient(135deg, #312e81 0%, #0f172a 100%)",
    v2v: "linear-gradient(135deg, #7f1d1d 0%, #0f172a 100%)",
    rv2v: "linear-gradient(135deg, #701a75 0%, #0f172a 100%)",
  };
  return { background: gradients[props.clip.mode] ?? gradients.t2v };
});

/** 该卡片的采样数（删除确认文案提示影响范围） */
const sampleCount = computed(
  () => store.historyByClipId[props.clip.id]?.samples.length ?? 0,
);
const showDeleteConfirm = ref(false);

// ---------- 采样/解码处理中（executor 经 WebSocket 广播，卡片绿框 + 进度条） ----------
// samplingProgress 覆盖采样（phase='sampling'）与 VAE 解码（phase='decoding'）两阶段：
// 绿框 + 底部进度条两阶段共用；采样 live 预览/等待占位只属于采样阶段（见上）。
const isProcessing = computed(
  () =>
    store.samplingProgress?.clipId === props.clip.id &&
    (store.samplingProgress.phase === "sampling" || store.samplingProgress.phase === "decoding"),
);
const progressPct = computed(() =>
  Math.round((store.samplingProgress?.value ?? 0) * 100),
);
/** 进度条悬停提示：采样 / 解码阶段中文标签 */
const progressTitle = computed(() =>
  isProcessing.value
    ? `${store.samplingProgress?.phase === "decoding" ? "解码中" : "采样中"} ${progressPct.value}%`
    : "",
);
/** 当前步数/总步数（如 2/4，仅采样阶段携带） */
const stepText = computed(() => {
  const p = store.samplingProgress;
  if (!p?.stepsTotal) return "";
  return `${p.step ?? 0}/${p.stepsTotal}`;
});

function onRemove(e: MouseEvent) {
  e.stopPropagation();
  showDeleteConfirm.value = true;
}

async function confirmDelete() {
  showDeleteConfirm.value = false;
  const ok = await store.deleteClip(props.clip.id);
  if (!ok) message.warning("删除失败：后端历史清理未完成，卡片已保留");
}

function onDuplicate(e: MouseEvent) {
  e.stopPropagation();
  store.duplicateClip(props.clip.id);
}

/** 点击卡片右下角历史徽标：选中该卡片并直接打开其历史弹窗 */
function onOpenHistory(): void {
  store.select(props.clip.id);
  store.openHistoryPanel();
}

function onToggleEnabled(e: Event) {
  const checked = (e.target as HTMLInputElement).checked;
  store.updateClip(props.clip.id, { enabled: checked });
}

/** 素材缩略图：按类型分行（图像 / 视频 / 音频） */
interface MediaThumb {
  preview?: string;
  icon: string;
  /** 类型名（首帧图/参考图…），悬停提示兜底 */
  label: string;
  /** 素材文件名，悬停提示主用 */
  name?: string;
}
const imageThumbs = computed<MediaThumb[]>(() => {
  const seg = props.clip;
  const list: MediaThumb[] = [];
  if (seg.firstFrame) list.push({ preview: seg.firstFrame.preview, icon: "🖼️", label: "首帧图", name: seg.firstFrame.name });
  if (seg.lastFrame) list.push({ preview: seg.lastFrame.preview, icon: "🖼️", label: "尾帧图", name: seg.lastFrame.name });
  for (const m of seg.refImages ?? []) list.push({ preview: m.preview, icon: "🖼️", label: "参考图", name: m.name });
  return list;
});
const videoThumbs = computed<MediaThumb[]>(() => {
  const seg = props.clip;
  const list: MediaThumb[] = [];
  if (seg.sourceVideo) list.push({ preview: undefined, icon: "🎞️", label: "源视频", name: seg.sourceVideo.name });
  for (const _m of seg.refVideos ?? []) list.push({ preview: undefined, icon: "🎞️", label: "参考视频", name: _m.name });
  return list;
});
const audioThumbs = computed<MediaThumb[]>(() => {
  const seg = props.clip;
  const list: MediaThumb[] = [];
  for (const _m of seg.refAudios ?? []) list.push({ preview: undefined, icon: "🎵", label: "参考音频", name: _m.name });
  return list;
});

// ---------- 超宽检测：缩略图总宽 > 段宽时切换为均分裁剪，保证全部在段内可见 ----------

const clipEl = ref<HTMLElement | null>(null);
/** 三行（图像/视频/音频）各自的超宽状态 */
const cropRows = ref([false, false, false]);
/** 切到 crop 时的自然宽缓存（用于放大后退出 crop 的判断） */
const naturalCache = ref<(number | undefined)[]>([undefined, undefined, undefined]);
let resizeObserver: ResizeObserver | null = null;

function measureRows() {
  const el = clipEl.value;
  if (!el) return;
  const rows = el.querySelectorAll<HTMLElement>(".clip-media-row");
  rows.forEach((row, i) => {
    if (!cropRows.value[i]) {
      // 非 crop 状态：scrollWidth 即自然宽，超宽才切换（避免 crop 改变布局后自我翻转）
      const over = row.scrollWidth > row.clientWidth + 1;
      if (over) {
        naturalCache.value[i] = row.scrollWidth;
        cropRows.value[i] = true;
      }
    } else {
      // crop 状态：保持裁剪，直到段宽放大到能容纳自然宽再退出
      const cached = naturalCache.value[i];
      if (cached != null && row.clientWidth >= cached) {
        cropRows.value[i] = false;
        naturalCache.value[i] = undefined;
      }
    }
  });
}

onMounted(() => {
  // 播放器绘制目标默认卡片小画布（常驻渲染；开大弹框时切换到大画布）
  activeCanvas.value = previewCanvas.value;
  measureRows();
  // 片段宽度变化（缩放、拖时长）时重新测量
  resizeObserver = new ResizeObserver(measureRows);
  if (clipEl.value) resizeObserver.observe(clipEl.value);
});
</script>

<template>
  <div
    ref="clipEl"
    class="clip"
    :class="{ selected, disabled: !clip.enabled, dragging, 'has-preview': hasPreview, waiting: waitingPreview, processing: isProcessing }"
    :style="{
      width: `${widthPx}px`,
      transform: dragging ? `translateX(${dragX ?? 0}px)` : undefined,
    }"
    :data-clip-id="clip.id"
    v-bind="dragHandlers"
    @mouseenter="onCardEnter"
    @mousemove="onCardMove"
    @mouseleave="onCardLeave"
  >
    <div class="clip-bg" :style="bgStyle">
      <!-- 采样预览层（动画 WebP，JS 控帧率；绘制时 cover 铺满）。
           弹框（大预览）打开时 v-show 隐藏 = 卡片上暂停显示 -->
      <canvas v-show="hasPreview && !bigOpen" ref="previewCanvas" class="clip-preview-canvas"></canvas>
      <!-- 采样阶段但画面帧未到（刷新后）：hover 黑屏等待占位（解码阶段不显示） -->
      <div v-if="waitingPreview" class="clip-wait">
        <span class="clip-wait-text">等待采样后显示</span>
      </div>
      <div class="clip-shade"></div>

      <!-- 顶部操作条：hover 显示 -->
      <div class="clip-actions" @pointerdown.stop>
        <label class="clip-check" title="参与生成" @click.stop>
          <input
            type="checkbox"
            :checked="clip.enabled"
            @change="onToggleEnabled"
          />
        </label>
        <button class="clip-btn" title="复制片段" @click="onDuplicate">⧉</button>
        <button class="clip-btn danger" title="删除片段" @click="onRemove">✕</button>
      </div>

      <!-- 模式徽标 -->
      <span class="clip-mode">{{ modeLabel }}</span>

      <!-- 素材缩略图：图像 / 视频 / 音频 各一行；默认按比例居中，超宽时均分裁剪 -->
      <div class="clip-media">
        <div
          v-if="imageThumbs.length"
          class="clip-media-row"
          :class="{ crop: cropRows[0] }"
        >
          <div
            v-for="(m, i) in imageThumbs"
            :key="i"
            class="clip-media-thumb"
            :title="m.name || m.label"
          >
            <img v-if="m.preview" :src="m.preview" alt="" loading="lazy" @load="measureRows" />
            <span v-else class="clip-media-icon">{{ m.icon }}</span>
          </div>
        </div>
        <div
          v-if="videoThumbs.length"
          class="clip-media-row"
          :class="{ crop: cropRows[1] }"
        >
          <div
            v-for="(m, i) in videoThumbs"
            :key="i"
            class="clip-media-thumb"
            :title="m.name || m.label"
          >
            <img v-if="m.preview" :src="m.preview" alt="" loading="lazy" />
            <span v-else class="clip-media-icon">{{ m.icon }}</span>
          </div>
        </div>
        <div
          v-if="audioThumbs.length"
          class="clip-media-row"
          :class="{ crop: cropRows[2] }"
        >
          <div
            v-for="(m, i) in audioThumbs"
            :key="i"
            class="clip-media-thumb"
            :title="m.name || m.label"
          >
            <img v-if="m.preview" :src="m.preview" alt="" loading="lazy" />
            <span v-else class="clip-media-icon">{{ m.icon }}</span>
          </div>
        </div>
      </div>

      <!-- 提示词摘要（素材行下方） -->
      <div class="clip-prompt" :title="clip.prompt || '双击编辑提示词'">
        {{ clip.prompt || "（空）" }}
      </div>

      <!-- 时长标签 -->
      <span class="clip-duration">{{ clip.durationSec }}s</span>

      <!-- 历史入口：采样数徽标（点击选中卡片，详情面板展示历史区）；
           已选用缓存 latent 时变绿高亮（当前 clip 出片将直接用缓存跳过采样） -->
      <button
        class="clip-history"
        :class="{ empty: sampleCount === 0, cached: !!clip.sampleFp }"
        :title="clip.sampleFp
          ? `已选用缓存 latent（出片跳过采样） · 共 ${sampleCount} 份采样历史，点击查看`
          : sampleCount
            ? `查看 ${sampleCount} 份采样历史`
            : '暂无采样，采样后自动存档'"
        @pointerdown.stop
        @click.stop="onOpenHistory"
      >🕘{{ sampleCount || "" }}</button>

      <!-- 采样/解码进度条（当前处理片段，卡片底部） -->
      <div v-if="isProcessing" class="clip-progress" :title="progressTitle">
        <div class="clip-progress-bar" :style="{ width: `${progressPct}%` }"></div>
        <span v-if="stepText" class="clip-progress-text">{{ stepText }}</span>
      </div>

      <!-- 右缘调时长把手 -->
      <div
        class="clip-resize"
        :data-clip-id="clip.id"
        v-bind="resizeHandlers"
        @pointerdown.stop
        title="拖动调整时长"
      ></div>
    </div>
  </div>

  <!-- 删除卡片：二次确认（连带全部采样缓存，不可逆） -->
  <n-modal
    :show="showDeleteConfirm"
    preset="dialog"
    title="删除卡片"
    :content="`将删除该卡片及其 ${sampleCount} 份采样缓存（latent 与预览），不可恢复。确定删除？`"
    :positive-text="'删除'"
    :negative-text="'取消'"
    @positive-click="confirmDelete"
    @negative-click="showDeleteConfirm = false"
    @close="showDeleteConfirm = false"
  />

  <!-- 大预览弹框：hover 2s 后出现在鼠标旁（同一播放器切画布放大）；鼠标移出缓冲带即关闭。
       画布常驻渲染（v-show）——ref 始终可用，开框即切换绘制目标无延迟 -->
  <Teleport to="body">
    <div
      v-show="bigOpen"
      class="preview-big"
      :style="{ left: `${bigX}px`, top: `${bigY}px` }"
    >
      <canvas ref="bigCanvas" data-big class="preview-big-canvas"></canvas>
    </div>
  </Teleport>
</template>

<style scoped>
.clip {
  position: relative;
  /* 紧凑布局：平时工具行贴底，采样时进度条/步数/图标紧挨排列 */
  height: 236px;
  border-radius: 6px;
  overflow: hidden;
  border: 2px solid var(--dc-border);
  cursor: pointer;
  flex-shrink: 0;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, opacity 0.15s ease;
  background: var(--dc-panel);
}
.clip:hover {
  border-color: var(--dc-border-strong);
}
.clip.selected {
  border-color: v-bind("palette.accent");
  box-shadow: 0 0 0 2px v-bind("palette.accentDim");
}
/* 处理中（采样 / VAE 解码）绿色高亮框：与选中态区分，处理优先级更高 */
.clip.processing {
  border-color: #4ade80;
  box-shadow: 0 0 0 2px rgba(74, 222, 128, 0.3);
}
.clip.disabled {
  opacity: 0.45;
}
.clip.dragging {
  z-index: 10;
  opacity: 0.85;
  border-color: v-bind("palette.accent");
  transition: none;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}

.clip-bg {
  position: absolute;
  inset: 0;
}
/* 采样预览窗：默认隐藏（卡片空间紧张），hover 卡片时显示；
   contain 完整显示（不裁切），居中独立小窗，带阴影与圆角 */
.clip-preview-canvas {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  max-width: calc(100% - 12px);
  max-height: calc(100% - 12px);
  z-index: 5;
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.15s ease;
}
/* 有预览的卡片 hover：预览浮现，素材行/提示词淡出让位（无预览的卡片不受影响） */
.clip.has-preview:hover .clip-preview-canvas {
  opacity: 1;
}
/* 采样中无画面帧（等待中）：黑屏占位 hover 浮现，同样淡出让位 */
.clip.waiting:hover .clip-wait {
  opacity: 1;
}
.clip.has-preview:hover .clip-media,
.clip.has-preview:hover .clip-prompt,
.clip.waiting:hover .clip-media,
.clip.waiting:hover .clip-prompt {
  opacity: 0;
}
.clip-media,
.clip-prompt,
.clip-preview-canvas,
.clip-wait {
  transition: opacity 0.15s ease;
}
/* 等待占位：黑屏 + 居中提示（与预览同位置规则） */
.clip-wait {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: calc(100% - 16px);
  aspect-ratio: 16 / 9;
  max-height: calc(100% - 16px);
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(2, 6, 14, 0.86);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  z-index: 5;
  opacity: 0;
  pointer-events: none;
}
.clip-wait-text {
  font-size: 11px;
  color: var(--dc-text-faint);
  letter-spacing: 0.5px;
}
.clip-shade {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    rgba(15, 23, 42, 0.25) 0%,
    rgba(15, 23, 42, 0) 40%,
    rgba(15, 23, 42, 0.72) 100%
  );
}

.clip-actions {
  position: absolute;
  top: 4px;
  right: 4px;
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.12s ease;
  /* 缩到最窄时与左上角模式徽标重叠，按钮需盖在徽标之上 */
  z-index: 20;
}
.clip:hover .clip-actions {
  opacity: 1;
}

.clip-btn {
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 4px;
  background: rgba(15, 23, 42, 0.72);
  color: var(--dc-text);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.clip-btn:hover {
  background: v-bind("palette.accent");
  color: #0f172a;
}
.clip-btn.danger:hover {
  background: v-bind("palette.danger");
  color: #fff;
}

/* 参与生成勾选框 */
.clip-check {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(15, 23, 42, 0.72);
  border-radius: 4px;
  cursor: pointer;
}
.clip-check input {
  width: 12px;
  height: 12px;
  margin: 0;
  cursor: pointer;
  accent-color: v-bind("palette.accent");
}

.clip-mode {
  position: absolute;
  top: 4px;
  left: 4px;
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(15, 23, 42, 0.72);
  color: v-bind("palette.accent");
  z-index: 10;
}

/* 素材缩略图区：图像 / 视频 / 音频 各一行纵向堆叠 */
.clip-media {
  position: absolute;
  left: 6px;
  right: 6px;
  top: 22px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.clip-media-row {
  display: flex;
  justify-content: center; /* 默认：缩略图按比例居中排列 */
  align-items: center;
  gap: 2px;
  height: 44px;
  overflow: hidden;
}
/* 超宽（总宽 > 段宽）：均分宽度，每张缩略图左右裁剪，全部在段内可见 */
.clip-media-row.crop .clip-media-thumb {
  flex: 1 1 0;
  min-width: 0;
}
.clip-media-row.crop .clip-media-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.clip-media-thumb {
  height: 100%;
  flex-shrink: 0;
  border-radius: 3px;
  overflow: hidden;
  background: rgba(15, 23, 42, 0.55);
  border: 1px solid rgba(255, 255, 255, 0.14);
  display: flex;
  align-items: center;
  justify-content: center;
}
.clip-media-thumb img {
  height: 100%;
  width: auto; /* 宽度按图片比例，不拉伸不固定 */
  object-fit: cover;
  display: block;
}
.clip-media-icon {
  font-size: 20px;
  padding: 0 8px;
}

.clip-prompt {
  position: absolute;
  left: 6px;
  right: 8px;
  /* 平时下探到工具行上方；采样时（.processing）上移让位给步数/进度条 */
  bottom: 24px;
  font-size: 11px;
  line-height: 1.35;
  color: var(--dc-text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.6);
  transition: bottom 0.15s ease;
}
.clip.processing .clip-prompt {
  bottom: 40px;
}

.clip-duration {
  position: absolute;
  left: 6px;
  /* 平时贴底；采样时上移给步数 pill/进度条让位 */
  bottom: 6px;
  font-size: 10px;
  color: var(--dc-text-dim);
  background: rgba(15, 23, 42, 0.6);
  padding: 0 4px;
  border-radius: 3px;
  font-variant-numeric: tabular-nums;
  transition: bottom 0.15s ease;
}
.clip.processing .clip-duration {
  bottom: 22px;
}

/* 历史入口徽标：平时贴底（右下角），采样时上移与时长同排 */
.clip-history {
  position: absolute;
  right: 12px;
  bottom: 6px;
  z-index: 15;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  font-size: 10px;
  line-height: 1;
  border-radius: 3px;
  background: rgba(15, 23, 42, 0.72);
  color: var(--dc-text-dim);
  border: 1px solid rgba(255, 255, 255, 0.14);
  cursor: pointer;
  font-variant-numeric: tabular-nums;
  transition: border-color 0.12s ease, color 0.12s ease, bottom 0.15s ease;
}
.clip.processing .clip-history {
  bottom: 22px;
}
.clip-history:hover {
  border-color: v-bind("palette.accent");
  color: v-bind("palette.accent");
}
.clip-history.empty {
  opacity: 0.5;
  cursor: default;
}
.clip-history.empty:hover {
  border-color: rgba(255, 255, 255, 0.14);
  color: var(--dc-text-dim);
}
/* 已选用缓存 latent：绿色高亮（同样特异性后定义，覆盖 hover 的强调色） */
.clip-history.cached {
  color: #4ade80;
  border-color: rgba(74, 222, 128, 0.55);
  background: rgba(74, 222, 128, 0.14);
}

/* 采样进度条带：贴卡片底部，圆角渐变填充 + 柔光；步数 pill 在其上，图标紧贴其上方 */
.clip-progress {
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: 2px;
  height: 4px;
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.1);
  overflow: hidden;
  z-index: 16;
}
.clip-progress-bar {
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, #16a34a, #4ade80);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.5);
  transition: width 0.15s ease;
}
.clip-progress-text {
  position: absolute;
  /* 步数 pill 紧贴进度条正上方居中 */
  left: 50%;
  transform: translateX(-50%);
  bottom: 7px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  color: #4ade80;
  letter-spacing: 0.4px;
  background: rgba(8, 12, 24, 0.82);
  border: 1px solid rgba(74, 222, 128, 0.28);
  padding: 1px 6px;
  border-radius: 8px;
  font-variant-numeric: tabular-nums;
}

/* 右缘调时长把手 */
.clip-resize {
  position: absolute;
  top: 0;
  right: 0;
  width: 7px;
  height: 100%;
  cursor: ew-resize;
  background: transparent;
  transition: background 0.12s ease;
}
.clip:hover .clip-resize {
  background: v-bind("palette.accentDim");
}
.clip-resize:hover {
  background: v-bind("palette.accent") !important;
}

/* ---------- 大预览弹框（hover 2s 后，跟随鼠标；canvas 位图即帧等比尺寸，无黑边） ---------- */
.preview-big {
  position: fixed;
  z-index: 3150;
  /* 以鼠标为锚点居中（left/top 由 JS 设鼠标坐标，这里平移自身一半） */
  transform: translate(-50%, -50%);
  display: inline-block;
  padding: 6px;
  background: rgba(8, 12, 24, 0.92);
  border: 1px solid var(--dc-border-strong);
  border-radius: 10px;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.6);
  line-height: 0;
}
/* 画布尺寸由播放器按帧等比设置（data-big → 视口上限），无黑边；仅细圆角描边 */
.preview-big-canvas {
  display: block;
  border-radius: 6px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.07);
}
</style>
