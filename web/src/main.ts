/**
 * ComfyUI 节点集成入口（库模式构建，产出 minimax-h3-studio.js）
 *
 * 集成方式：照官方 Vue 示例 ComfyUI_frontend_vue_basic 的模式
 * - Python 端声明自定义输入类型 MINIMAX_H3_STUDIO_UI
 * - 本文件 getCustomWidgets() 注册同名 widget 类型，节点创建时自动挂载 Vue 面板
 * - 数据桥：Queue 前把 store.serialize() 写入 timeline_data widget，后端据此反序列化；
 *   工作流保存/加载时 timeline_data 即序列化载体
 *
 * 注意：ComfyUI Frontend 1.33.9+ 不再对外暴露 Vue，
 * 因此本文件把 Vue/Pinia/Naive UI 全部打进产物，独立运行。
 */
import { createApp } from "vue";
import { createPinia } from "pinia";
// @ts-ignore ComfyUI 前端运行时模块（构建时 external，运行时解析）
import { app } from "../../scripts/app.js";
import App from "./App.vue";
import { useTimelineStore } from "@/stores/timeline";
import type { StudioPayload } from "@/types/timeline";
import "@/styles/global.css";

// 构建时注入（vite define，读 web/package.json）；运行时与后端 /version 对比做缓存自检
declare const __STUDIO_VERSION__: string;

// ComfyUI 前端运行时类型（最小声明，仅覆盖本扩展用到的部分）
interface ComfyLGraphNode {
  id: number;
  constructor?: { comfyClass?: string };
  size: [number, number];
  widgets?: {
    name: string;
    value?: unknown;
    beforeQueued?: () => void;
    serializeValue?: () => unknown;
    hidden?: boolean;
    options?: Record<string, unknown>;
    computeSize?: () => [number, number];
    element?: HTMLElement;
    _stHidden?: boolean;
    _stSynced?: boolean;
    /** 工作流恢复期间为 true：禁止 sync() 覆盖 widget.value（时序防御，见 nodeCreated） */
    _stRestoring?: boolean;
  }[];
  studioConsole?: StudioConsoleApi;
  studioSync?: () => void;
  _stStopSync?: () => void;
  _stStopState?: () => void;
  /** 事件监听已绑定标记（nodeCreated 可能多次触发，只绑一次） */
  _stEventsBound?: boolean;
  /** 事件监听清理函数（widget.onRemove 时调用） */
  _stCleanup?: () => void;
  addDOMWidget: (...args: unknown[]) => {
    onRemove?: () => void;
    value?: unknown;
  };
  setSize: (size: [number, number]) => void;
}

// ---------- CSS 注入（lib 模式 CSS 为独立文件，构建时与 JS 同名） ----------
{
  const link = document.createElement("link");
  link.rel = "stylesheet";
  // @vite-ignore 运行时解析同目录 CSS（构建产物中不存在该文件）
  link.href = new URL("./minimax-h3-studio.css", import.meta.url).href;
  document.head.appendChild(link);
}

// ---------- Vue 应用工厂 ----------

/** ComfyUI fetchApi（自动加 /api 前缀） */
function studioFetchApi(url: string): Promise<Response> {
  const api = (window as { app?: { api?: { fetchApi?: (u: string, i?: RequestInit) => Promise<Response> } } })
    .app?.api;
  if (!api?.fetchApi) return Promise.reject(new Error("ComfyUI fetchApi 不可用"));
  return api.fetchApi(url, { cache: "no-store" });
}

/** 前端构建版本自检：与后端 /minimax/studio/version 对比。
 *  不一致 → 浏览器缓存了旧版 minimax-h3-studio.js（旧版会在 nodeCreated 时把完整空 payload
 *  写进工作流 json 导致时间线丢失），提示用户强制刷新/清缓存。自检失败静默，不影响主流程。 */
