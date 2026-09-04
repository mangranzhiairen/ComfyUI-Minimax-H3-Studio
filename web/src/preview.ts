/**
 * 浏览器独立预览入口（不依赖 ComfyUI）
 *
 * 运行方式：vite dev / preview 后打开 http://localhost:5178
 *
 * dev mock（web/src/dev/）：由 vite 中间件在 /view、/minimax/studio/*、/upload
 * 提供本地后端实现，前端经 window.app.api 走真实 HTTP——因此 dev 里任务库 /
 * 时间线 / 历史 / 素材 / 假采样等前端功能都能像在 ComfyUI 里一样全量验证。
 * 这些模块只被本文件 import，不进 lib 构建（生产无影响）。
 */
import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import { useTimelineStore } from "@/stores/timeline";
import "@/styles/global.css";
import { installMockApi, ensureDemoTask, runMockSampling } from "@/dev/mockClient";

/** 初始化顺序：装 mock api（fetchApi 可直连）→ 播种演示任务 → 挂载 App */
async function bootstrap() {
  installMockApi();

  // 播种演示任务（幂等：有则复用）
  let demoTaskId = "";
  try {
    demoTaskId = await ensureDemoTask();
  } catch (err) {
    // mock 后端不可用：退回纯内存空时间线（理论不发生）
    console.warn("[StudioPreview] dev mock 不可用，跳过演示任务：", err);
  }

  const app = createApp(App);
  const pinia = createPinia();
  app.use(pinia);
  app.mount("#app");

  const store = useTimelineStore();

  // 加载演示任务（从 mock DB 恢复 timeline，走与 ComfyUI 相同的 loadTask 链路）
  if (demoTaskId) {
    const ok = await store.loadTask(demoTaskId);
    if (!ok) store.unloadTask();
  }

  // 若无任务（异常路径）至少给一段可见时间线便于调视觉
  if (!store.taskId && !store.clips.length) {
    store.addClip({ mode: "t2v", prompt: "示例镜头（空演示）", durationSec: 4 });
  }

  // 自动跑一遍假采样，让历史区/卡片徽标出现可验证数据（无需额外按钮）
  if (store.taskId && store.clips.length) {
    void runMockSampling({
      taskId: store.taskId,
      clips: store.clips.map((c) => ({
        id: c.id,
        enabled: c.enabled,
        mode: c.mode,
        durationSec: c.durationSec,
        continuity: c.continuity,
      })),
      setProgress: (p) => store.setSamplingProgress(p),
      setLivePreview: (clipId, image) => store.setLivePreview(clipId, image),
      refreshHistory: () => store.fetchHistory(),
      serializeClip: (id) => {
        const c = store.clips.find((x) => x.id === id);
        // 返回结构兼容 mock 后端可读的 clip 载荷（含 path/kind 素材引用）
        return c as unknown as Record<string, unknown>;
      },
    });
  }
}

void bootstrap();
