<script setup lang="ts">
import { computed, ref } from "vue";
import { palette } from "@/styles/theme";

// 画布分辨率/帧率选择器：分辨率计算严格对齐官方 ResolutionSelector
// （comfy_extras/nodes_resolution.py）——以「宽高比 × 目标百万像素」驱动宽高：
//   totalPixels = megapixels × 1024²
//   scale       = sqrt(totalPixels / (aspect_w × aspect_h))
//   width/height = round(ratio × scale / 32) × 32   ← 官方是 round，不是 ceil
// 唯一差异：对齐倍数固定 32（官方默认 8）——H3 patchify 要求宽高为 32 的倍数。
// 注：曾把这里改成 ceil（"向上取整保证像素不缩水"），结果每个轴各自向上取整，
// 既偏离官方表（0.4MP → 864×512 而非 864×480），也把 16:9 拉成 1.6875:1；
// 后端 sampling._align32 只对非 32 倍数向下取整，本函数输出必为 32 倍数，无需额外保险。

const props = defineProps<{ width: number; height: number; fps: number }>();
const emit = defineEmits<{
  (e: "apply", v: { width: number; height: number; fps: number }): void;
}>();

/** 按钮标签（不点击时的显示：864x480@24） */
const label = computed(() => `${props.width}x${props.height}@${props.fps}`);

/** 官方 ResolutionSelector AspectRatio 枚举同款 8 种宽高比 */
const RATIOS = [
  { key: "1:1", label: "1:1 方屏", w: 1, h: 1 },
  { key: "2:3", label: "2:3 竖幅照片", w: 2, h: 3 },
  { key: "3:2", label: "3:2 横幅照片", w: 3, h: 2 },
  { key: "3:4", label: "3:4 竖版标准", w: 3, h: 4 },
  { key: "4:3", label: "4:3 横版标准", w: 4, h: 3 },
  { key: "9:16", label: "9:16 竖屏宽幅", w: 9, h: 16 },
  { key: "16:9", label: "16:9 横屏宽幅", w: 16, h: 9 },
  { key: "21:9", label: "21:9 超宽屏", w: 21, h: 9 },
];
const ratioOptions = RATIOS.map((r) => ({ label: r.label, value: r.key }));

/** 对齐倍数：后端 H3 patchify 要求宽高为 32 的倍数（官方 ResolutionSelector 默认 8） */
const ALIGN = 32;

/** 官方 ResolutionSelector.execute 同款算法（对齐倍数固定 32）：
 *  总像素 = megapixels × 1024² → 按宽高比展开 → 各轴四舍五入到 32 的倍数。
 *  与官方 16:9 表逐行一致（0.2→608×352 … 0.98→1344×768 … 2.0→1920×1088）。 */
function calcSize(key: string, megapixels: number): { width: number; height: number } {
  const r = RATIOS.find((x) => x.key === key) ?? RATIOS[6];
  const totalPixels = megapixels * 1024 * 1024;
  const scale = Math.sqrt(totalPixels / (r.w * r.h));
  return {
    width: Math.round((r.w * scale) / ALIGN) * ALIGN,
    height: Math.round((r.h * scale) / ALIGN) * ALIGN,
  };
}

/** 当前宽高比最接近的官方比例 key（弹框打开时回填，保证选中项与画布视觉一致） */
function nearestRatioKey(w: number, h: number): string {
  let best = RATIOS[6].key;
  let bestErr = Infinity;
  for (const r of RATIOS) {
    const err = Math.abs(w / h - r.w / r.h);
    if (err < bestErr) {
      bestErr = err;
      best = r.key;
    }
  }
  return best;
}

/** 弹框内编辑态（打开时从当前画布初始化，应用前不动 store） */
const ratioKey = ref("16:9");
const mp = ref(1);
const draftW = ref(props.width);
const draftH = ref(props.height);
const draftFps = ref(props.fps);
const show = ref(false);

function onOpen(visible: boolean) {
  if (!visible) return;
  ratioKey.value = nearestRatioKey(props.width, props.height);
  // 反推当前像素总量作为百万像素初值（2 位小数：官方 0.98MP→1344×768 这类非 0.1 步进值
  // 也能原样回填，避免开弹框再应用就漂到 1.0MP→1376×768）
  const px = (props.width * props.height) / (1024 * 1024);
  mp.value = Math.min(2, Math.max(0.1, Math.round(px * 100) / 100));
  draftFps.value = props.fps;
  recalcFromRatio(); // 弹框内宽高始终 = 比例×像素的自洽计算值（32 对齐）
}