async function checkFrontendVersion(): Promise<void> {
  try {
    const res = await studioFetchApi("/minimax/studio/version");
    if (!res.ok) return;
    const data = (await res.json()) as { version?: string };
    if (data.version && data.version !== __STUDIO_VERSION__) {
      console.warn(
        `[StudioConsole] 前端构建版本 ${__STUDIO_VERSION__} ≠ 后端插件版本 ${data.version}：`,
        "浏览器缓存了旧版 minimax-h3-studio.js，请强制刷新（Ctrl+Shift+R）或清除浏览器缓存，",
        "否则旧 JS 可能在保存工作流时把空时间线数据写进 json（clips 丢失）。",
      );
    }
  } catch {
    // 自检是增强信息，失败静默
  }
}

/** 模块级 pinia 单例：nodeCreated / loadedGraphNode 需要访问 store（作用域外） */
let piniaInstance: ReturnType<typeof createPinia> | null = null;

export interface StudioConsoleApi {
  mount: (container: HTMLElement, props?: Record<string, unknown>) => void;
  destroy: () => void;
  /** 获取当前时间线数据（供 ComfyUI 序列化） */
  getPayload: () => StudioPayload;
  /** 从工作流恢复时间线数据 */
  loadPayload: (payload: StudioPayload) => void;
  /** 订阅时间线数据变化（store action / state 变化时回调） */
  subscribe: (callback: () => void) => () => void;
}

export function createStudioConsole(): StudioConsoleApi {
  let appInstance: ReturnType<typeof createApp> | null = null;
  piniaInstance = createPinia();
  const pinia = piniaInstance;

  return {
    mount(el: HTMLElement, props?: Record<string, unknown>) {
      if (appInstance) return;
      appInstance = createApp(App, props);
      appInstance.use(pinia);
      appInstance.mount(el);
    },
    destroy() {
      appInstance?.unmount();
      appInstance = null;
    },
    getPayload() {
      return useTimelineStore(pinia).serialize();
    },
    loadPayload(payload: StudioPayload) {
      useTimelineStore(pinia).loadFromPayload(payload);
    },
    subscribe(callback: () => void) {
      const store = useTimelineStore(pinia);
      // $onAction 会监听 store 的【所有】action——包括我们回调里调用的
      // serialize / loadFromPayload / saveToDb 等。若不跳过，回调 → 保存 →
      // 再次触发 $onAction → after → 回调 → 无限递归爆栈。
      // 因此仅对外部操作型 action（add/remove/update/move 等）响应。
      const INTERNAL_ACTIONS = new Set([
        "serialize",
        "loadFromPayload",
        "setTaskId",
        "setNodeId",
        "loadTask",
        "createTask",
        "newTask",
        "renameTask",
        "saveToDb",
        "deleteTask",
        "exportTask",
        "importTaskFile",
        "fetchTaskList",
        "fetchHistory",
        "deleteClip",
        "setSamplingProgress",
        "openRestoreModal",
        "closeRestoreModal",
        "openHistoryPanel",
        "closeHistoryPanel",
      ]);
      const stopAction = store.$onAction(({ name, after }) => {
        if (INTERNAL_ACTIONS.has(name)) return;
        after(() => callback());
      });
      return () => {
        stopAction();
      };
    },
  };
}

// ---------- 创意工作台 widget 创建（照官方 Vue 示例） ----------

function createVueWidget(node: ComfyLGraphNode) {
  const container = document.createElement("div");
  container.style.width = "100%";
  container.style.height = "100%";
  container.style.minHeight = "560px";
  container.style.display = "flex";
  container.style.flexDirection = "column";
  container.style.overflow = "hidden";

  const widget = node.addDOMWidget("studio_console_ui", "minimax-h3-studio", container, {
    getMinHeight: () => 560,
    hideOnZoom: false,
    // required 输入必须能序列化出值，否则校验报 missing；占位值即可（数据走 timeline_data）
    serialize: true,
    getValue: () => "",
    setValue: () => {},
  });
  // 占位值：保证 required 输入在 prompt 中始终有值（后端 **kwargs 忽略）
  widget.value = "";

  // 挂载 Vue 创意工作台（数据同步由 nodeCreated 统一处理，无需传 props）
  const consoleApi = createStudioConsole();
  node.studioConsole = consoleApi;
  consoleApi.mount(container);

  widget.onRemove = () => {
    consoleApi.destroy();
    node._stCleanup?.();
    node._stStopState?.();
    node._stStopSync?.();
    node.studioConsole = undefined;
  };

  return { widget };
}

