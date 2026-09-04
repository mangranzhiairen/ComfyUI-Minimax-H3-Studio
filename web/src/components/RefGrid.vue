<script setup lang="ts">
/**
 * 参考素材网格（ClipDetailPanel 右侧参考素材区用）：紧凑列表 + SortableJS 拖拽排序。
 *
 * 语义（与官方 MiniMax <Picture N> 对齐）：
 * - 参考素材 = 有序实体列表，编号 = 下标 + 1，始终连续、无空位概念；
 * - 删除素材 = 直接从列表移除（后面自动前移补位）；添加 = 追加到末尾；
 * - 拖拽排序 = SortableJS 自由排序：所有卡都可拖、无"墙"，松手按 DOM 顺序整表重排；
 * - "＋添加"是列表尾的入口卡（唯一不可拖项，同时是"排到末尾"的落点）。
 *
 * 实现要点（踩坑总结）：
 * - draggable 仅排除"＋添加"；容器内其它项全可拖，SortableJS 不存在固定墙问题；
 * - v-for 的 key 必须用"卡片身份"（素材 kind:path，同文件重复引用附加出现次数），
 *   不能用槽位 index：SortableJS 直接移动 DOM 而 Vue 按自身记忆的 key 顺序 patch，
 *   key=index 会导致拖拽后"序号跟图不跟位置"（数据其实已正确落库、刷新后才正常）；
 * - 松手后不依赖 SortableJS 的 oldIndex/newIndex，直接读容器内真实 DOM 顺序重建
 *   实体数组 emit reorder，所见即所得。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import Sortable from "sortablejs";
import UploadSlot from "./UploadSlot.vue";
import type { MediaKind, ReferenceMedia } from "@/types/timeline";

const props = defineProps<{
  /** 实体素材列表（紧凑，无空位；编号 = 下标 + 1） */
  list: ReferenceMedia[];
  kind: MediaKind;
  /** 容量上限（参考图 9 / 视频 3 / 音频 3） */
  max: number;
  /** 名称前缀：「参考图」「参考视频」「参考音频」 */
  name: string;
}>();

const emit = defineEmits<{
  /** 原位替换（点击卡上传 / ⟳ 素材库替换 / 拖入替换） */
  (e: "change", index: number, media: ReferenceMedia): void;
  /** 删除指定素材（列表自动前移补位） */
  (e: "remove", index: number): void;
  /** 末尾追加新素材 */
  (e: "add", media: ReferenceMedia): void;
  /** 拖拽排序完成：按新顺序整表替换 */
  (e: "reorder", list: ReferenceMedia[]): void;
}>();

const gridEl = ref<HTMLElement | null>(null);
let sortable: Sortable | null = null;
/** 是否已达容量上限（驱动"＋添加"显隐） */
const full = computed(() => props.list.length >= props.max);

/** 卡片身份 key：素材 kind:path（同文件重复引用附加出现次数），拖拽后 Vue 靠它对齐位置 */
const cardKeys = computed(() => {
  const seen = new Map<string, number>();
  return props.list.map((m) => {
    const base = `${m.kind}:${m.path || m.name}`;
    const n = (seen.get(base) ?? 0) + 1;
    seen.set(base, n);
    return n > 1 ? `${base}#${n}` : base;
  });
});

onMounted(() => {
  if (!gridEl.value) return;
  sortable = Sortable.create(gridEl.value, {
    animation: 160,
    // 全部素材卡可拖；唯一排除"＋添加"（列表尾入口，同时是"排到末尾"的落点）
    draggable: ".grid-slot.filled",
    ghostClass: "sortable-ghost",
    chosenClass: "sortable-chosen",
    dragClass: "sortable-drag",
    // 面板超高时拖到边缘自动滚动（Sortable 自动检测滚动容器）
    scroll: true,
    scrollSensitivity: 36,
    scrollSpeed: 12,
    onEnd: rebuildOrder,
  });
});

onBeforeUnmount(() => {
  sortable?.destroy();
  sortable = null;
});

/** 拖拽结束：读容器内素材卡的真实 DOM 顺序，整表重建 emit（所见即所得） */
function rebuildOrder(): void {
  const g = gridEl.value;
  if (!g) return;
  const order: ReferenceMedia[] = [];
  g.querySelectorAll<HTMLElement>(".grid-slot.filled").forEach((el) => {
    const m = props.list[Number(el.dataset.slot)];
    if (m) order.push(m);
  });
  // 顺序未变则不触发更新（拖回原位不白写）
  if (!order.length || (order.length === props.list.length && order.every((m, i) => m === props.list[i]))) {
    return;
  }
  emit("reorder", order);
}
</script>

<template>
  <div ref="gridEl" class="slot-grid">
    <!-- 素材卡：编号 = 位置（i+1）；可拖拽排序；点击 = 本地上传原位替换；⟳ = 素材库替换 -->
    <div
      v-for="(m, i) in list"
      :key="cardKeys[i]"
      class="grid-slot filled"
      :data-slot="i"
    >
      <UploadSlot
        :media="m"
        :label="`${name} ${i + 1}`"
        :index-label="i + 1"
        :kind="kind"
        @change="(mm: ReferenceMedia) => emit('change', i, mm)"
        @remove="emit('remove', i)"
      />
    </div>

    <!-- 末尾追加入口：不可拖起（可作为"排到末尾"的落点） -->
    <div v-if="!full" class="grid-slot append" :data-slot="list.length">
      <UploadSlot
        :label="`＋ 添加${name}`"
        :kind="kind"
        @change="(mm: ReferenceMedia) => emit('add', mm)"
      />
    </div>
  </div>
</template>

<style scoped>
.slot-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
  gap: 8px;
}

.grid-slot {
  min-width: 0;
}

/* 素材卡可拖拽（SortableJS 只允许 .filled 拖起）；禁文本选择避免拖拽中选中 */
.grid-slot.filled {
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
}
.grid-slot.filled :deep(.slot) {
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
  -webkit-user-drag: none;
}

/* ---- SortableJS 拖拽态（类由 sortable 运行时加在本组件渲染的 wrapper 上） ---- */
/* 原位卡片：拖拽中被移开后留下的原卡降透明，其余卡平滑让位（animation） */
.sortable-ghost {
  opacity: 0.35;
}
.sortable-chosen {
  opacity: 0.9;
}
</style>