/** 自动计算结果（实时预览文案用） */
const autoSize = computed(() => {
  const s = calcSize(ratioKey.value, mp.value);
  return `${s.width}×${s.height}`;
});

/** 比例/像素变化 → 按官方算法重算并回填宽高草稿 */
function recalcFromRatio() {
  const s = calcSize(ratioKey.value, mp.value);
  draftW.value = s.width;
  draftH.value = s.height;
}

function onRatioChange(v: string | null) {
  if (!v) return;
  ratioKey.value = v;
  recalcFromRatio();
}

function onMpChange(v: number | null) {
  if (v == null) return;
  mp.value = Math.min(2, Math.max(0.1, v));
  recalcFromRatio();
}

/** 应用：合法值校验后 emit（Toolbar 走 updateCanvas + 清勾选提示） */
function onApply() {
  const width = Math.round(draftW.value || 0);
  const height = Math.round(draftH.value || 0);
  const fps = Math.round(draftFps.value || 0);
  if (width < 64 || height < 64 || fps < 1) return;
  show.value = false;
  emit("apply", { width, height, fps });
}
</script>

<template>
  <n-popover v-model:show="show" trigger="click" placement="bottom" @update:show="onOpen">
    <template #trigger>
      <button class="res-btn" :title="'画布参数（分辨率/帧率），点击展开选择'">
        {{ label }}
      </button>
    </template>

    <div class="res-pop">
      <!-- 宽高比：官方 ResolutionSelector 同款 8 种预设 -->
      <div class="res-pop-row">
        <span class="res-pop-label">宽高比</span>
        <n-select
          :value="ratioKey"
          :options="ratioOptions"
          size="small"
          style="width: 168px"
          @update:value="onRatioChange"
        />
      </div>

      <!-- 目标百万像素：滑块 + 数字输入（按 H3 实际支持限制 0.1~2，步进 0.1） -->
      <div class="res-pop-row">
        <span class="res-pop-label">像素</span>
        <n-slider
          :value="mp"
          :min="0.1"
          :max="2"
          :step="0.1"
          class="res-mp"
          @update:value="onMpChange"
        />
        <n-input-number
          :value="mp"
          :min="0.1"
          :max="2"
          :step="0.1"
          size="small"
          style="width: 84px"
          @update:value="onMpChange"
        />
        <span class="res-unit">MP</span>
      </div>

      <div class="res-hint">自动 {{ autoSize }}（官方算法，四舍五入到 32 的倍数），下方可微调</div>

      <!-- 宽高（比例/像素自动回填，可手动微调精确值） -->
      <div class="res-pop-row">
        <span class="res-pop-label">宽</span>
        <n-input-number v-model:value="draftW" :min="64" :max="8192" size="small" style="width: 96px" />
        <span class="res-pop-label">高</span>
        <n-input-number v-model:value="draftH" :min="64" :max="8192" size="small" style="width: 96px" />
      </div>

      <div class="res-pop-row">
        <span class="res-pop-label">帧率</span>
        <n-input-number v-model:value="draftFps" :min="1" :max="60" size="small" style="width: 96px" />
        <button class="res-apply" @click="onApply">应用</button>
      </div>
    </div>
  </n-popover>
</template>

<style scoped>
.res-btn {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--dc-border);
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--dc-text);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  white-space: nowrap;
}
.res-btn:hover {
  border-color: v-bind("palette.accent");
  background: rgba(255, 255, 255, 0.08);
}

.res-pop {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 2px;
  width: 352px;
}
.res-pop-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.res-pop-label {
  flex-shrink: 0;
  width: 40px;
  font-size: 12px;
  color: var(--dc-text-dim);
  text-align: right;
}
/* 像素滑块：占满剩余宽度，拖动即重算宽高 */
.res-mp {
  flex: 1;
  min-width: 0;
  margin: 0 2px;
}
.res-unit {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--dc-text-faint);
}
/* 自动计算结果提示行 */
.res-hint {
  font-size: 10px;
  color: #93c5fd;
  font-variant-numeric: tabular-nums;
  padding-left: 48px;
  line-height: 1;
}
.res-apply {
  margin-left: auto;
  height: 24px;
  padding: 0 14px;
  border: none;
  border-radius: 5px;
  background: v-bind("palette.accent");
  color: #fff;
  font-size: 12px;
  cursor: pointer;
}
.res-apply:hover {
  opacity: 0.85;
}
</style>
