<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { storeToRefs } from "pinia";
import { useTimelineStore } from "@/stores/timeline";
import { palette } from "@/styles/theme";
import TimelineRuler from "./TimelineRuler.vue";
import TimelineTrack from "./TimelineTrack.vue";

const store = useTimelineStore();
const { clips, zoom, totalDurationSec, taskId, historyByClipId } = storeToRefs(store);

/** 历史里有片段快照的 clip 数（空态恢复入口出现条件：有任务 + 有时间线历史） */
const historyCount = computed(() => Object.keys(historyByClipId.value).length);

/** 内容宽度 = 总时长 × 缩放（不留白：缩小下限时内容恰好铺满可视区，无滚动条） */
const contentWidth = computed(() => totalDurationSec.value * zoom.value);

// ---------- 鼠标滑轮交互：普通滑轮缩放，Shift+滑轮横向移动 ----------

const timelineEl = ref<HTMLElement | null>(null);

/** 轨道铺满可视区对应的缩放（缩小下限：内容宽=轨道宽=可视宽，无滚动条无留白） */
function fitZoomFor(el: HTMLElement): number {
  const duration = Math.max(totalDurationSec.value, 0.5);
  return Math.max(24, el.clientWidth / duration);
}

function onWheel(e: WheelEvent) {
  // 阻止冒泡到 ComfyUI 画布（否则会被画布缩放/滚动劫持）
  e.preventDefault();
  e.stopPropagation();

  if (e.shiftKey) {
    // Shift + 滑轮：时间线左右移动
    const el = timelineEl.value;
    if (el) el.scrollLeft += e.deltaY || e.deltaX;
    return;
  }
  // 普通滑轮：缩放；缩小下限 = 轨道铺满可视区（右侧不出现空白）
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
  let next = store.zoom * factor;
  const el = timelineEl.value;
  if (el) next = Math.max(next, fitZoomFor(el));
  store.setZoom(next);
}

/** 若当前缩放低于"铺满可视区"下限（如加载工作流后 zoom 仍是默认值），校正到下限 */
function clampZoomToFit() {
  ensureInitialFit();
  const el = timelineEl.value;
  if (!el || el.clientWidth <= 0) return;
  const fit = fitZoomFor(el);
  if (store.zoom < fit) store.setZoom(fit);
}

/** 初始适配标记：打开/刷新/加载工作流后只强制 fit 一次，之后交给用户自由缩放 */
let didInitialFit = false;

/** 初始状态显示所有段落（zoom = 铺满可视区），有内容且容器尺寸就绪后执行一次 */
function ensureInitialFit() {
  if (didInitialFit || !clips.value.length) return;
  const el = timelineEl.value;
  if (!el || el.clientWidth <= 0) return;
  store.setZoom(fitZoomFor(el));
  didInitialFit = true;
}

/** 强制显示所有段落（添加/复制片段、切换任务后） */
function fitToView() {
  const el = timelineEl.value;
  if (!el || el.clientWidth <= 0 || !clips.value.length) return;
  store.setZoom(fitZoomFor(el));
  didInitialFit = true;
}

let resizeObserver: ResizeObserver | null = null;

onMounted(() => {
  // passive: false 保证 preventDefault 生效
  timelineEl.value?.addEventListener("wheel", onWheel, { passive: false });
  // 数据加载（loadPayload）后总时长变化 → 校正缩放下限
  watch(totalDurationSec, clampZoomToFit);
  // 添加/复制片段（段数增加）→ 显示所有段落（否则新段落在可视区外，需手动缩小）
  watch(
    () => clips.value.length,
    (len, oldLen) => {
      if (len > oldLen) fitToView();
    },
  );
  // 切换任务（taskId 变化）→ 重新适配显示全部。
  // loadTask 先加载时间线再 setTaskId，此路径 clips 已就绪可直接 fit；
  // nodeCreated 路径 setTaskId 在 loadTask 之前（段尚为空）→ ensureInitialFit
  // 延后到 totalDurationSec 变化时由 clampZoomToFit 兜底执行。
  watch(
    () => store.taskId,
    (tid, oldTid) => {
      if (!tid || tid === oldTid) return;
      didInitialFit = false;
      ensureInitialFit();
    },
  );
  // 容器尺寸变化（节点拉伸/挂载布局完成）→ 校正缩放下限
  clampZoomToFit();
  resizeObserver = new ResizeObserver(clampZoomToFit);
  if (timelineEl.value) resizeObserver.observe(timelineEl.value);
});

onBeforeUnmount(() => {
  timelineEl.value?.removeEventListener("wheel", onWheel);
  resizeObserver?.disconnect();
});
</script>

<template>
  <div ref="timelineEl" class="timeline dc-scroll">
    <div class="timeline-inner" :style="{ width: `${contentWidth}px` }">
      <TimelineRuler />
      <TimelineTrack />

      <!-- 空态引导 -->
      <div v-if="!clips.length" class="timeline-empty">
        <div class="timeline-empty-icon">🎬</div>
        <p>时间线还是空的</p>
        <span>点击上方「添加片段」开始你的第一个镜头</span>
        <!-- 数据丢失兜底：任务历史里有过采样快照时，打开面板手动挑选恢复 -->
        <button
          v-if="taskId && historyCount > 0"
          class="empty-restore"
          title="从该任务的历史版本快照中手动挑选要恢复的片段"
          @click="store.openRestoreModal()"
        >↩ 从历史恢复片段</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  flex: 1;
  overflow-x: auto;
  overflow-y: hidden;
  background: var(--dc-bg);
  border-radius: 8px;
  border: 1px solid var(--dc-border);
  min-height: 0;
}

.timeline-inner {
  min-width: 100%;
  position: relative;
}

.timeline-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 48px 0;
  color: var(--dc-text-dim);
}
.timeline-empty-icon {
  font-size: 28px;
  opacity: 0.6;
}
.timeline-empty p {
  margin: 4px 0 0;
  font-size: 14px;
}
.timeline-empty span {
  font-size: 12px;
  color: var(--dc-text-faint);
}
.empty-restore {
  margin-top: 12px;
  border: 1px solid var(--dc-border);
  background: var(--dc-panel);
  color: var(--dc-text);
  font-size: 13px;
  line-height: 1;
  padding: 8px 14px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.12s ease;
}
.empty-restore:hover {
  border-color: v-bind("palette.accent");
  color: v-bind("palette.accentHover");
}
</style>
