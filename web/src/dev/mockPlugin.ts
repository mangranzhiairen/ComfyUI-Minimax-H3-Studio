/**
 * vite dev 中间件插件：为「浏览器独立预览」提供一个本地 mock 后端。
 *
 * 仅在 vite dev/preview（非 lib）模式挂载（见 web/vite.config.ts 的 dev 分支）。
 * 目的：让 dev 预览里任务库 / 时间线 / 历史 / 素材等全部走真实的 HTTP 请求，
 * 复刻 ComfyUI 后端（studio/http_routes.py + segment_cache.py）对前端暴露的语义，
 * 从而无需启动 ComfyUI 即可全量验证前端交互。
 *
 * - 图片 /view 与 /upload 在此真正可返回（解决了纯前端模块无法拦截 <img> 的问题）。
 * - 状态保存在 dev server 进程内存（模块单例）：页面刷新不丢，重启 dev server 则重置。
 * - 不参与 lib 构建（lib 分支不含此插件）。
 */
import type { Plugin } from "vite";
import type { IncomingMessage, ServerResponse } from "node:http";

// ---------- 内存状态（模块单例，跨页面刷新保留，重启 dev server 重置） ----------

interface MockTask {
  task_id: string;
  node_id: string;
  name: string;
  timeline: string; // JSON 字符串，与后端 tasks.timeline 一致
  status: string;
  created_at: number;
  updated_at: number;
}

interface MockVersion {
  versionId: number;
  contentFp: string;
  canvas?: string;
  snapshot: Record<string, unknown>;
  createdAt: number;
}

interface MockSample {
  versionId: number;
  clipId: string;
  contentFp: string;
  canvas?: string;
  sampleFp: string;
  seed: number;
  durationSec: number;
  continuity: boolean;
  frames: number;
  sampleLen: number;
  createdAt: number;
}

/** 每个 task 的 clip 历史（clipId → {versions, samples}） */
type ClipHist = Record<string, { versions: MockVersion[]; samples: MockSample[] }>;

type Req = IncomingMessage;
type Res = ServerResponse;

const state = {
  tasks: new Map<string, MockTask>(),
  /** task_id -> clip history */
  hist: new Map<string, ClipHist>(),
  /** 上传到 "input 目录" 的文件：filename -> Buffer + mime（/upload 写入，/view 读取） */
  input: new Map<string, { data: Buffer; mime: string; kind: string }>(),
  seq: 1,
  verSeq: 1000,
};

function nextTaskId(): string {
  state.seq += 1;
  return String(state.seq);
}

// ---------- 工具 ----------

function json(res: Res, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(body));
}

