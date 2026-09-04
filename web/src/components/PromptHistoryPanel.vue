<script setup lang="ts">
/** 片段历史面板（提示词主轴）：左 = 提示词条目列表（采样固化的画面语义快照）；
 *  右 = 选中条目的画面语义摘要 + 该条目下采样结果墙（当前画布可用，点击锁定/解锁）；
 *  含版本比对入口（⧉ 选两个条目 → PromptDiffModal）与覆盖确认（统一 useDialog）。
 *  概念收敛：历史只有「提示词条目 → 采样记录」两层；分辨率不是维度（失效样本沉底），
 *  采样工艺（seed/steps…）只是样本元数据，无独立"恢复参数"按钮——应用条目/启用样本
 *  时把画面语义回填编辑面板（覆盖确认由本组件弹窗），执行态/编排保持片段当前值。 */
import { computed, ref, watch } from "vue";
import { useDialog, useMessage } from "naive-ui";
import { useTimelineStore } from "@/stores/timeline";
import PromptDiffModal from "./PromptDiffModal.vue";
import PreviewThumb from "./PreviewThumb.vue";
import { usePreviewPlayer } from "@/composables/usePreviewPlayer";
import type { Clip, ClipVersion, VersionSample } from "@/types/timeline";

const props = defineProps<{ clip: Clip }>();
const store = useTimelineStore();
const dialog = useDialog();
const message = useMessage();

// ---------- 数据视图 ----------

const EMPTY = { versions: [], samples: [] };
const history = computed(() => store.historyByClipId[props.clip.id] ?? EMPTY);
/** 提示词条目（versions 已按创建时间倒序 = 越新越靠前） */
const entries = computed<ClipVersion[]>(() => history.value.versions);
/** 当前画布标签（失效样本判定） */
const currentCanvasLabel = computed(
  () => `${store.canvas.width}x${store.canvas.height}@${store.canvas.fps}`,
);

/** 最近采样归属的条目（"最近"徽标语义 = 最近采过，不是最近创建条目） */
const latestSampledVersionId = computed(() => history.value.samples[0]?.versionId ?? null);

// ---------- 条目浏览 ----------

const selectedId = ref<number | null>(null);
const selectedEntry = computed<ClipVersion | null>(() => {
  if (selectedId.value == null) return null;
  return entries.value.find((v) => v.versionId === selectedId.value) ?? null;
});

/** 条目列表变化时：默认选中最近条目；列表清空则取消选择 */
watch(
  () => entries.value[0]?.versionId ?? null,
  (first) => {
    if (first == null) selectedId.value = null;
    else if (selectedId.value == null) selectedId.value = first;
  },
);

/** 某条目下全部采样（按时间倒序已在接口层排好） */
function samplesOf(versionId: number): VersionSample[] {
  return history.value.samples.filter((s) => s.versionId === versionId);
}
/** 结果墙样本：该条目全部采样；与当前画布一致者排前（不同画布卡上标画布，启用时提示切画布） */
function shotSamplesOf(versionId: number): VersionSample[] {
  const cur = currentCanvasLabel.value;
  return [...samplesOf(versionId)].sort((a, b) => {
    const da = a.canvas === cur ? 0 : 1;
    const db = b.canvas === cur ? 0 : 1;
    return da - db;
  });
}

function isLocked(sample: VersionSample): boolean {
  return props.clip.sampleFp === sample.sampleFp;
}
function selectEntry(versionId: number): void {
  selectedId.value = versionId;
}

// ---------- 覆盖确认（应用条目 / 启用样本统一入口） ----------

/** 当前片段内容（不含时长/画布——规格随样本）是否与目标快照一致 */
function contentEquals(snap: ClipVersion["snapshot"]): boolean {
  const seg = props.clip;
  if (!snap || snap.mode !== seg.mode || snap.prompt !== seg.prompt) return false;
  const eqOne = (a?: { path?: string }, b?: { path?: string }) => {
    const pa = a?.path ?? "";
    const pb = b?.path ?? "";
    return pa === pb;
  };
  const eqList = (a?: { path?: string }[], b?: { path?: string }[]) => {
    const la = a ?? [];
    const lb = b ?? [];
    if (la.length !== lb.length) return false;
    return la.every((m, i) => m.path === lb[i]?.path);
  };
  return (
    eqOne(snap.firstFrame, seg.firstFrame) &&
    eqOne(snap.lastFrame, seg.lastFrame) &&
    eqOne(snap.sourceVideo, seg.sourceVideo) &&
    eqList(snap.refImages, seg.refImages) &&
    eqList(snap.refVideos, seg.refVideos) &&
    eqList(snap.refAudios, seg.refAudios)
  );
}

