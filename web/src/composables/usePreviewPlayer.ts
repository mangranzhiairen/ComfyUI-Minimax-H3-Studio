/**
 * 动画 WebP 预览播放器
 *
 * 思路对齐 KJNodes Model Preview Override：WebP 内嵌的 ANMF duration 在浏览器中
 * 不可靠（Pillow 编码的 duration 常被浏览器忽略或误读 → 播放速度失控），因此
 * 用 ImageDecoder（WebCodecs）把动画解码成 VideoFrame[] 后，由 JS 定时器按帧率
 * 控制逐帧绘制到 canvas —— 播放速率完全由代码控制，与文件内 duration 无关。
 *
 * 帧率语义与 KJNodes Model Preview Override 对齐：预览时长 = 视频实际时长，
 * 即播放帧率 = 帧数 ÷ 段时长（自适应，不固定 8fps）——5 秒的段抽多少帧都播满
 * 5 秒，只是画面疏密不同。
 *
 * 用法：
 *   const previewCanvas = ref<HTMLCanvasElement | null>(null);
 *   const { playFrom, stop } = usePreviewPlayer(previewCanvas);
 *   playFrom(blob, durationSec)  // 动画 WebP Blob + 段时长（秒），循环播放
 *   stop()                       // 停止并释放帧
 */
import { onBeforeUnmount, type Ref } from "vue";

export const PREVIEW_FPS = 8;

export function usePreviewPlayer(
  canvasRef: Ref<HTMLCanvasElement | null>,
  fps: number = PREVIEW_FPS,
  maxSize?: { width?: number; height?: number },
) {
  let frames: VideoFrame[] = [];
  let activeFps = fps;
  let rafId = 0;
  let playing = false;
  let startMs = 0;

  function draw(timestamp: number) {
    if (!playing) return;
    const c = canvasRef.value;
    if (c && frames.length) {
      const ctx = c.getContext("2d");
      if (ctx) {
        const idx = Math.floor(((timestamp - startMs) / 1000) * activeFps) % frames.length;
        const f = frames[idx];
        // 画布尺寸：按视频帧宽高比等比缩放（无黑边）
        // - 大预览弹框（canvas 带 data-big）：以 maxSize（视口上限）为约束，画布即帧等比例尺寸
        // - 卡片内预览：以父容器为窗口 contain（CSS 居中）
        const isBig = maxSize != null && c.hasAttribute("data-big");
        const parent = c.parentElement;
        const maxW = isBig
          ? maxSize.width ?? (parent?.clientWidth || c.clientWidth || f.displayWidth)
          : (parent?.clientWidth || c.clientWidth || f.displayWidth);
        const maxH = isBig
          ? maxSize.height ?? (parent?.clientHeight || c.clientHeight || f.displayHeight)
          : (parent?.clientHeight || c.clientHeight || f.displayHeight);
        const scale = Math.min(maxW / f.displayWidth, maxH / f.displayHeight);
        const w = Math.max(1, Math.round(f.displayWidth * scale));
        const h = Math.max(1, Math.round(f.displayHeight * scale));
        if (c.width !== w || c.height !== h) {
          c.width = w;
          c.height = h;
        }
        ctx.drawImage(f, 0, 0, w, h);
      }
    }
    rafId = requestAnimationFrame(draw);
  }

  function stopInternal() {
    playing = false;
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
    for (const f of frames) {
      try {
        f.close();
      } catch {
        /* 已关闭则忽略 */
      }
    }
    frames = [];
  }

  async function decodeBlob(blob: Blob): Promise<VideoFrame[]> {
    if (typeof ImageDecoder === "undefined") return [];
    let decoder: ImageDecoder | null = null;
    try {
      decoder = new ImageDecoder({ data: blob.stream(), type: blob.type });
      await decoder.completed;
      const track = decoder.tracks.selectedTrack;
      if (!track || track.frameCount <= 1) return [];
      const out: VideoFrame[] = [];
      for (let i = 0; i < track.frameCount; i++) {
        const r = await decoder.decode({ frameIndex: i });
        out.push(r.image);
      }
      return out;
    } catch {
      return [];
    } finally {
      decoder?.close?.();
    }
  }

  /** 播放新动画（自动替换旧的，从头循环）。durationSec 为视频实际时长（秒）：
   *  播放帧率 = 帧数 ÷ 时长，保证预览时长与视频一致；不传则用默认 fps。
   *  返回是否成功（有帧可播）；失败/非动画时已停止并返回 false。 */
  async function playFrom(blob: Blob, durationSec?: number): Promise<boolean> {
    stopInternal();
    const decoded = await decodeBlob(blob);
    if (!decoded.length) return false;
    frames = decoded;
    // KJ 语义：预览时长 = 视频实际时长（帧率 = 帧数/时长，长段自动低帧率）
    activeFps =
      durationSec && durationSec > 0 ? frames.length / durationSec : fps;
    playing = true;
    startMs = performance.now();
    rafId = requestAnimationFrame(draw);
    return true;
  }

  function stop(): void {
    stopInternal();
    const c = canvasRef.value;
    if (c) c.getContext("2d")?.clearRect(0, 0, c.width, c.height);
  }

  onBeforeUnmount(stop);

  return { playFrom, stop };
}