function bodyText(req: Req, limit = 5 * 1024 * 1024): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on("data", (c: Buffer) => {
      size += c.length;
      if (size > limit) {
        reject(new Error("payload too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

/** 极简 multipart 解析：仅取第一个文件字段的原始字节与文件名（dev mock 够用）。 */
function parseMultipart(req: Req): Promise<{ filename?: string; data: Buffer; mime?: string }> {
  return new Promise((resolve, reject) => {
    const ct = (req.headers["content-type"] as string) || "";
    const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(ct);
    const boundary = m?.[1] ?? m?.[2];
    if (!boundary) {
      reject(new Error("缺少 multipart boundary"));
      return;
    }
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const buf = Buffer.concat(chunks);
      const del = Buffer.from(`--${boundary}`);
      const parts = splitBuffer(buf, del);
      for (const part of parts) {
        const headEnd = part.indexOf(Buffer.from("\r\n\r\n"));
        if (headEnd === -1) continue;
        const header = part.subarray(0, headEnd).toString("utf-8");
        const content = part.subarray(headEnd + 4);
        const isFile = header.includes("filename=");
        if (isFile) {
          const fn = /filename="([^"]*)"/.exec(header)?.[1] ?? "";
          const mime = /Content-Type:\s*([^\r\n]+)/i.exec(header)?.[1]?.trim();
          // 去掉尾部可能残留的 \r\n
          let data = content;
          if (data.length >= 2 && data[data.length - 1] === 0x0a) {
            data = data.subarray(0, data.length - (data[data.length - 2] === 0x0d ? 2 : 1));
          }
          resolve({ filename: fn || undefined, data, mime });
          return;
        }
      }
      reject(new Error("multipart 中没有文件字段"));
    });
    req.on("error", reject);
  });
}

function splitBuffer(buf: Buffer, del: Buffer): Buffer[] {
  const out: Buffer[] = [];
  let start = 0;
  let idx = buf.indexOf(del);
  while (idx !== -1) {
    if (idx > start) out.push(buf.subarray(start, idx));
    start = idx + del.length;
    if (buf[start] === 0x0d) start += 2; // 跳过边界后的 \r\n
    idx = buf.indexOf(del, start);
  }
  return out;
}

/** 简易确定性 hash（16 位 hex，模拟内容指纹 sampleFp 用） */
function hash16(s: string): string {
  let h1 = 0xdeadbeef ^ 0x1f2e3d4c;
  let h2 = 0x41c6ce57 ^ 0x1f2e3d4c;
  for (let i = 0; i < s.length; i++) {
    const ch = s.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h2 >>> 0).toString(16).padStart(8, "0") + (h1 >>> 0).toString(16).padStart(8, "0");
}

function clipPayloadFp(clip: Record<string, unknown>): string {
  return hash16(
    JSON.stringify({
      mode: clip.mode,
      prompt: clip.prompt,
      firstFrame: clip.firstFrame,
      lastFrame: clip.lastFrame,
      refImages: clip.refImages,
      refVideos: clip.refVideos,
      refAudios: clip.refAudios,
      sourceVideo: clip.sourceVideo,
    }),
  );
}

/** 占位图（SVG）：dev 里为没有真实文件的图片/样本预览生成可见色块。 */
function svgImage(hue: number, label: string): string {
  const c = `hsl(${hue},45%,32%)`;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="100%" height="100%" fill="${c}"/><text x="50%" y="50%" fill="rgba(255,255,255,.9)" font-size="20" text-anchor="middle" dominant-baseline="middle" font-family="sans-serif">${label}</text></svg>`;
}

/** 预设的 mock input 目录素材（文件名会在 /view 处生成色块） */
const PRESET_MEDIA: { kind: string; name: string; relPath: string; hue: number; label: string }[] = [
  { kind: "image", name: "forest.png", relPath: "forest.png", hue: 140, label: "森林" },
  { kind: "image", name: "city.png", relPath: "city.png", hue: 210, label: "城市" },
  { kind: "image", name: "portrait.png", relPath: "portrait.png", hue: 30, label: "人像" },
  { kind: "image", name: "ocean.png", relPath: "ocean.png", hue: 200, label: "海洋" },
  { kind: "video", name: "clip_broll.mp4", relPath: "clip_broll.mp4", hue: 90, label: "B-roll" },
  { kind: "audio", name: "ambient.wav", relPath: "ambient.wav", hue: 300, label: "环境" },
];
function presetFor(filename: string) {
  return PRESET_MEDIA.find((m) => m.name === filename);
}

// ---------- 路由处理 ----------

type Handler = (params: string[], req: Req, res: Res, url: URL) => Promise<void>;

const routeHandlers: { method: string; re: RegExp; handle: Handler }[] = [];

function route(method: string, pattern: string, handle: Handler): void {
  const re = new RegExp(
    "^" + pattern.replace(/\/+/g, "/").replace(/:\w+/g, "([^/]+)") + "$",
  );
  routeHandlers.push({ method, re, handle });
}

// ---- 任务库 ----

route("GET", "/minimax/studio/version", async (_p, _req, res) => {
  json(res, 200, { version: "0.1.0" });
});

route("POST", "/minimax/studio/tasks", async (_p, req, res) => {
  const b = JSON.parse((await bodyText(req)) || "{}");
  const id = nextTaskId();
  const name = String(b?.name || "").trim() || "新任务";
  const task: MockTask = {
    task_id: id,
    node_id: String(b?.node_id || ""),
    name,
    timeline:
      typeof b?.timeline === "string"
        ? b.timeline
        : JSON.stringify(
            b?.timeline ?? {
              version: 1,
              canvas: { fps: 24, width: 864, height: 480 },
              clips: [],
              totalDurationSec: 0,
            },
          ),
    status: "created",
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  };
  state.tasks.set(id, task);
  state.hist.set(id, {});
  json(res, 200, { task_id: id });
});

route("GET", "/minimax/studio/tasks", async (_p, _req, res) => {
  const tasks = [...state.tasks.values()].map((t) => {
    const timeline = safeParse<{ clips?: unknown[] }>(t.timeline);
    const h = state.hist.get(t.task_id) || {};
    let sampled = 0;
    for (const k of Object.keys(h)) sampled += (h[k].samples || []).length;
    return {
      task_id: t.task_id,
      node_id: t.node_id,
      name: t.name,
      status: t.status,
      created_at: t.created_at,
      updated_at: t.updated_at,
      segment_count: Array.isArray(timeline.clips) ? timeline.clips.length : 0,
      sampled_count: sampled,
    };
  });
  tasks.sort((a, b) => b.updated_at - a.updated_at);
  json(res, 200, { tasks });
});

route("GET", "/minimax/studio/tasks/:task_id", async ([taskId], _req, res) => {
  const t = state.tasks.get(taskId);
  if (!t) return json(res, 404, { error: `任务不存在: ${taskId}` });
  json(res, 200, {
    task_id: t.task_id,
    node_id: t.node_id,
    name: t.name,
    timeline: t.timeline,
    sampling_json: "",
    status: t.status,
    created_at: t.created_at,
    updated_at: t.updated_at,
  });
});

route("PUT", "/minimax/studio/tasks/:task_id/timeline", async ([taskId], req, res) => {
  const t = state.tasks.get(taskId);
  if (!t) return json(res, 404, { error: `任务不存在: ${taskId}` });
  const b = JSON.parse((await bodyText(req)) || "{}");
  t.timeline = typeof b?.timeline === "string" ? b.timeline : JSON.stringify(b?.timeline ?? {});
  t.updated_at = Date.now() / 1000;
  json(res, 200, { ok: true });
});

route("PUT", "/minimax/studio/tasks/:task_id/name", async ([taskId], req, res) => {
  const t = state.tasks.get(taskId);
  if (!t) return json(res, 404, { error: `任务不存在: ${taskId}` });
  const b = JSON.parse((await bodyText(req)) || "{}");
  const name = String(b?.name || "").trim();
  if (name) t.name = name;
  t.updated_at = Date.now() / 1000;
  json(res, 200, { ok: true });
});

route("DELETE", "/minimax/studio/tasks/:task_id", async ([taskId], _req, res) => {
  state.tasks.delete(taskId);
  state.hist.delete(taskId);
  json(res, 200, { ok: true });
});

route("GET", "/minimax/studio/tasks/:task_id/history", async ([taskId], _req, res) => {
  if (!state.tasks.has(taskId)) return json(res, 404, { error: `任务不存在: ${taskId}` });
  const h = state.hist.get(taskId) || {};
  const out: ClipHist = {};
  for (const clipId of Object.keys(h)) {
    out[clipId] = {
      versions: (h[clipId].versions || []).slice().sort((a, b) => b.createdAt - a.createdAt),
      samples: (h[clipId].samples || []).slice().sort((a, b) => b.createdAt - a.createdAt),
    };
  }
  json(res, 200, { history: out });
});

route("DELETE", "/minimax/studio/tasks/:task_id/clips/:clip_id", async ([taskId, clipId], _req, res) => {
  const h = state.hist.get(taskId);
  if (h) delete h[clipId];
  json(res, 200, { ok: true });
});

route("DELETE", "/minimax/studio/tasks/:task_id/clips/:clip_id/samples/:sample_fp", async ([taskId, clipId, fp], _req, res) => {
  const h = state.hist.get(taskId);
  const clip = h?.[clipId];
  if (clip) clip.samples = clip.samples.filter((s) => s.sampleFp !== fp);
  json(res, 200, { ok: true });
});

route("DELETE", "/minimax/studio/tasks/:task_id/clips/:clip_id/versions/:version_id", async ([taskId, clipId, vid], _req, res) => {
  const h = state.hist.get(taskId);
  const clip = h?.[clipId];
  if (clip) {
    const vn = Number(vid);
    clip.versions = clip.versions.filter((v) => v.versionId !== vn);
    // 删版本连带其下样本
    clip.samples = clip.samples.filter((s) => s.versionId !== vn);
  }
  json(res, 200, { ok: true });
});

route("GET", "/minimax/studio/tasks/:task_id/export", async ([taskId], _req, res) => {
  const t = state.tasks.get(taskId);
  if (!t) return json(res, 404, { error: `任务不存在: ${taskId}` });
  const h = state.hist.get(taskId) || {};
  const history: Record<string, { versions: Record<string, unknown>[]; samples: Record<string, unknown>[] }> = {};
  for (const clipId of Object.keys(h)) {
    history[clipId] = {
      versions: h[clipId].versions.map((v) => ({ versionId: v.versionId, contentFp: v.contentFp, canvas: v.canvas, snapshot: v.snapshot, createdAt: v.createdAt })),
      samples: h[clipId].samples.map((s) => ({ versionId: s.versionId, contentFp: s.contentFp, canvas: s.canvas, sampleFp: s.sampleFp, seed: s.seed, durationSec: s.durationSec, continuity: s.continuity, frames: s.frames, sampleLen: s.sampleLen, createdAt: s.createdAt })),
    };
  }
  json(res, 200, {
    type: "minimax-h3-studio-task",
    version: 1,
    name: t.name,
    timeline: safeParse(t.timeline),
    history,
  });
});

route("POST", "/minimax/studio/tasks/import", async (_p, req, res) => {
  const b = JSON.parse((await bodyText(req)) || "{}");
  const data = b?.data;
  if (!data || typeof data !== "object") return json(res, 400, { error: "缺少 data" });
  const id = nextTaskId();
  const name = String((data as { name?: unknown }).name || "").trim() || "导入任务";
  const task: MockTask = {
    task_id: id,
    node_id: String(b?.node_id || ""),
    name,
    timeline: JSON.stringify(
      (data as { timeline?: unknown }).timeline ?? {
        version: 1,
        canvas: { fps: 24, width: 864, height: 480 },
        clips: [],
        totalDurationSec: 0,
      },
    ),
    status: "created",
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  };
  state.tasks.set(id, task);
  const histSrc = (data as { history?: Record<string, { versions?: Record<string, unknown>[]; samples?: Record<string, unknown>[] }> }).history || {};
  const hist: ClipHist = {};
  for (const clipId of Object.keys(histSrc)) {
    const vlist = histSrc[clipId].versions || [];
    const slist = histSrc[clipId].samples || [];
    const versions: MockVersion[] = vlist.map((v) => ({
      versionId: (state.verSeq += 1),
      contentFp: String(v?.contentFp || ""),
      canvas: v?.canvas as string | undefined,
      snapshot: (v?.snapshot as Record<string, unknown>) || {},
      createdAt: Number(v?.createdAt) || Date.now() / 1000,
    }));
    const samples: MockSample[] = slist.map((s) => ({
      versionId: Number(s?.versionId) || versions[0]?.versionId || 0,
      clipId,
      contentFp: String(s?.contentFp || ""),
      canvas: s?.canvas as string | undefined,
      sampleFp: String(s?.sampleFp || ""),
      seed: Number(s?.seed) || 0,
      durationSec: Number(s?.durationSec) || 0,
      continuity: Boolean(s?.continuity),
      frames: Number(s?.frames) || 0,
      sampleLen: Number(s?.sampleLen) || 0,
      createdAt: Number(s?.createdAt) || Date.now() / 1000,
    }));
    hist[clipId] = { versions, samples };
  }
  state.hist.set(id, hist);
  json(res, 200, { task_id: id });
});

route("POST", "/minimax/studio/tasks/:task_id/duplicate", async ([taskId], req, res) => {
  const t = state.tasks.get(taskId);
  if (!t) return json(res, 404, { error: `任务不存在: ${taskId}` });
  const b = JSON.parse((await bodyText(req)) || "{}");
  const id = nextTaskId();
  const name = String(b?.name || "").trim() || `${t.name} 副本`;
  const timeline = JSON.parse(t.timeline);
  if (Array.isArray(timeline.clips)) for (const c of timeline.clips) delete c.sampleFp;
  const task: MockTask = {
    task_id: id,
    node_id: String(b?.node_id || t.node_id),
    name,
    timeline: JSON.stringify(timeline),
    status: "created",
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
  };
  state.tasks.set(id, task);
  // 复制历史（dev 里等价于保留记录）
  const srcHist = state.hist.get(taskId) || {};
  const dstHist: ClipHist = {};
  for (const clipId of Object.keys(srcHist)) {
    dstHist[clipId] = {
      versions: srcHist[clipId].versions.map((v) => ({ ...v })),
      samples: srcHist[clipId].samples.map((s) => ({ ...s })),
    };
  }
  state.hist.set(id, dstHist);
  json(res, 200, { task_id: id });
});

// ---- 素材列表 / 上传 / 查看 ----

route("GET", "/minimax/studio/list_input_media", async (_p, _req, res, url) => {
  const kind = String(url.searchParams.get("kind") || "").toLowerCase();
  const now = Date.now() / 1000;
  const base = PRESET_MEDIA.map((m, i) => ({ ...m, modified: now - i - 100 }));
  const uploaded = [...state.input.entries()].map(([name, f], i) => ({
    kind: f.kind,
    name,
    relPath: name,
    hue: 0,
    label: name,
    modified: now - 1000 + i,
  }));
  const items = [...base, ...uploaded]
    .filter((m) => {
      if (kind === "image") return m.kind === "image";
      if (kind === "video") return m.kind === "video";
      if (kind === "audio") return m.kind === "audio";
      if (kind === "reference_audio") return m.kind === "audio" || m.kind === "video";
      return true;
    })
    .sort((a, b) => b.modified - a.modified)
    .map((m) => ({
      name: m.name,
      relPath: m.relPath,
      subfolder: "",
      type: "input",
      modified: m.modified,
      width: m.kind === "image" ? 160 : 0,
      height: m.kind === "image" ? 90 : 0,
      mediaKind: m.kind,
    }));
  json(res, 200, { items });
});

route("POST", "/upload/:kind", async ([kind], req, res) => {
  let file;
  try {
    file = await parseMultipart(req);
  } catch {
    return json(res, 400, { error: "上传解析失败" });
  }
  const filename = file.filename || `upload_${Date.now()}`;
  const lower = kind.toLowerCase();
  const mime = file.mime || (lower === "image" ? "image/png" : lower === "video" ? "video/mp4" : "audio/wav");
  state.input.set(filename, { data: file.data, mime, kind: lower });
  json(res, 200, { name: filename, subfolder: "", type: "input" });
});

// ---- dev 专用：模拟一次采样（写入一条版本 + 一条样本） ----

route("POST", "/minimax/dev/sample", async (_p, req, res) => {
  const b = JSON.parse((await bodyText(req)) || "{}");
  const taskId = String(b?.task_id || "");
  const clip = (b?.clip || {}) as Record<string, unknown>;
  const clipId = String(clip.id || "");
  const canvas = String(b?.canvas || "");
  const durationSec = Number(b?.durationSec) || 4;
  if (!state.tasks.has(taskId) || !clipId) return json(res, 400, { error: "缺少 task_id 或 clip" });

  const h = state.hist.get(taskId)!;
  if (!h[clipId]) h[clipId] = { versions: [], samples: [] };
  const c = h[clipId];
  const fp = clipPayloadFp(clip);

  const existingVer = c.versions.find((v) => v.contentFp === fp);
  let versionId: number;
  if (existingVer) {
    versionId = existingVer.versionId;
  } else {
    state.verSeq += 1;
    versionId = state.verSeq;
    c.versions.push({
      versionId,
      contentFp: fp,
      canvas,
      snapshot: snapshotFromClip(clip),
      createdAt: Date.now() / 1000,
    });
  }

  const sampleFp = hash16(fp + "_" + canvas + "_" + Math.random());
  c.samples.push({
    versionId,
    clipId,
    contentFp: fp,
    canvas,
    sampleFp,
    seed: Math.floor(Math.random() * 1e9),
    durationSec,
    continuity: Boolean(clip.continuity),
    frames: Math.round(durationSec * 24),
    sampleLen: Math.round(durationSec * 24),
    createdAt: Date.now() / 1000,
  });
  json(res, 200, { ok: true, sampleFp });
});

/** 从 clip 载荷构造画面语义快照（PromptSnapshot 形状） */
function snapshotFromClip(clip: Record<string, unknown>): Record<string, unknown> {
  const media = (m?: unknown): unknown => {
    if (!m || typeof m !== "object") return undefined;
    const mm = m as { path?: string; name?: string; kind?: string };
    return mm?.path || mm?.name ? { path: mm.path || mm.name || "", kind: mm.kind || "image" } : undefined;
  };
  const arr = (l?: unknown): unknown[] | undefined => {
    if (!Array.isArray(l)) return undefined;
    const mapped = l.map((m) => media(m)).filter(Boolean);
    return mapped.length ? mapped : undefined;
  };
  const snap: Record<string, unknown> = {
    id: String(clip.id || ""),
    mode: String(clip.mode || "t2v"),
    prompt: String(clip.prompt || ""),
  };
  const f = media(clip.firstFrame);
  if (f) snap.firstFrame = f;
  const l = media(clip.lastFrame);
  if (l) snap.lastFrame = l;
  const ri = arr(clip.refImages as unknown);
  if (ri) snap.refImages = ri;
  const rv = arr(clip.refVideos as unknown);
  if (rv) snap.refVideos = rv;
  const ra = arr(clip.refAudios as unknown);
  if (ra) snap.refAudios = ra;
  const sv = media(clip.sourceVideo);
  if (sv) snap.sourceVideo = sv;
  return snap;
}

function safeParse<T>(s: string): T {
  try {
    return JSON.parse(s) as T;
  } catch {
    return {} as T;
  }
}

// ---- /view 图片查看（<img> 直连 / 样本预览 fetch） ----

const MIME: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
  mp4: "video/mp4",
  wav: "audio/wav",
};

const TINY_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

function serveView(res: Res, url: URL): void {
  const filename = url.searchParams.get("filename") || "";
  const uploaded = state.input.get(filename);
  if (uploaded) {
    res.statusCode = 200;
    res.setHeader("content-type", uploaded.mime);
    res.end(uploaded.data);
    return;
  }
  const preset = presetFor(filename);
  if (preset) {
    res.statusCode = 200;
    res.setHeader("content-type", "image/svg+xml");
    res.end(svgImage(preset.hue, preset.label));
    return;
  }
  const ext = (filename.split(".").pop() || "").toLowerCase();
  const seedHue = parseInt(hash16(filename).slice(0, 2), 16) % 360;
  res.statusCode = 200;
  res.setHeader("content-type", MIME[ext] || "image/svg+xml");
  if (MIME[ext]) {
    res.end(TINY_PNG);
  } else {
    res.end(svgImage(Number.isNaN(seedHue) ? 200 : seedHue, filename));
  }
}

// ---------- vite 插件 ----------

export function minimaxStudioDevMock(): Plugin {
  return {
    name: "minimax-h3-studio-dev-mock",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = new URL(req.url || "/", "http://x");
        const pathname = url.pathname;
        const method = (req.method || "GET").toUpperCase();

        if (pathname === "/view") {
          serveView(res as Res, url);
          return;
        }
        for (const r of routeHandlers) {
          if (r.method !== method) continue;
          const m = r.re.exec(pathname);
          if (!m) continue;
          const params = m.slice(1).map((s) => decodeURIComponent(s || ""));
          r.handle(params, req as Req, res as Res, url).catch((err) => {
            json(res as Res, 500, { error: String((err as Error)?.message || err) });
          });
          return;
        }
        next();
      });
    },
  };
}