/** 通用确认弹窗；返回是否放行。
 *  resolve 覆盖全部关闭路径：确认 / 取消 / 遮罩 / ESC / 关闭（naive 的 onClose 并非
 *  所有关闭路径都触发，必须逐个显式挂 onNegativeClick/onMaskClick/onEsc 才能可靠 resolve）。 */
function askConfirm(title: string, content: string, positive = "确认"): Promise<boolean> {
  return new Promise((resolve) => {
    dialog.warning({
      title,
      content,
      positiveText: positive,
      negativeText: "取消",
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onMaskClick: () => resolve(false),
      onEsc: () => resolve(false),
      onClose: () => resolve(false),
    });
  });
}

/** 应用提示词条目：仅回填画面语义 + 解锁（时长/分辨率保持当前——规格随样本，不随条目恢复） */
async function onApplyEntry(entry: ClipVersion): Promise<void> {
  const snap = entry.snapshot;
  if (!contentEquals(snap)) {
    const ok = await askConfirm(
      "覆盖当前内容？",
      `将用该提示词版本「${(snap?.prompt ?? "").slice(0, 24) || "空提示词"}」覆盖当前片段的提示词与素材（时长/分辨率保持当前，不随条目恢复）。当前草稿未采样不会留存。`,
    );
    if (!ok) return;
  }
  if (store.loadPromptEntry(props.clip.id, entry.versionId)) {
    message.success("已应用该提示词内容（解锁，将重新采样）");
  }
}

/** 启用/停用采样：勾选 = 分别确认分辨率/时长覆盖 → 回填条目内容 → 锁定 latent（出片跳过采样）
 *  勾选后弹确认框为异步：取消路径不写 store（:checked 绑 isLocked 无变化不重渲染），
 *  需手动把 checkbox DOM 复位，避免“弹窗取消后仍显示勾选”。 */
async function onToggleSample(
  sample: VersionSample,
  checked: boolean,
  input: HTMLInputElement | null = null,
): Promise<void> {
  /** 取消路径：复位 checkbox DOM（store 未变，单向绑定不会自动还原） */
  const bail = (): void => {
    if (input && input.checked) input.checked = false;
  };
  if (!checked) {
    store.releaseSample(props.clip.id);
    return;
  }
  if (!sample.exists) return;
  const seg = props.clip;
  const snap = store.promptSnapshotOf(props.clip.id, sample.versionId);
  if (!snap) return;

  // 1) 分辨率不匹配 → 单独确认（切画布是全局动作，会清除其它片段锁定）
  const canvasNow = currentCanvasLabel.value;
  if (sample.canvas && sample.canvas !== canvasNow) {
    const ok = await askConfirm(
      "任务分辨率不匹配",
      `该 latent 基于 ${sample.canvas}，当前任务分辨率为 ${canvasNow}。将任务分辨率切换到 ${sample.canvas}？（影响全部片段并清除其它已锁定缓存）`,
      "切换并选用",
    );
    if (!ok) {
      message.info("已取消——画布不匹配的 latent 无法出片");
      bail();
      return;
    }
    const m = /^(\d+)x(\d+)@(\d+)$/.exec(sample.canvas);
    if (!m) {
      bail();
      return;
    }
    store.updateCanvas({ width: Number(m[1]), height: Number(m[2]), fps: Number(m[3]) });
  }

  // 2) 时长不匹配 → 单独确认（片段时长改为该样本出片长度）
  if (sample.durationSec > 0 && Math.abs(sample.durationSec - seg.durationSec) > 1e-6) {
    const ok = await askConfirm(
      "片段时长不匹配",
      `该 latent 的出片长度为 ${sample.durationSec}s，当前片段时长为 ${seg.durationSec}s。将片段时长改为 ${sample.durationSec}s？`,
      "改用样本时长",
    );
    if (!ok) {
      message.info("已取消——片段时长需与该 latent 一致");
      bail();
      return;
    }
  }

  // 3) 内容不一致 → 覆盖确认（提示词/素材随样本所属条目）
  if (!contentEquals(snap)) {
    const ok = await askConfirm(
      "覆盖当前内容？",
      `将用历史版本「${(snap.prompt ?? "").slice(0, 24) || "空提示词"}」覆盖当前片段的提示词与素材。当前草稿未采样不会留存。`,
    );
    if (!ok) {
      bail();
      return;
    }
  }

  if (!store.applySample(props.clip.id, sample)) {
    bail();
    return;
  }
  message.success(`已选用 seed ${sample.seed}（${sample.canvas || "?"} · ${sample.durationSec || "?"}s，出片跳过采样）`);
}

