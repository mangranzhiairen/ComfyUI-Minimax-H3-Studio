<script setup lang="ts">
import { computed } from "vue";
import { storeToRefs } from "pinia";
import { useTimelineStore } from "@/stores/timeline";
import { palette } from "@/styles/theme";

const store = useTimelineStore();
const { clips, zoom, totalDurationSec } = storeToRefs(store);

/** 主刻度候选步长（秒），保证刻度间隔不小于 56px */
const STEP_CANDIDATES = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];

const mainStep = computed(() => {
  for (const s of STEP_CANDIDATES) {
    if (s * zoom.value >= 56) return s;
  }
  return 600;
});

const subStep = computed(() => mainStep.value / 4);

interface Tick {
  time: number;
  x: number;
  isMajor: boolean;
}

const ticks = computed<Tick[]>(() => {
  const result: Tick[] = [];
  const total = totalDurationSec.value;
  const sub = subStep.value;
  for (let t = 0; t <= total + 1e-6; t += sub) {
    const isMajor = Math.abs(t % mainStep.value) < 1e-6;
    result.push({ time: t, x: t * zoom.value, isMajor });
  }
  return result;
});

function formatTime(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** 每个片段的序号和起点，用于标尺上的编号标签 */
const segStarts = computed(() => {
  const starts: { x: number; label: string; id: string }[] = [];
  let acc = 0;
  for (const seg of clips.value) {
    starts.push({ x: acc * zoom.value, label: "#" + (starts.length + 1), id: seg.id });
    acc += seg.durationSec;
  }
  return starts;
});
</script>

<template>
  <div class="ruler">
    <div
      v-for="seg in segStarts"
      :key="seg.id"
      class="ruler-seg-label"
      :style="{
        left: `${seg.x}px`,
        // 起点标签贴合左边缘；其余居中在片段边界上
        transform: seg.x === 0 ? 'none' : 'translateX(-50%)',
      }"
    >
      {{ seg.label }}
    </div>
    <div
      v-for="tick in ticks"
      :key="tick.time"
      class="ruler-tick"
      :class="{ major: tick.isMajor }"
      :style="{ left: `${tick.x}px` }"
    >
      <span v-if="tick.isMajor" class="ruler-tick-label">{{ formatTime(tick.time) }}</span>
    </div>
  </div>
</template>

<style scoped>
.ruler {
  position: relative;
  height: 30px;
  background: var(--dc-panel);
  border-bottom: 1px solid var(--dc-border);
  overflow: hidden;
  min-width: 100%;
}

.ruler-tick {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--dc-border);
}
.ruler-tick.major {
  background: var(--dc-border-strong);
  height: 8px;
}

.ruler-tick-label {
  position: absolute;
  top: 3px;
  left: 4px;
  font-size: 10px;
  color: v-bind("palette.textFaint");
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.ruler-seg-label {
  position: absolute;
  top: 14px;
  left: 0;
  transform: translateX(-50%);
  font-size: 9px;
  color: v-bind("palette.accent");
  background: v-bind("palette.accentDim");
  padding: 0 4px;
  border-radius: 4px;
  line-height: 13px;
  white-space: nowrap;
  pointer-events: none;
}
</style>
