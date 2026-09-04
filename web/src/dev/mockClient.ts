/**
 * dev 预览客户端接线：装上 window.app.api（fetchApi 直连 vite dev mock 中间件），
 * 并在首启时播种一个演示任务，随后自动跑一段假采样，供前端全量验证。
 *
 * 仅被 src/preview.ts import，不进 lib 构建。
 */

// ---------- 安装 window.app.api（fetchApi 同源直连 dev mock） ----------

interface MockApi {
  fetchApi: (url: string, init?: RequestInit) => Promise<Response>;
}

export function installMockApi(): void {
  const api: MockApi = {
    fetchApi: (url, init) => {
      // 直连同源 vite dev server（mock 中间件处理 /view、/minimax/studio/*、/upload）
      return fetch(url, init ?? { cache: "no-store" });
    },
  };
  (window as unknown as { app?: { api?: MockApi } }).app = {
    ...((window as unknown as { app?: object }).app || {}),
    api,
  };
}

// ---------- 演示任务种子 ----------

const DEMO_NAME = "演示任务";

function clip(
  id: string,
  mode: string,
  prompt: string,
  durationSec: number,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: `clip_${id}`,
    mode,
    prompt,
    durationSec,
    enabled: true,
    continuity: false,
    ...extra,
  };
}

/** 构造一个含多模式的演示时间线（StudioPayload 形状） */
function demoTimelineJson(): string {
  // 素材引用以 {path, kind} 进数据契约（与后端一致），/view 由 dev mock 返回色块
  const img = (name: string): { path: string; kind: string } => ({ path: name, kind: "image" });
  const vid = (name: string): { path: string; kind: string } => ({ path: name, kind: "video" });
  const aud = (name: string): { path: string; kind: string } => ({ path: name, kind: "audio" });
  const clips = [
    clip("a", "t2v", "清晨的森林，薄雾中一缕阳光穿过树梢，镜头缓慢向前推进", 5),
    clip("b", "i2v", "镜头推向溪流，水面波光粼粼，落叶顺流而下", 4, { firstFrame: img("ocean.png") }),
    clip("c", "fl2v", "从低角度仰拍瀑布，水花飞溅，光线折射出彩虹", 6, { firstFrame: img("forest.png"), lastFrame: img("city.png") }),
    clip("d", "r2v", "黄昏时分，山谷里的村庄亮起灯火，人物保持 <Picture 1> 的外观，环境参考 <Picture 2>，配 <Audio 1>", 5.5, { refImages: [img("portrait.png"), img("city.png")], refAudios: [aud("ambient.wav")] }),
    clip("e", "v2v", "将源视频改造成赛博朋克风格，霓虹灯光增强，<Video 1> 的动作保持不变", 4.5, { sourceVideo: vid("clip_broll.mp4") }),
  ];
  const total = clips.reduce((s, c) => s + Number(c.durationSec), 0);
  return JSON.stringify({
    version: 1,
    canvas: { fps: 24, width: 864, height: 480 },
    clips,
    totalDurationSec: total,
  });
}

/** 确保 dev mock 里至少有一个演示任务，返回其 task_id */
export async function ensureDemoTask(): Promise<string> {
  const list = await fetchApiJson<{ tasks?: { task_id: string; name: string }[] }>("/minimax/studio/tasks");
  const existing = (list?.tasks || []).find((t) => t.name === DEMO_NAME);
  if (existing) return existing.task_id;

  const created = await fetchApiJson<{ task_id: string }>("/minimax/studio/tasks", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ node_id: "dev-node", name: DEMO_NAME, timeline: demoTimelineJson() }),
  });
  if (!created?.task_id) throw new Error("dev mock 创建演示任务失败");
  return created.task_id;
}

async function fetchApiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const api = (window as unknown as { app?: { api?: MockApi } }).app?.api;
  if (!api) throw new Error("mock api 未安装");
  const res = await api.fetchApi(url, init);
  if (!res.ok) throw new Error(`dev mock ${init?.method || "GET"} ${url} → ${res.status}`);
  return (await res.json()) as T;
}

