<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import { storeToRefs } from "pinia";
import { useMessage } from "naive-ui";
import { useTimelineStore } from "@/stores/timeline";
import type { ClipMode } from "@/types/timeline";

const store = useTimelineStore();
const message = useMessage();
const { showRestoreModal, clips, historyByClipId } = storeToRefs(store);

/** 时间戳 → 短时间（版本下拉展示） */
function fmtTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 面板列表项：历史中的一个 clip 身份（含全部历史版本，供手动挑选） */
type RestoreOption = {
  clipId: string;
  mode: ClipMode;
  prompt: string;
  durationSec: number;
  sampleCount: number;
  /** 该 clip 是否已在当前时间线（在则禁选，避免重复） */
  inTimeline: boolean;
  versions: { versionId: number; createdAt: number; label: string }[];
};

/** 列表：有快照的 clip，按最早版本创建时间排序（近似原时间线顺序） */
const options = computed<RestoreOption[]>(() => {
  const list: RestoreOption[] = [];
  for (const [clipId, h] of Object.entries(historyByClipId.value)) {
    if (!h.versions.length) continue;
    const latest = h.versions[0];
    const snap = latest.snapshot;
    // 条目快照无时长（规格随样本）：恢复卡片时长取该片段最近采样规格，无则默认 4s
    const durationSec = h.samples[0]?.durationSec ?? 4;
    list.push({
      clipId,
      mode: snap.mode,
      prompt: snap.prompt ?? "",
      durationSec,
      sampleCount: h.samples.length,
      inTimeline: clips.value.some((c) => c.id === clipId),
      versions: h.versions.map((v) => ({
        versionId: v.versionId,
        createdAt: v.createdAt,
        label: `v${v.versionId} · ${fmtTime(v.createdAt)}${
          v.versionId === h.versions[0].versionId ? "（最近）" : ""
        }`,
      })),
    });
  }
  list.sort(
    (a, b) =>
      (a.versions[a.versions.length - 1]?.createdAt ?? 0) -
      (b.versions[b.versions.length - 1]?.createdAt ?? 0),
  );
  return list;
});

/** 勾选状态：clipId → 是否恢复该卡片 */
const checked = reactive<Record<string, boolean>>({});
/** 版本选择：clipId → 恢复用版本（默认最新） */
const chosenVer = reactive<Record<string, number>>({});

/** 面板打开：刷新历史 + 重置勾选/版本（每次打开重新挑选） */
watch(showRestoreModal, (open) => {
  if (!open) return;
  void store.fetchHistory();
  for (const k of Object.keys(checked)) delete checked[k];
  for (const k of Object.keys(chosenVer)) delete chosenVer[k];
  for (const opt of options.value) {
    chosenVer[opt.clipId] = opt.versions[0]?.versionId ?? 0;
  }
});

function toggleCheck(clipId: string, on: boolean) {
  checked[clipId] = on;
}

/** 可恢复的勾选数（不含已在时间线的） */
const checkedCount = computed(
  () => options.value.filter((o) => checked[o.clipId] && !o.inTimeline).length,
);

/** 恢复所选：按列表顺序（近似原时间线顺序）追加到时间线末尾 */
async function confirmRestore() {
  const selections = options.value
    .filter((o) => checked[o.clipId] && !o.inTimeline)
    .map((o) => ({
      clipId: o.clipId,
      versionId: chosenVer[o.clipId] ?? o.versions[0]?.versionId ?? 0,
    }));
  if (!selections.length) return;
  const n = await store.addClipsFromHistory(selections);
  if (n > 0) {
    message.success(`已恢复 ${n} 个片段到时间线末尾`);
    store.closeRestoreModal();
  } else {
    message.info("没有可恢复的片段（均已在时间线或历史快照缺失）");
  }
}
</script>

