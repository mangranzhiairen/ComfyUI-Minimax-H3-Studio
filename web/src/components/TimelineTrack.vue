<script setup lang="ts">
import { computed, ref } from "vue";
import { storeToRefs } from "pinia";
import { useTimelineStore } from "@/stores/timeline";
import { useDrag } from "@/composables/useDrag";
import ClipCard from "./ClipCard.vue";
import { DURATION_LIMITS } from "@/types/timeline";
import { palette } from "@/styles/theme";

const store = useTimelineStore();
const { clips, zoom, selectedId, totalDurationSec } = storeToRefs(store);

// ---------- 排序拖拽状态 ----------
const dragState = ref<{
  id: string;
  startIndex: number;
  dx: number;
  targetIndex: number;
} | null>(null);

/** 排序拖拽：拖动片段跟随指针 + 插入指示线，松手落位 */
const sortDrag = useDrag({
  onStart(e) {
    const el = e.currentTarget as HTMLElement;
    const id = el.dataset.clipId;
    if (!id) return;
    const idx = clips.value.findIndex((s) => s.id === id);
    if (idx === -1) return;
    store.select(id);
    dragState.value = { id, startIndex: idx, dx: 0, targetIndex: idx };
  },
  onMove(_e, dx) {
    const st = dragState.value;
    if (!st) return;
    st.dx = dx;
    const len = clips.value.length;
    const avgWidth = (totalDurationSec.value * zoom.value) / len;
    let target = st.startIndex + Math.round(dx / avgWidth);
    st.targetIndex = Math.max(0, Math.min(len - 1, target));
  },
  onEnd() {
    const st = dragState.value;
    if (st) {
      const from = clips.value.findIndex((s) => s.id === st.id);
      if (from !== -1 && from !== st.targetIndex) {
        store.moveClip(from, st.targetIndex);
      }
    }
    dragState.value = null;
  },
});

/** 插入指示线位置（目标槽位左侧） */
const dropLineX = computed(() => {
  const st = dragState.value;
  if (!st) return 0;
  let x = 0;
  for (let i = 0; i < st.targetIndex; i++) {
    x += clips.value[i].durationSec * zoom.value;
  }
  return x;
});

// ---------- 右缘拖拽调时长 ----------
const resizeState = ref<{ id: string; startDur: number } | null>(null);

const resizeDrag = useDrag({
  threshold: 2,
  onStart(e) {
    const el = e.currentTarget as HTMLElement;
    const id = el.dataset.clipId;
    const seg = id ? clips.value.find((s) => s.id === id) : undefined;
    if (!seg || !id) return;
    store.select(id);
    resizeState.value = { id, startDur: seg.durationSec };
  },
  onMove(_e, dx) {
    const st = resizeState.value;
    const seg = st && clips.value.find((s) => s.id === st.id);
    if (!st || !seg) return;
    const delta = dx / zoom.value;
    let dur = st.startDur + delta;
    dur = Math.round(dur / DURATION_LIMITS.stepSec) * DURATION_LIMITS.stepSec;
    store.updateClip(seg.id, { durationSec: dur });
  },
  onEnd() {
    resizeState.value = null;
  },
});

const dragging = computed(() => dragState.value !== null);
</script>

<template>
  <div class="track" :class="{ 'dc-no-select': dragging }">
    <TransitionGroup name="clip-move" tag="div" class="track-clips">
      <ClipCard
        v-for="seg in clips"
        :key="seg.id"
        :clip="seg"
        :width-px="seg.durationSec * zoom"
        :selected="seg.id === selectedId"
        :dragging="dragState?.id === seg.id"
        :drag-x="dragState?.id === seg.id ? dragState.dx : 0"
        :drag-handlers="sortDrag.bind"
        :resize-handlers="resizeDrag.bind"
      />
    </TransitionGroup>

    <!-- 拖拽插入指示线 -->
    <div
      v-if="dragState"
      class="drop-line"
      :style="{ left: `${dropLineX}px` }"
    ></div>
  </div>
</template>

<style scoped>
.track {
  position: relative;
  min-height: 228px;
  padding: 4px 0;
  background: var(--dc-bg);
}

.track-clips {
  display: flex;
  align-items: stretch;
  gap: 0;
  min-height: 220px;
}

.clip-move {
  transition: transform 0.18s ease;
}

.drop-line {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  background: v-bind("palette.accent");
  border-radius: 1px;
  pointer-events: none;
  box-shadow: 0 0 6px v-bind("palette.accent");
}
</style>
