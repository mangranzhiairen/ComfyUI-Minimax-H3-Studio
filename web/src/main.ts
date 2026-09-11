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
  /** 节点属性（随工作流 json 序列化）：studio_task_id 作 taskId 的冗余载体 */
  properties?: Record<string, unknown>;
  /** 从载体恢复时间线绑定（nodeCreated 注册，loadedGraphNode 复用） */
  _stRestore?: () => boolean;
  /** 取消未完成的恢复重试（widget.onRemove 时调用） */
  _stStopRestore?: () => void;
  /** 立刻落库（切 tab 销毁节点前调用，保证草稿已写进 DB） */
  _stFlush?: () => void;
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

/** 模块级 pinia 单例已移除：每个节点持有自己的 pinia/store。
 *  （模块级单例会在「两个工作流 tab 各有一个工作台节点」时被后建的节点覆盖，
 *   导致 loadedGraphNode 的恢复落到另一个节点的 store 上。） */

export interface StudioConsoleApi {
  mount: (container: HTMLElement, props?: Record<string, unknown>) => void;
  destroy: () => void;
  /** 获取当前时间线数据（供 ComfyUI 序列化） */
  getPayload: () => StudioPayload;
  /** 从工作流恢复时间线数据 */
  loadPayload: (payload: StudioPayload) => void;
  /** 订阅时间线数据变化（store action / state 变化时回调） */
  subscribe: (callback: () => void) => () => void;
  /** 本节点专属的时间线 store（与 mount 的 Vue 应用同一个 pinia） */
  getStore: () => ReturnType<typeof useTimelineStore>;
}