// ---------- 自动模拟采样 ----------

/**
 * 对当前已加载任务的启用片段，依次跑一段"假采样"并写入 mock 历史：
 * 每段走 sampling 进度（step/total）→ live 预览帧 → 完成后写一条样本 →
 * 全部完成清进度 + 刷新历史。直接调用 store actions（dev 无 executor/WS）。
 *
 * store 由 preview.ts 创建后传入（需在 pinia 安装、种子任务加载之后调用）。
 */
export interface MockSamplerCtx {
  taskId: string;
  clips: { id: string; enabled: boolean; mode: string; durationSec: number; continuity?: boolean }[];
  setProgress: (p: {
    clipId: string;
    phase: string;
    value: number;
    step?: number;
    stepsTotal?: number;
    preview?: string;
  } | null) => void;
  setLivePreview: (clipId: string, image: string | null) => void;
  refreshHistory: () => Promise<void>;
  serializeClip: (id: string) => Record<string, unknown>;
}

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** 生成一张占位 live 预览帧（dataURL），hue 由 clipId 派生，视觉有区分 */
function liveFrame(clipId: string, seedOffset: number): string {
  const canvas = document.createElement("canvas");
  canvas.width = 160;
  canvas.height = 90;
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";
  let hue = 0;
  for (const ch of clipId) hue = (hue + ch.charCodeAt(0)) % 360;
  hue = (hue + seedOffset * 7) % 360;
  const grad = ctx.createLinearGradient(0, 0, 160, 90);
  grad.addColorStop(0, `hsl(${hue}, 50%, 26%)`);
  grad.addColorStop(1, `hsl(${(hue + 60) % 360}, 60%, 14%)`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 160, 90);
  ctx.fillStyle = "rgba(255,255,255,.8)";
  ctx.font = "14px sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(`采样 ${seedOffset}`, 80, 48);
  return canvas.toDataURL("image/png");
}

export async function runMockSampling(ctx: MockSamplerCtx): Promise<void> {
  const enabled = ctx.clips.filter((c) => c.enabled);
  if (!ctx.taskId || !enabled.length) return;

  // 节奏设计（让交互看得清）：
  // - 采样期每段持续数秒（期间持续推 live 帧），否则 hover 满 2s 触发大预览的机制来不及出现
  // - 步数随片段时长增长、步进带轻微抖动，模拟真实采样不至于像计时器一样机械
  const stepBaseMs = 520;
  const jitter = () => Math.round((Math.random() - 0.5) * 160);

  for (let i = 0; i < enabled.length; i++) {
    const seg = enabled[i];
    const steps = Math.max(6, Math.min(14, Math.round(seg.durationSec * 2)));

    // sampling 阶段：进度 0→1，持续推 live 预览帧（保证 hover 期间一直有画面）
    for (let s = 0; s < steps; s++) {
      ctx.setProgress({
        clipId: seg.id,
        phase: "sampling",
        value: (s + 1) / steps,
        step: s + 1,
        stepsTotal: steps,
        preview: liveFrame(seg.id, s),
      });
      await wait(stepBaseMs + jitter());
    }

    // decoding 阶段：不再推帧（置空 live），走两小步
    ctx.setProgress({ clipId: seg.id, phase: "decoding", value: 0.4 });
    await wait(900);
    ctx.setProgress({ clipId: seg.id, phase: "decoding", value: 0.9 });
    await wait(900);

    // 写一条样本到 mock 历史
    const clip = ctx.serializeClip(seg.id);
    try {
      await fetchApiJson("/minimax/dev/sample", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          task_id: ctx.taskId,
          clip,
          canvas: "864x480@24",
          durationSec: seg.durationSec,
        }),
      });
    } catch {
      // 写历史失败不阻断后续
    }
    ctx.setProgress(null);
    // 段间留一拍，让进度条收起 / 卡片高亮看得见
    await wait(600);
  }
  ctx.setLivePreview("", null);
  await ctx.refreshHistory(); // 采样完成 → 历史区/卡片徽标刷新
}
