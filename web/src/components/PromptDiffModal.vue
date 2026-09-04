<script setup lang="ts">
/** 提示词版本比对（git diff 风格）：A（旧）vs B（新）。
 *  纯展示组件：文本字符级 diff（token 原子）+ 素材槽变更清单 + 模式/时长摘要；
 *  覆盖确认由父组件（PromptHistoryPanel）统一处理（emit apply）。 */
import { computed } from "vue";
import type { ClipVersion } from "@/types/timeline";
import { diffPrompts, mediaSlotChanges, snapshotMetaDiff, type DiffRun } from "@/utils/promptDiff";

const props = defineProps<{
  show: boolean;
  /** 旧版本（左侧，被删除词出自这里） */
  a: ClipVersion | null;
  /** 新版本（右侧，新增词出自这里；「应用」使用它） */
  b: ClipVersion | null;
}>();
const emit = defineEmits<{
  (e: "update:show", v: boolean): void;
  (e: "apply", versionId: number): void;
}>();

/** 时间戳 → 短时间（弹窗展示） */
function fmtTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 提示词摘要（无则空占位） */
function promptSummary(snap: { prompt: string } | undefined): string {
  return snap?.prompt || "（空提示词）";
}

/** diff 段（左侧旧版 = del 段即为删除） */
const runs = computed<DiffRun[]>(() => {
  const a = props.a?.snapshot?.prompt ?? "";
  const b = props.b?.snapshot?.prompt ?? "";
  if (a === b) return [];
  return diffPrompts(a, b);
});

/** 素材槽变更（换参考图等文本 diff 感知不到的差异） */
const slotChanges = computed(() => {
  if (!props.a?.snapshot || !props.b?.snapshot) return [];
  return mediaSlotChanges(props.a.snapshot, props.b.snapshot);
});

/** mode/时长差异摘要 */
const meta = computed(() => {
  if (!props.a?.snapshot || !props.b?.snapshot) return null;
  return snapshotMetaDiff(props.a.snapshot, props.b.snapshot);
});

/** 素材相对路径 → ComfyUI 图片缩略 URL（image 才显示；video/audio 用图标） */
function imageThumb(path?: string): string | undefined {
  if (!path) return undefined;
  const parts = path.split("/");
  const filename = parts.pop() ?? path;
  const params = new URLSearchParams({ filename, type: "input", preview: "webp" });
  if (parts.length) params.set("subfolder", parts.join("/"));
  return `/view?${params.toString()}`;
}

/** 文件名（path 尾部） */
function baseName(path?: string): string {
  return path?.split("/").pop() ?? "";
}

