/**
 * 预览缩略图（静态首帧）
 *
 * 历史区采样缩略图直接用 <img> 播动画 WebP 会遇到与卡片预览相同的 duration
 * 失控问题（Pillow 编码的 WebP 动画在浏览器中帧率不可靠），且小尺寸下动画
 * 意义有限——这里用 ImageDecoder 解码出第一帧绘制到 canvas，静态展示。
 */
import { type Ref } from "vue";

export function usePreviewThumb(canvasRef: Ref<HTMLCanvasElement | null>) {
  async function showFromUrl(url: string): Promise<void> {
    const c = canvasRef.value;
    if (!c || typeof ImageDecoder === "undefined") return;
    try {
      const res = await fetch(url);
      if (!res.ok) return;
      const blob = await res.blob();
      const decoder = new ImageDecoder({ data: blob.stream(), type: blob.type });
      await decoder.completed;
      const track = decoder.tracks.selectedTrack;
      if (!track || track.frameCount <= 0) {
        decoder.close?.();
        return;
      }
      const { image } = await decoder.decode({ frameIndex: 0 });
      const ctx = c.getContext("2d");
      if (ctx) {
        // contain 完整显示（竖屏内容在窄缩略图内不裁切，四周留边）
        const cw = c.clientWidth || c.width;
        const ch = c.clientHeight || c.height;
        c.width = cw;
        c.height = ch;
        const scale = Math.min(cw / image.displayWidth, ch / image.displayHeight);
        const w = image.displayWidth * scale;
        const h = image.displayHeight * scale;
        ctx.clearRect(0, 0, cw, ch);
        ctx.drawImage(image, (cw - w) / 2, (ch - h) / 2, w, h);
      }
      image.close?.();
      decoder.close?.();
    } catch {
      /* 缩略图是增强信息，失败忽略 */
    }
  }

  function clear(): void {
    const c = canvasRef.value;
    if (c) c.getContext("2d")?.clearRect(0, 0, c.width, c.height);
  }

  return { showFromUrl, clear };
}