export function createStudioConsole(): StudioConsoleApi {
  let appInstance: ReturnType<typeof createApp> | null = null;
  // 每个节点一个 pinia：组件与服务端恢复逻辑共用同一个 store，互不串台
  const pinia = createPinia();

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
    getStore() {
      return useTimelineStore(pinia);
    },
    subscribe(callback: () => void) {
      const store = useTimelineStore(pinia);
      // $onAction 会监听 store 的【所有】action——包括我们回调里调用的
      // serialize / loadFromPayload / saveToDb 等。若不跳过，回调 → 保存 →
      // 再次触发 $onAction → after → 回调 → 无限递归爆栈。
      // 因此仅对外部操作型 action（add/remove/update/move 等）响应。
      // 注意：纯 UI 动作（setZoom/select）不算数据变更，必须一并跳过——
      // 否则面板挂载时的缩放适配会触发保存，在恢复未完成时把空时间线写回 DB。
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
        "setZoom",
        "select",
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
    // 切工作流 tab 会销毁节点：先把待写的草稿落库（未就绪时 store 内部会拒绝），
    // 再取消未完成的恢复重试，最后清理订阅/监听。
    node._stStopRestore?.();
    node._stFlush?.();
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
      // 本节点专属 store（与 mount 的 Vue 应用同一 pinia，不随其它节点/其它 tab 串台）
      const store = consoleApi.getStore();

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
      // ★ 真正的"防覆盖"守卫在 store.saveToDb（loadedTaskId 未就绪时拒绝写库）。
      let saveTimer: number | undefined;
      const flushSave = () => {
        // 节点即将销毁（切 tab）→ 立刻把草稿落库，不等防抖
        if (saveTimer) {
          window.clearTimeout(saveTimer);
          saveTimer = undefined;
        }
        void store.saveToDb();
      };
      node._stFlush = flushSave;
      const scheduleSave = () => {
        const tid = store.taskId;
        if (saveTimer) window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
          saveTimer = undefined;
          if (tid && store.taskId === tid) void store.saveToDb();
        }, 100);
      };
      const sync = () => {
        // widget.value = 只存任务 id（保存工作流时 json 里只有 taskId）
        // ★ 不变式 I1（载体侧）：只有"用户显式卸载/删除任务"才允许把载体写空。
        //   瞬时 taskId=null（恢复窗口内 / 加载失败）一律不动载体——旧版正是因此
        //   在恢复失败时把 taskId 写成空串，工作流从此永久失绑（切多少次都是空）。
        if (tw._stRestoring) return;
        const tid = store.taskId;
        if (!tid) {
          if (!store.bindingReleased) return;
          if (tw.value !== "") tw.value = "";
          if (node.properties) delete node.properties.studio_task_id;
          return;
        }
        // 载体 A：widget（工作流 json 里唯一的时间线指针）
        const next = JSON.stringify({ taskId: tid });
        if (tw.value !== next) tw.value = next;
        // 载体 B：node.properties 冗余一份（只在变化时写，不会因编辑把工作流标脏；
        //  widget 被清 / configure 时序错位时兜底恢复）
        if (node.properties && node.properties.studio_task_id !== tid) {
          node.properties.studio_task_id = tid;
        }
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

      // ---------- 恢复时间线（双载体 + 宽容失败） ----------
      // 载体 A：timeline_data widget = {"taskId":"N"}（工作流 json 的时间线指针）
      // 载体 B：node.properties.studio_task_id（同值冗余，widget 缺失/时序错位时兜底）
      // 兼容：旧版完整 payload（version/canvas/clips）→ 直接恢复片段 UI
      const readBinding = (): { taskId: string; legacy: StudioPayload | null } => {
        const raw = typeof tw.value === "string" ? tw.value : "";
        if (raw) {
          try {
            const p = JSON.parse(raw) as { taskId?: unknown; version?: number; clips?: unknown[] };
            if (p && typeof p === "object") {
              if (p.taskId) return { taskId: String(p.taskId), legacy: null };
              if (typeof p.version === "number" && Array.isArray(p.clips) && p.clips.length > 0) {
                return { taskId: "", legacy: p as unknown as StudioPayload };
              }
            }
          } catch {
            // 非法 JSON：继续走 properties 兜底
          }
        }
        const prop = node.properties?.studio_task_id;
        return { taskId: typeof prop === "string" && prop ? prop : "", legacy: null };
      };

      let restoreTimer: number | undefined;
      let restoringFor: string | null = null;
      let restoreAttempt = 0;
      /** 拉取任务时间线；瞬时失败只重试，绝不清空（清空 = 丢正在编辑的草稿） */
      const scheduleRestore = (taskId: string) => {
        restoringFor = taskId;
        restoreAttempt = 0;
        const run = async () => {
          const ok = await store.loadTask(taskId);
          if (ok) {
            restoringFor = null;
            return;
          }
          if (store.taskMissing) {
            // 任务在本地任务库确实不存在（跨机工作流 / 库被清）→ 回未加载态。
            // 这是**显式**卸载（bindingReleased），允许清空载体。
            restoringFor = null;
            console.warn(`[StudioConsole] 任务 ${taskId} 不在本地任务库，回到未加载态`);
            store.unloadTask();
            return;
          }
          if (restoreAttempt < 3) {
            restoreAttempt++;
            const delay = restoreAttempt === 1 ? 400 : 1200;
            console.warn(
              `[StudioConsole] 任务 ${taskId} 加载失败（网络/服务异常），${delay}ms 后重试 ${restoreAttempt}/3`,
            );
            restoreTimer = window.setTimeout(() => void run(), delay);
          } else {
            restoringFor = null;
            console.error(
              `[StudioConsole] 任务 ${taskId} 加载失败（已重试 3 次）：已保留本地内容，可稍后手动重选任务`,
            );
          }
        };
        void run();
      };
      node._stStopRestore = () => {
        if (restoreTimer) window.clearTimeout(restoreTimer);
        restoreTimer = undefined;
      };

      /** 从载体恢复绑定；幂等（已就绪 / 正在恢复则跳过）。返回是否读到可用绑定 */
      const tryRestore = (): boolean => {
        const b = readBinding();
        if (b.taskId) {
          if (store.loadedTaskId === b.taskId || restoringFor === b.taskId) return true;
          store.setTaskId(b.taskId);
          scheduleRestore(b.taskId);
          return true;
        }
        if (b.legacy) {
          console.warn(
            "[StudioConsole] timeline_data 为旧格式（完整 payload），已恢复片段 UI；",
            "建议新建/加载任务后重新保存工作流，升级为任务库格式（widget 只存 taskId）",
          );
          store.loadFromPayload(b.legacy);
          return true;
        }
        return false;
      };
      node._stRestore = tryRestore;

      // 任务初始化：widget/properties 已有 taskId（工作流恢复）→ 用之；
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

      // ★ 进入恢复期：setNodeId/setTaskId 等 state 变化不触发 sync 覆盖 widget.value
      tw._stRestoring = true;
      if (!tryRestore()) {
        // 时序兜底：部分前端版本下 configure() 回填 widget 值晚于 nodeCreated
        // （nodeCreated 由 invokeExtensionsAsync 异步触发）。延迟再读一次载体；
        // 权威入口仍是 loadedGraphNode（configure 之后统一触发）。
        restoreTimer = window.setTimeout(() => void tryRestore(), 400);
      }
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
    // 统一走 nodeCreated 注册的 tryRestore（双载体 + 幂等 + 失败重试 + 绝不清空）：
    // 这里在 configure() 回填 widget 值之后触发，是时序上最权威的恢复入口。
    node._stRestore?.();
  },
});