// ---------- 版本比对 ----------

const diffBaseId = ref<number | null>(null);
const diffShow = ref(false);
const diffA = ref<ClipVersion | null>(null);
const diffB = ref<ClipVersion | null>(null);

const pickingDiff = computed(() => diffBaseId.value != null);

/** 点条目行 ⧉：进入选择第二个版本模式（按时间旧→新排序为 A→B） */
function beginDiff(entry: ClipVersion): void {
  if (entries.value.length < 2) {
    message.info("至少需要两个提示词版本才能比对");
    return;
  }
  diffBaseId.value = entry.versionId; // 进入选择模式：该条为对比基
}

function cancelDiffPick(): void {
  diffBaseId.value = null;
}

/** 查看模式点行 = 查看该条目；比对模式点行 = 选对比对象（点对比基行 = 取消选择） */
function onEntryClick(entry: ClipVersion): void {
  if (!pickingDiff.value) {
    selectEntry(entry.versionId);
    return;
  }
  resolveDiffPick(entry);
}

/** ⧉ 比对按钮：查看模式 = 以该条为基进入选择；比对模式 = 直接与基开对比（点基 = 取消） */
function onCompareClick(entry: ClipVersion): void {
  if (entries.value.length < 2) {
    message.info("至少需要两个提示词版本才能比对");
    return;
  }
  if (!pickingDiff.value) {
    beginDiff(entry);
    return;
  }
  resolveDiffPick(entry);
}

/** 打开 A(旧) vs B(新) 对比框并退出选择模式 */
function resolveDiffPick(entry: ClipVersion): void {
  if (diffBaseId.value == null) return;
  if (entry.versionId === diffBaseId.value) {
    cancelDiffPick();
    return;
  }
  const base = entries.value.find((v) => v.versionId === diffBaseId.value);
  if (!base) return;
  const [a, b] =
    base.createdAt <= entry.createdAt ? [base, entry] : [entry, base];
  diffA.value = a;
  diffB.value = b;
  diffBaseId.value = null;
  diffShow.value = true;
}

/** 该条目下是否存在当前锁定的采样（条目前绿点：此版本有被选用的 latent） */
function hasLockedSample(entry: ClipVersion): boolean {
  return samplesOf(entry.versionId).some((s) => isLocked(s));
}

/** diff 弹窗「使用新版」→ 覆盖确认后应用 */
async function onDiffApply(versionId: number): Promise<void> {
  const ver = entries.value.find((v) => v.versionId === versionId);
  if (!ver) return;
  diffShow.value = false;
  await onApplyEntry(ver);
}

// ---------- 删除（条目 / 样本，二次确认） ----------

function onDeleteEntry(entry: ClipVersion): void {
  const n = samplesOf(entry.versionId).length;
  dialog.warning({
    title: "删除该提示词版本",
    content: `将删除该版本及其 ${n} 份采样记录${n ? "（含无引用 latent/预览缓存）" : ""}，不可恢复。确定删除？`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      const ok = await store.deleteVersion(props.clip.id, entry.versionId);
      if (!ok) message.warning("删除失败，历史未变更");
      if (selectedId.value === entry.versionId) selectedId.value = null;
    },
  });
}

function onDeleteSample(sample: VersionSample): void {
  dialog.warning({
    title: "删除该次采样",
    content: `将删除 seed ${sample.seed}${sample.durationSec ? ` · ${sample.durationSec}s` : ""} 的采样记录${sample.exists ? "及其 latent/预览缓存" : ""}，不可恢复。确定删除？`,
    positiveText: "删除",
    negativeText: "取消",
    onPositiveClick: async () => {
      const ok = await store.deleteSample(props.clip.id, sample.sampleFp);
      if (!ok) message.warning("删除失败，历史未变更");
    },
  });
}