<template>
  <n-modal
    :show="store.showRestoreModal"
    preset="card"
    title="从历史恢复片段"
    :bordered="false"
    class="restore-modal"
    style="width: 720px"
    @update:show="(v: boolean) => v || store.closeRestoreModal()"
  >
    <div class="restore-body">
      <div v-if="!options.length" class="restore-empty">
        该任务没有可恢复的提示词快照（片段采样后才会固化提示词历史）
      </div>
      <template v-else>
        <div class="restore-hint">
          勾选要恢复的片段，每行可选历史版本（默认最近）；恢复后追加到时间线末尾，不覆盖已有卡片
        </div>
        <div class="restore-list">
          <div
            v-for="opt in options"
            :key="opt.clipId"
            class="restore-item"
            :class="{ disabled: opt.inTimeline }"
          >
            <input
              type="checkbox"
              class="restore-check"
              :disabled="opt.inTimeline"
              :checked="!!checked[opt.clipId]"
              @change="(e: Event) => toggleCheck(opt.clipId, (e.target as HTMLInputElement).checked)"
            />
            <span class="mode-badge">{{ opt.mode }}</span>
            <span class="restore-prompt" :title="opt.prompt">{{ opt.prompt || "（空提示词）" }}</span>
            <span class="restore-meta">{{ opt.durationSec }}s · {{ opt.sampleCount }} 抽</span>
            <n-select
              size="small"
              class="ver-select"
              :disabled="opt.inTimeline || !checked[opt.clipId]"
              :value="chosenVer[opt.clipId] ?? opt.versions[0]?.versionId"
              :options="opt.versions.map((v) => ({ label: v.label, value: v.versionId }))"
              @update:value="(v: number) => (chosenVer[opt.clipId] = v)"
            />
            <span v-if="opt.inTimeline" class="in-timeline">已在时间线</span>
          </div>
        </div>
      </template>
    </div>
    <template #footer>
      <div class="restore-footer">
        <span class="restore-footer-hint">恢复的卡片 id 沿用历史，采样记录继续跟随</span>
        <button class="restore-cancel" @click="store.closeRestoreModal()">取消</button>
        <button class="restore-confirm" :disabled="!checkedCount" @click="confirmRestore">
          恢复所选（{{ checkedCount }}）
        </button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.restore-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.restore-empty {
  font-size: 12px;
  color: var(--dc-text-faint);
  padding: 16px 0;
  text-align: center;
}
.restore-hint {
  font-size: 11px;
  color: var(--dc-text-faint);
  padding-bottom: 4px;
  border-bottom: 1px solid var(--dc-border);
}
.restore-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 52vh;
  overflow-y: auto;
  padding: 2px;
}
.restore-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
  min-width: 0;
}
.restore-item.disabled {
  opacity: 0.45;
}
.restore-check {
  width: 13px;
  height: 13px;
  margin: 0;
  cursor: pointer;
  accent-color: var(--dc-accent);
  flex-shrink: 0;
}
.restore-check:disabled {
  cursor: not-allowed;
}
.mode-badge {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 600;
  color: #93c5fd;
  padding: 2px 6px;
  background: rgba(96, 165, 250, 0.14);
  border-radius: 5px;
}
.restore-prompt {
  flex: 1;
  font-size: 12px;
  color: var(--dc-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.restore-meta {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
}
.ver-select {
  width: 170px;
  flex-shrink: 0;
}
.in-timeline {
  flex-shrink: 0;
  font-size: 10px;
  color: #4ade80;
  border: 1px solid rgba(74, 222, 128, 0.4);
  border-radius: 3px;
  padding: 0 4px;
}
.restore-footer {
  display: flex;
  align-items: center;
  gap: 8px;
}
.restore-footer-hint {
  flex: 1;
  font-size: 11px;
  color: var(--dc-text-faint);
}
/* 注意：本组件内容在 n-modal 内（teleport 到 body），不能用 v-bind("palette.*")——
 * 局部 CSS 变量绑定在组件根元素上，teleport 后失效；必须用全局 :root 变量（--dc-*）。 */
.restore-cancel,
.restore-confirm {
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
.restore-cancel:hover,
.restore-confirm:hover:not(:disabled) {
  border-color: var(--dc-accent);
  color: var(--dc-accent-hover);
}
.restore-confirm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
