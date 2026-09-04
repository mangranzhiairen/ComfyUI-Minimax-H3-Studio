import { ref, type Ref } from "vue";

export interface DragState {
  dragging: Ref<boolean>;
  /** 距拖拽起点的累计位移（px） */
  dx: Ref<number>;
  dy: Ref<number>;
}

export interface DragOptions {
  onStart?: (e: PointerEvent) => void;
  onMove?: (e: PointerEvent, dx: number, dy: number) => void;
  onEnd?: (e: PointerEvent, dx: number, dy: number) => void;
  /** 指针按下后移动多少像素才进入拖拽态（防误触） */
  threshold?: number;
}

/**
 * 通用指针拖拽封装（原生 Pointer Events，零依赖）。
 * 返回 bind 函数，绑定到元素 pointerdown 事件即可。
 *
 * 用法：
 *   const { bind } = useDrag({ onMove: (e, dx) => { ... } });
 *   <div v-bind="bind">...</div>
 */
export function useDrag(options: DragOptions): { bind: Record<string, unknown> } {
  const dragging = ref(false);
  const dx = ref(0);
  const dy = ref(0);

  let startX = 0;
  let startY = 0;
  let pointerId: number | null = null;

  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0) return; // 仅左键
    pointerId = e.pointerId;
    startX = e.clientX;
    startY = e.clientY;
    dx.value = 0;
    dy.value = 0;
    options.onStart?.(e);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: PointerEvent) {
    if (pointerId !== e.pointerId) return;
    const newDx = e.clientX - startX;
    const newDy = e.clientY - startY;
    const threshold = options.threshold ?? 3;
    if (!dragging.value && Math.hypot(newDx, newDy) > threshold) {
      dragging.value = true;
    }
    dx.value = newDx;
    dy.value = newDy;
    options.onMove?.(e, newDx, newDy);
  }

  function onPointerUp(e: PointerEvent) {
    if (pointerId !== e.pointerId) return;
    pointerId = null;
    const ended = dragging.value;
    dragging.value = false;
    options.onEnd?.(e, dx.value, dy.value);
    if (ended) e.preventDefault();
  }

  return {
    bind: {
      onPointerdown: onPointerDown,
      onPointermove: onPointerMove,
      onPointerup: onPointerUp,
      onPointercancel: onPointerUp,
    },
  };
}