// ---------- 样本预览（缩略图点击 → 弹窗播放动画 WebP） ----------

const previewSample = ref<VersionSample | null>(null);
const previewCanvas = ref<HTMLCanvasElement | null>(null);
const { playFrom: playPreview, stop: stopPreview } = usePreviewPlayer(previewCanvas);

function canvasFpsOf(canvasStr?: string): number {
  const m = /@(\d+)$/.exec(canvasStr ?? "");
  return m ? Number(m[1]) : 24;
}

function openPreview(sample: VersionSample): void {
  if (!sample.previewUrl || !sample.exists) return;
  previewSample.value = sample;
  const duration = sample.sampleLen / canvasFpsOf(sample.canvas);
  void fetch(sample.previewUrl)
    .then((r) => (r.ok ? r.blob() : null))
    .then((b) => {
      if (b && previewSample.value) void playPreview(b, duration);
    })
    .catch(() => {
      /* 预览是增强信息，失败忽略 */
    });
}

function closePreview(): void {
  previewSample.value = null;
  stopPreview();
}

// ---------- 展示辅助 ----------

function fmtTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function promptSummary(entry: ClipVersion): string {
  const p = entry.snapshot?.prompt ?? "";
  return p ? (p.length > 18 ? `${p.slice(0, 18)}…` : p) : "（空提示词）";
}

/** 素材缩略行（快照各槽素材，图片缩略 / 音视频图标） */
const snapMediaRow = computed<{ preview?: string; icon: string; label: string }[]>(() => {
  const snap = selectedEntry.value?.snapshot;
  if (!snap) return [];
  const out: { preview?: string; icon: string; label: string }[] = [];
  const iconOf = (kind: string) => (kind === "image" ? "🖼️" : kind === "video" ? "🎞️" : "🎵");
  const push = (m: { path: string; kind: string } | undefined, label: string) => {
    if (!m) return;
    const item = { icon: iconOf(m.kind), label, preview: undefined as string | undefined };
    if (m.kind === "image") {
      const parts = m.path.split("/");
      const filename = parts.pop() ?? m.path;
      const params = new URLSearchParams({ filename, type: "input", preview: "webp" });
      if (parts.length) params.set("subfolder", parts.join("/"));
      item.preview = `/view?${params.toString()}`;
    }
    out.push(item);
  };
  push(snap.firstFrame as never, "首帧");
  push(snap.lastFrame as never, "尾帧");
  snap.refImages?.forEach((m, i) => push(m as never, `图${i + 1}`));
  snap.refVideos?.forEach((m, i) => push(m as never, `视${i + 1}`));
  snap.refAudios?.forEach((m, i) => push(m as never, `音${i + 1}`));
  push(snap.sourceVideo as never, "源视频");
  return out;
});
</script>