// ---------- ComfyUI 扩展注册 ----------

const NODE_CLASS = "MiniMaxH3StudioConsole";

app.registerExtension({
  name: "ComfyUI-MiniMaxH3-Studio.console",

  /** 自定义 widget 类型：Python 端 INPUT_TYPES 里 MINIMAX_H3_STUDIO_UI 输入 */
  getCustomWidgets() {
    return {
      MINIMAX_H3_STUDIO_UI: (node: ComfyLGraphNode) => createVueWidget(node),
    };
  },

  /** 节点创建后：撑开尺寸 + 隐藏 timeline_data + 挂数据同步（此时所有 Python widget 已就绪） */
  nodeCreated(node: ComfyLGraphNode) {
    if (node.constructor?.comfyClass !== NODE_CLASS) return;
    const [oldWidth, oldHeight] = node.size;
    node.setSize([Math.max(oldWidth, 620), Math.max(oldHeight, 640)]);

    const tw = node.widgets?.find((x) => x.name === "timeline_data");
    const consoleApi = node.studioConsole;
    // 诊断日志（构建后保留，用于定位加载/同步问题）
    console.log(
      "[StudioConsole] nodeCreated: timeline_data 找到 =",
      !!tw,
      "| consoleApi 存在 =",
      !!consoleApi,
      "| widget 数 =",
      node.widgets?.length ?? 0,
    );
    if (!tw || !consoleApi) return;

    // 隐藏 timeline_data 数据载体（参考项目 hideWidget 同款）
    if (!tw._stHidden) {
      tw._stHidden = true;
      tw.hidden = true;
      if (!tw.options) tw.options = {};
      tw.options.hidden = true;
      tw.computeSize = () => [0, 0];
      if (tw.element) tw.element.style.display = "none";
    }

    // 数据同步（任务库模式）：
    // - $onAction：外部编辑 action → 防抖保存（taskId 为空时惰性创建任务）
    // - $subscribe：任何 state 变化（含 taskId 清空/切换）→ 同步 widget（只存 taskId）
    // 必须在这里挂（nodeCreated 时 widget 才全部就绪），不能依赖组件 onMounted。
    if (!tw._stSynced) {
      tw._stSynced = true;
      const store = useTimelineStore(piniaInstance!);

      // 事件监听（节点级，闭包绑定本节点 store——setup 阶段 api/store 未就绪，必须在此注册）：
      // - studio_progress：executor 段级采样进度广播 → 卡片底部进度条
      // - executed：节点执行完成 → 刷新历史 + 清空进度
      const api = (window as { app?: { api?: { addEventListener?: (e: string, f: (ev: unknown) => void) => void; removeEventListener?: (e: string, f: (ev: unknown) => void) => void } } })
        .app?.api;
      if (api?.addEventListener && !node._stEventsBound) {
        node._stEventsBound = true;
        // 段级采样进度 → 卡片底部进度条（按 clipId 匹配卡片，含当前步数/总步数）
        const onStudioProgress = (ev: unknown) => {
          const d = (ev as { detail?: { clipId?: unknown; phase?: unknown; value?: unknown; step?: unknown; stepsTotal?: unknown } })?.detail;
          store.setSamplingProgress({
            clipId: String(d?.clipId ?? ""),
            phase: String(d?.phase ?? ""),
            value: Number(d?.value ?? 0),
            ...(d?.step != null ? { step: Number(d.step), stepsTotal: Number(d.stepsTotal ?? 0) } : {}),
          });
        };
        // 片段采样完成 → 刷新历史（最终预览只在历史面板查看；卡片预览仅采样过程 live）
        const onClipDone = () => {
          if (!store.taskId) return;
          void store.fetchHistory();
        };
        // 采样中 live 预览（executor studio_preview：动画 WebP base64 → 当前片段卡片背景）
        const onStudioPreview = (ev: unknown) => {
          const d = (ev as { detail?: { clipId?: unknown; image?: unknown } })?.detail;
          if (!d?.clipId) return;
          store.setLivePreview(String(d.clipId), d?.image ? String(d.image) : null);
        };
        // 节点执行完成 → 刷新历史 + 清空进度
        const onExecuted = () => {
          if (!store.taskId) return;
          store.setSamplingProgress(null); // 采样结束，隐藏进度条
          void store.fetchHistory(); // 采样完成 → 历史区/卡片徽标即时更新
        };
        api.addEventListener("studio_progress", onStudioProgress);
        api.addEventListener("studio_preview", onStudioPreview);
        api.addEventListener("studio_clip_done", onClipDone);
        api.addEventListener("executed", onExecuted);
        node._stCleanup = () => {
          api.removeEventListener?.("studio_progress", onStudioProgress);
          api.removeEventListener?.("studio_preview", onStudioPreview);
          api.removeEventListener?.("studio_clip_done", onClipDone);
          api.removeEventListener?.("executed", onExecuted);
        };
      }
      // 防抖保存：编辑高频操作合并为一次 DB 写入（短防抖，确保 Queue 时数据已落库）。
      // MVC：saveToDb 只存时间线当前数据（canvas + clips[] 完整参数草稿，覆盖式自动保存），
      // 片段当前数据独立于历史；历史版本由采样固化（Model 纯历史）。
      // 不自动创建任务——无 taskId 时不保存（时间线留在内存，需用户新建任务后编辑）。
      let saveTimer: number | undefined;
      const scheduleSave = () => {
        const tid = store.taskId;
        if (saveTimer) window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
          if (tid && store.taskId === tid) void store.saveToDb();
        }, 100);
      };
      const sync = () => {
        // widget.value = 只存任务 id（保存工作流时 json 里只有 taskId）
        // - _stRestoring 期间（工作流恢复流程中）不覆盖 widget.value：ComfyUI 加载工作流时
        //   把 json 里的值赋回 widget，时序在不同前端版本/浏览器缓存下可能晚于 nodeCreated；
        //   若在此前先 sync（旧 store 无 taskId）会把已恢复的数据抹掉——旧版构建正是因此
        //   在节点重建时把完整空 payload 写进 json，导致切回工作流时间线全部丢失。
        // - 无 taskId 且 widget 里是旧版完整 payload（无 taskId 字段）时保留不清空：
        //   作兼容载体（旧构建缓存/旧工作流），待用户新建/加载任务后升级为 taskId 格式。
        if (tw._stRestoring) return;
        const next = store.taskId ? JSON.stringify({ taskId: store.taskId }) : "";
        if (!next) {
          try {
            const existing = JSON.parse(typeof tw.value === "string" ? tw.value : "") as {
              taskId?: string;
            };
            if (existing && !existing.taskId) return; // 旧版完整 payload：保留
          } catch {
            // 空字符串/非 JSON：正常清空
          }
        }
        tw.value = next;
      };
      // Queue 时 ComfyUI 调 serializeValue → 实时构建前端权威完整数据发给后端
      // （前端正在编辑的时间线永远是对的；DB 只做持久化，不参与执行）
      tw.serializeValue = () =>
        JSON.stringify({
          taskId: store.taskId,
          payload: store.serialize(),
        });
      node._stStopSync = consoleApi.subscribe(() => {
        void scheduleSave();
      });
      // state 变化（编辑/加载/卸载）→ widget 同步；随节点销毁清理
      const stopState = store.$subscribe(() => {
        sync();
      });
      node._stStopState = stopState;

      // ★ 进入恢复期：setNodeId/setTaskId 等 state 变化不触发 sync 覆盖 widget.value
      tw._stRestoring = true;

      // 任务初始化：widget 已有 taskId（工作流恢复）→ 用之；
      // 无 → 不创建（待加载界面，需用户先新建/加载任务才能编辑保存）
      store.setNodeId(String(node.id));
      // node.id 在创建早期可能是临时值 -1（真实 id 由 ComfyUI 后续分配）：
      // 延迟校准，保证任务记录/事件绑定的 node_id 正确
      if (node.id === -1) {
        window.setTimeout(() => {
          if (node.id !== -1 && String(node.id) !== store.nodeId) {
            store.setNodeId(String(node.id));
          }
        }, 500);
      }

      // 前端构建版本自检（浏览器缓存旧 JS 时给出强刷提示）
      void checkFrontendVersion();

      // 恢复时间线：读取 widget.value，兼容两种格式——
      // 1) 任务库 {taskId}（当前）：按 id 从 DB 加载（时间线唯一数据源在 SQLite）
      // 2) 旧版完整 payload（version/canvas/clips，旧构建缓存/旧工作流）：恢复片段 UI 不丢数据
      const raw = typeof tw.value === "string" ? tw.value : "";
      let parsed: { taskId?: string; version?: number; clips?: unknown[] } | null = null;
      if (raw) {
        try {
          parsed = JSON.parse(raw) as { taskId?: string; version?: number; clips?: unknown[] };
        } catch {
          parsed = null;
        }
      }
      if (parsed?.taskId) {
        // 工作流记录了任务 id：先暂存（widget sync 需要 taskId 已就位）再加载；
        // 若该任务在本地 DB 不存在（跨机工作流 / 库被清），回退到未加载态——
        // 否则残留假 taskId 会让「＋ 片段」误以为有任务，无法新建/保存时间线。
        store.setTaskId(parsed.taskId);
        void store.loadTask(parsed.taskId).then((ok) => {
          if (!ok) store.unloadTask();
        });
      } else if (
        parsed &&
        typeof parsed.version === "number" &&
        Array.isArray(parsed.clips) &&
        parsed.clips.length > 0
      ) {
        console.warn(
          "[StudioConsole] timeline_data 为旧格式（完整 payload），已恢复片段 UI；",
          "建议新建/加载任务后重新保存工作流，升级为任务库格式（widget 只存 taskId）",
        );
        store.loadFromPayload(parsed as unknown as StudioPayload);
      }

      // ★ 恢复完成：放开 sync 并同步一次（taskId 已设置则写 {taskId}）
      tw._stRestoring = false;
      sync();
    }
  },

  /** 加载工作流时：修复采样参数错位 + 恢复时间线 UI（timeline_data 由 ComfyUI 自动还原 value） */
  loadedGraphNode(node: ComfyLGraphNode) {
    if (node.constructor?.comfyClass !== NODE_CLASS) return;

    // 旧工作流在节点定义变更后 widgets_values 会按位置错位（如 seed 收到
    // control_after_generate 的值）。按类型校验，错位值重置为默认，避免污染 prompt。
    const numericDefaults: [string, number][] = [
      ["seed", 0],
      ["steps", 25],
      ["cfg", 1],
      ["shift_video", 12],
      ["shift_audio", 3],
    ];
    for (const [name, def] of numericDefaults) {
      const w = node.widgets?.find((x) => x.name === name);
      if (w && typeof w.value !== "number") w.value = def;
    }
    const stringDefaults: [string, string][] = [
      ["sampler", "res_multistep"],
      ["scheduler", "simple"],
    ];
    for (const [name, def] of stringDefaults) {
      const w = node.widgets?.find((x) => x.name === name);
      if (w && typeof w.value !== "string") w.value = def;
    }

    // 恢复时间线 UI（任务库模式）：widget.value 存 {taskId}，按 id 从 DB 加载。
    // 时间线唯一数据源在 SQLite——绝不把时间线写进工作流 json。
    // 兼容旧格式：widget.value 若是完整 payload（旧构建缓存/旧工作流），恢复片段 UI 不丢数据。
    const tw = node.widgets?.find((x) => x.name === "timeline_data");
    const raw = typeof tw?.value === "string" ? tw.value : "";
    if (raw && node.studioConsole) {
      try {
        const parsed = JSON.parse(raw) as { taskId?: string; version?: number; clips?: unknown[] };
        if (parsed.taskId) {
          const store = useTimelineStore(piniaInstance!);
          // 任务 id 在本地库不存在时回退未加载态（同上：避免假 taskId 卡住新建/保存）
          store.setTaskId(parsed.taskId);
          void store.loadTask(parsed.taskId).then((ok) => {
            if (!ok) store.unloadTask();
          });
        } else if (
          typeof parsed.version === "number" &&
          Array.isArray(parsed.clips) &&
          parsed.clips.length > 0
        ) {
          const store = useTimelineStore(piniaInstance!);
          store.loadFromPayload(parsed as unknown as StudioPayload);
        }
      } catch {
        // 非法数据：保持空时间线（任务库模式不兼容旧完整数据格式）
      }
    }
  },
});