const KIND_ICON = { image: "🖼️", video: "🎞️", audio: "🎵" } as const;
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    :title="`版本比对 · ${promptSummary(a?.snapshot).slice(0, 14)}`"
    :bordered="false"
    class="diff-modal"
    style="width: 720px"
    @update:show="(v: boolean) => v || emit('update:show', false)"
  >
    <div v-if="!a || !b" class="diff-empty">缺少对比版本</div>
    <div v-else class="diff-body">
      <!-- 头部：A vs B + mode/时长摘要 -->
      <div class="diff-meta">
        <div class="diff-side">
          <span class="diff-role">旧</span>
          <span class="diff-time">{{ fmtTime(a.createdAt) }}</span>
        </div>
        <div class="diff-arrow">→</div>
        <div class="diff-side">
          <span class="diff-role new">新</span>
          <span class="diff-time">{{ fmtTime(b.createdAt) }}</span>
        </div>
        <span v-if="meta?.modeChanged" class="diff-meta-hint">
          模式 {{ a.snapshot.mode }} → {{ b.snapshot.mode }}
        </span>
      </div>

      <!-- 文本 diff（字符级，token 原子） -->
      <div v-if="runs.length" class="diff-text">
        <span
          v-for="(r, i) in runs"
          :key="i"
          :class="['diff-run', `d-${r.kind}`, { 'd-token': /^<[A-Za-z]+\s+\d+>$/.test(r.text) }]"
        >{{ r.text }}</span>
      </div>
      <div v-else class="diff-text same">两版提示词文本一致（差异在素材/模式/时长）</div>

      <!-- 素材槽变更清单 -->
      <div v-if="slotChanges.length" class="diff-slots">
        <div class="diff-slots-title">素材变更</div>
        <div v-for="(c, i) in slotChanges" :key="i" class="diff-slot-row">
          <span class="diff-slot-name">{{ c.slot }}</span>
          <span class="diff-slot-media">{{ baseName(c.old?.path) || "（无）" }}</span>
          <img v-if="c.kind === 'image' && imageThumb(c.old?.path)" class="diff-thumb" :src="imageThumb(c.old?.path)" alt="" />
          <span v-else-if="c.kind !== 'image' && c.old" class="diff-thumb-icon">{{ KIND_ICON[c.kind] }}</span>
          <span class="diff-slot-arrow">→</span>
          <span class="diff-slot-media">{{ baseName(c.new?.path) || "（无）" }}</span>
          <img v-if="c.kind === 'image' && imageThumb(c.new?.path)" class="diff-thumb" :src="imageThumb(c.new?.path)" alt="" />
          <span v-else-if="c.kind !== 'image' && c.new" class="diff-thumb-icon">{{ KIND_ICON[c.kind] }}</span>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="diff-footer">
        <span class="diff-footer-hint">红色 = 旧版删除 · 绿色 = 新版新增</span>
        <button class="diff-cancel" @click="emit('update:show', false)">关闭</button>
        <button class="diff-apply" @click="emit('apply', b?.versionId ?? 0)">使用新版</button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.diff-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 62vh;
  overflow-y: auto;
  padding: 2px;
}
.diff-empty {
  font-size: 12px;
  color: var(--dc-text-faint);
  padding: 16px 0;
  text-align: center;
}
.diff-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}
.diff-side {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.diff-role {
  font-size: 10px;
  font-weight: 700;
  color: #fca5a5;
  border: 1px solid rgba(248, 113, 113, 0.45);
  border-radius: 4px;
  padding: 1px 6px;
}
.diff-role.new {
  color: #86efac;
  border-color: rgba(74, 222, 128, 0.45);
}
.diff-time {
  color: var(--dc-text-dim);
  font-variant-numeric: tabular-nums;
}
.diff-arrow {
  color: var(--dc-text-faint);
}
.diff-meta-hint {
  color: var(--dc-text-faint);
  font-size: 11px;
}
.diff-text {
  font-size: 13px;
  line-height: 1.9;
  color: var(--dc-text);
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.02);
  word-break: break-word;
}
.diff-text.same {
  color: var(--dc-text-faint);
}
.diff-run {
  white-space: pre-wrap;
}
.d-del {
  background: rgba(248, 113, 113, 0.16);
  color: #fca5a5;
  text-decoration: line-through;
  border-radius: 3px;
}
.d-add {
  background: rgba(74, 222, 128, 0.14);
  color: #86efac;
  border-radius: 3px;
}
.d-token {
  outline: 1px solid currentColor;
  outline-offset: 1px;
  padding: 0 2px;
}
.diff-slots {
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.diff-slots-title {
  font-size: 11px;
  font-weight: 600;
  color: #93c5fd;
}
.diff-slot-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  flex-wrap: wrap;
}
.diff-slot-name {
  color: var(--dc-text-dim);
  flex-shrink: 0;
  min-width: 64px;
}
.diff-slot-media {
  color: var(--dc-text);
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.diff-slot-arrow {
  color: var(--dc-text-faint);
}
.diff-thumb {
  width: 64px;
  height: 36px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid rgba(255, 255, 255, 0.14);
}
.diff-thumb-icon {
  font-size: 16px;
}
.diff-footer {
  display: flex;
  align-items: center;
  gap: 8px;
}
.diff-footer-hint {
  flex: 1;
  font-size: 11px;
  color: var(--dc-text-faint);
}
.diff-cancel,
.diff-apply {
  border: 1px solid var(--dc-border);
  background: var(--dc-bg);
  color: var(--dc-text);
  border-radius: 6px;
  font-size: 13px;
  line-height: 1;
  padding: 7px 14px;
  cursor: pointer;
  transition: all 0.12s ease;
}
.diff-cancel:hover,
.diff-apply:hover {
  border-color: var(--dc-accent);
  color: var(--dc-accent-hover);
}
.diff-apply {
  background: rgba(74, 222, 128, 0.14);
  border-color: rgba(74, 222, 128, 0.45);
  color: #86efac;
}
</style>