<template>
  <div class="ph">
    <!-- 空态 -->
    <div v-if="!entries.length" class="ph-empty">
      暂无采样——采样后自动存档；每版提示词下的采样结果会直接展示在这里
    </div>

    <template v-else>
      <!-- 比对模式提示条 -->
      <div v-if="pickingDiff" class="ph-pickbar">
        <span>对比基：{{ entries.find((v) => v.versionId === diffBaseId)?.snapshot?.prompt?.slice(0, 14) || "（空）" }}——点另一个版本（或其 ⧉ 比对）开始比对；再点对比基取消</span>
        <button class="mini-btn" @click="cancelDiffPick">取消</button>
      </div>

      <div class="ph-main">
        <!-- 左：提示词条目列表 -->
        <div class="ph-list">
          <div class="ph-pane-title">提示词历史</div>
          <div
            v-for="entry in entries"
            :key="entry.versionId"
            class="ph-entry"
            :class="{
              active: !pickingDiff && selectedId === entry.versionId,
              'diff-base': pickingDiff && diffBaseId === entry.versionId,
              'diff-candidate': pickingDiff && diffBaseId !== entry.versionId,
            }"
            @click="onEntryClick(entry)"
          >
            <span v-if="hasLockedSample(entry)" class="entry-lock-dot" title="该版本下有已锁定的采样"></span>
            <span class="ph-entry-time">{{ fmtTime(entry.createdAt) }}</span>
            <span class="ph-entry-prompt" :title="entry.snapshot?.prompt ?? ''">{{ promptSummary(entry) }}</span>
            <span class="ph-entry-count">{{ samplesOf(entry.versionId).length }} 抽</span>
            <span v-if="latestSampledVersionId === entry.versionId" class="ph-latest">最近</span>
            <span v-if="pickingDiff && diffBaseId === entry.versionId" class="ph-diff-badge">对比基</span>
            <div class="ph-entry-actions" @click.stop>
              <button
                class="mini-btn"
                :title="pickingDiff
                  ? (diffBaseId === entry.versionId ? '取消对比选择' : '与对比基比对')
                  : '与另一个提示词版本比对'"
                @click="onCompareClick(entry)"
              >⧉ 比对</button>
              <button class="mini-btn danger" title="删除该提示词版本及其采样" @click="onDeleteEntry(entry)">✕</button>
            </div>
          </div>
        </div>

        <!-- 右：选中条目详情 + 结果墙 -->
        <div v-if="selectedEntry" class="ph-detail">
          <div class="ph-snap">
            <div class="ph-snap-head">
              <span class="mode-badge">{{ selectedEntry.snapshot?.mode }}</span>
              <span class="ph-snap-hint">提示词条目只记内容；时长/分辨率随每次采样记录，启用时分别确认恢复</span>
              <button class="mini-btn ph-apply" @click="onApplyEntry(selectedEntry)">
                应用此内容（解锁重采样）
              </button>
            </div>
            <div class="ph-snap-prompt">{{ selectedEntry.snapshot?.prompt || "（空提示词）" }}</div>
            <div v-if="snapMediaRow.length" class="ph-snap-media">
              <span
                v-for="(m, i) in snapMediaRow"
                :key="i"
                class="snap-media-thumb"
                :title="m.label"
              >
                <img v-if="m.preview" :src="m.preview" alt="" loading="lazy" />
                <span v-else class="snap-media-icon">{{ m.icon }}</span>
              </span>
            </div>
          </div>

          <!-- 结果墙：该条目全部采样（规格随样本：时长显示、画布与当前不同标徽标，启用时切画布/改时长/覆盖内容分别确认） -->
          <div class="ph-shots-title">采样结果（{{ shotSamplesOf(selectedEntry.versionId).length }}）</div>
          <div v-if="shotSamplesOf(selectedEntry.versionId).length" class="ph-shots">
            <div
              v-for="s in shotSamplesOf(selectedEntry.versionId)"
              :key="s.sampleFp"
              class="shot"
              :class="{ locked: isLocked(s) }"
            >
              <span class="shot-thumb" :title="s.exists ? '点击播放预览' : '预览文件丢失'">
                <PreviewThumb v-if="s.previewUrl && s.exists" :url="s.previewUrl" @click.stop.prevent="openPreview(s)" />
                <span v-else class="shot-thumb-empty">🎞</span>
              </span>
              <div class="shot-meta">
                <label class="shot-check" title="启用 = 出片直接用这份 latent（画布/时长/内容不匹配时分别确认恢复）">
                  <input
                    type="checkbox"
                    :checked="isLocked(s)"
                    :disabled="!s.exists"
                    @change="(e: Event) => void onToggleSample(s, (e.target as HTMLInputElement).checked, e.target as HTMLInputElement)"
                  />
                  <span v-if="isLocked(s)" class="shot-cur">已选用</span>
                </label>
                <span class="shot-seed">seed {{ s.seed }}</span>
                <span v-if="s.durationSec" class="shot-dur">{{ s.durationSec }}s</span>
                <span class="shot-time">{{ fmtTime(s.createdAt) }}</span>
              </div>
              <!-- 画布徽标常驻：每个样本都显示其采样分辨率（蓝色样式，与当前画布不一致时启用会提示切换） -->
              <span v-if="s.canvas" class="shot-canvas">{{ s.canvas }}</span>
              <span v-if="!s.exists" class="shot-stale">文件丢失</span>
              <button class="shot-del" title="删除该次采样" @click="onDeleteSample(s)">✕</button>
            </div>
          </div>
          <div v-else class="ph-no-shots">该提示词暂无采样——采样后记录会出现在这里</div>
        </div>
        <div v-else class="ph-detail ph-detail-empty">← 选择一个提示词版本查看其采样</div>
      </div>
    </template>

    <!-- 版本比对弹窗 -->
    <PromptDiffModal
      :show="diffShow"
      :a="diffA"
      :b="diffB"
      @update:show="(v: boolean) => (diffShow = v)"
      @apply="(id: number) => void onDiffApply(id)"
    />

    <!-- 样本预览播放（teleport 到 body，脱离 modal stacking context） -->
    <Teleport to="body">
      <div v-if="previewSample" class="preview-pop-mask" @click="closePreview"></div>
      <div v-if="previewSample" class="preview-pop" @click.stop>
        <canvas ref="previewCanvas" class="preview-pop-canvas"></canvas>
        <div class="preview-pop-info">
          <span>seed {{ previewSample.seed }} · {{ previewSample.durationSec || "?" }}s · {{ previewSample.canvas || "?" }}</span>
          <button class="mini-btn" @click="closePreview">关闭</button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.ph {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.ph-empty {
  font-size: 12px;
  color: var(--dc-text-faint);
  padding: 12px 0;
  text-align: center;
}
.ph-pickbar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #93c5fd;
  border: 1px solid rgba(96, 165, 250, 0.4);
  background: rgba(96, 165, 250, 0.1);
  border-radius: 6px;
  padding: 6px 8px;
}
.ph-pickbar span {
  flex: 1;
}
.ph-main {
  display: flex;
  gap: 10px;
  min-height: 320px;
}
/* 左列：提示词条目（固定窄列，右区看详情/结果墙更宽） */
.ph-list {
  width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-right: 1px solid var(--dc-border);
  padding-right: 10px;
  max-height: 54vh;
  overflow-y: auto;
}
.ph-pane-title,
.ph-shots-title {
  font-size: 11px;
  font-weight: 600;
  color: #93c5fd;
  padding: 2px 0 4px;
}
.ph-entry {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--dc-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
  cursor: pointer;
  min-width: 0;
  transition: border-color 0.12s ease;
}
.ph-entry:hover {
  border-color: var(--dc-border-strong);
}
/* 查看选中（非启用）：蓝色——绿色只表示"有 latent 被锁定/选用"，避免歧义 */
.ph-entry.active {
  border-color: rgba(96, 165, 250, 0.85);
  box-shadow: 0 0 0 1px rgba(96, 165, 250, 0.25);
}
/* 比对选择模式：对比基高亮，候选行提框提示可点 */
.ph-entry.diff-base {
  border-color: rgba(96, 165, 250, 0.95);
  background: rgba(96, 165, 250, 0.14);
}
.ph-entry.diff-candidate {
  border-color: rgba(96, 165, 250, 0.45);
}
.ph-entry.diff-candidate:hover {
  border-color: rgba(96, 165, 250, 0.9);
  background: rgba(96, 165, 250, 0.08);
}
.entry-lock-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #4ade80;
  box-shadow: 0 0 0 2px rgba(74, 222, 128, 0.2);
  flex-shrink: 0;
}
.ph-diff-badge {
  font-size: 9px;
  color: #93c5fd;
  border: 1px solid rgba(96, 165, 250, 0.5);
  border-radius: 3px;
  padding: 0 3px;
  flex-shrink: 0;
}
.ph-entry-time {
  font-size: 10px;
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.ph-entry-prompt {
  flex: 1;
  font-size: 12px;
  color: var(--dc-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.ph-entry-count {
  font-size: 10px;
  color: var(--dc-text-faint);
  flex-shrink: 0;
}
.ph-latest {
  font-size: 9px;
  color: #93c5fd;
  border: 1px solid rgba(96, 165, 250, 0.45);
  border-radius: 3px;
  padding: 0 3px;
  flex-shrink: 0;
}
.ph-entry-actions {
  display: none;
  gap: 4px;
  flex-shrink: 0;
}
.ph-entry:hover .ph-entry-actions {
  display: inline-flex;
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
  border-color: var(--dc-accent);
  color: var(--dc-accent-hover);
}
.mini-btn.danger:hover {
  border-color: #f87171;
  color: #f87171;
}
.ph-apply {
  border-color: rgba(74, 222, 128, 0.45);
  color: #86efac;
}
.ph-apply:hover {
  background: rgba(74, 222, 128, 0.14);
  border-color: #4ade80;
}
/* 右列：详情 + 结果墙 */
.ph-detail {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 54vh;
  overflow-y: auto;
  padding-right: 4px;
}
.ph-detail-empty {
  align-items: center;
  justify-content: center;
  color: var(--dc-text-faint);
  font-size: 12px;
}
.ph-snap {
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: rgba(255, 255, 255, 0.02);
}
.ph-snap-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.mode-badge {
  font-size: 10px;
  font-weight: 600;
  color: #93c5fd;
  padding: 2px 6px;
  background: rgba(96, 165, 250, 0.14);
  border-radius: 5px;
}
.ph-snap-duration {
  font-size: 11px;
  color: var(--dc-text-dim);
  font-variant-numeric: tabular-nums;
}
.ph-snap-hint {
  flex: 1;
  font-size: 10px;
  color: var(--dc-text-faint);
  text-align: right;
}
.ph-snap-prompt {
  font-size: 13px;
  line-height: 1.7;
  color: var(--dc-text);
  word-break: break-word;
  max-height: 96px;
  overflow-y: auto;
}
.ph-snap-media {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.snap-media-thumb {
  width: 56px;
  height: 34px;
  border-radius: 4px;
  overflow: hidden;
  border: 1px solid rgba(255, 255, 255, 0.14);
  background: rgba(15, 23, 42, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
}
.snap-media-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.snap-media-icon {
  font-size: 15px;
}
/* 结果墙 */
.ph-shots {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
}
.shot {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border: 1px solid var(--dc-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
}
.shot.locked {
  border-color: rgba(74, 222, 128, 0.7);
  background: rgba(20, 83, 45, 0.12);
}
/* 预览缩略图：铺满卡宽 + 固定 16:9，首帧 contain 绘制（usePreviewThumb）在 canvas 内居中 */
.shot-thumb {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  border-radius: 4px;
  overflow: hidden;
  background: rgba(15, 23, 42, 0.55);
  cursor: zoom-in;
  line-height: 0;
}
.shot-thumb :deep(canvas.sample-thumb) {
  display: block;
  width: 100%;
  height: 100%;
}
.shot-thumb-empty {
  width: 100%;
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  background: rgba(15, 23, 42, 0.55);
  border-radius: 4px;
}
.shot-meta {
  display: flex;
  align-items: center;
  gap: 4px 6px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--dc-text);
}
.shot-check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}
.shot-check input {
  width: 12px;
  height: 12px;
  margin: 0;
  cursor: pointer;
  accent-color: #4ade80;
}
.shot-cur {
  font-size: 10px;
  color: #4ade80;
}
.shot-seed {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.shot-meta-line,
.shot-time {
  color: var(--dc-text-faint);
  font-variant-numeric: tabular-nums;
}
.shot-stale {
  font-size: 10px;
  color: #fbbf24;
}
.shot-dur {
  color: var(--dc-text-dim);
  font-variant-numeric: tabular-nums;
}
/* 画布徽标（常驻显示，统一蓝色样式） */
.shot-canvas {
  position: absolute;
  top: 3px;
  left: 3px;
  font-size: 9px;
  color: #93c5fd;
  background: rgba(15, 23, 42, 0.78);
  border: 1px solid rgba(96, 165, 250, 0.5);
  border-radius: 3px;
  padding: 1px 4px;
  z-index: 2;
  font-variant-numeric: tabular-nums;
}
.shot-del {
  position: absolute;
  top: 3px;
  right: 3px;
  border: none;
  background: rgba(15, 23, 42, 0.72);
  color: var(--dc-text-faint);
  font-size: 10px;
  line-height: 1;
  padding: 2px 4px;
  border-radius: 4px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease, color 0.12s ease;
}
.shot:hover .shot-del {
  opacity: 1;
}
.shot-del:hover {
  color: #f87171;
}
.ph-no-shots {
  font-size: 11px;
  color: var(--dc-text-faint);
  padding: 8px 0;
}
/* 失效样本 */
.ph-stale {
  border: 1px dashed var(--dc-border);
  border-radius: 6px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ph-stale-title {
  font-size: 11px;
  color: #fbbf24;
}
.stale-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--dc-text-dim);
}
.stale-canvas {
  font-variant-numeric: tabular-nums;
  color: var(--dc-text-faint);
}
.stale-seed {
  flex: 1;
  font-variant-numeric: tabular-nums;
}
/* 样本预览弹窗 */
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
