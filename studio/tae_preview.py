"""采样预览解码器 —— TAE（tiny VAE）实时预览 + 动画 WebP 编码。

产品规格（用户拍板）：
- 预览形态：低帧率低分辨率视频（非单帧），8fps / 8 帧 / 最大边 832px
- 采样中：卡片实时显示（live，TAE 解码当前步 x0 的 video 流）
- 采样完成：final preview 落盘（绑定 sample_fp，与 latent 缓存同生命周期）

技术要点（实现思路参考 KJNodes tiny_vae.py，见 README 致谢）：
- ComfyUI 官方 TAESD 对 H3 不兼容：官方 Decoder 硬编码 64 宽 3 上采样，taeh3 是
  96 宽 4 上采样 + patch_size=2 的 temporal 结构，需按 checkpoint 结构自建解码器
- taeh3 是 temporal 格式（TAEHV 家族）：decode_video 用前缀解码 + linspace 抽帧
  （MemBlock 状态链式前传，跨段抽帧必须先解码前缀）
- 解码在采样回调同步执行（x0 是 GPU tensor，引用有生命周期）；PIL → WebP 编码
  放异步线程（有界队列满则丢），不阻塞采样
"""

# Copyright (C) 2026 mangranzhiairen
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with this
# program. If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import base64
import io
import logging
import queue
import threading

import numpy as np
import torch
import torch.nn as nn
from PIL import Image, ImageOps

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.tae_preview")

# 预览规格（产品定稿）：8fps 动画、最大边 832px；预览时长 = 视频实际时长（抽帧降帧率，
# 不做内容快进）——帧数由 latent 时间维按 17 像素帧/5 token 推断，长段以 cap 封顶保护成本
PREVIEW_FPS = 8
PREVIEW_MAX_RES = 832
PREVIEW_QUALITY = 80

# final 预览帧数 = 段实际时长 × 8fps，完全动态、无上限（产品时长已限 30s，帧数天然有界）。
# live 是过程示意：固定 8fps 流畅短循环（24 帧 = 3 秒），解码成本与内容完整度独立设计。
LIVE_PREVIEW_FRAME_CAP = 24
MIN_PREVIEW_FRAMES = 8


def preview_frames_for_latent(latent_t: int, *, canvas_fps: float, cap: int) -> int:
    """按视频实际时长推算预览帧数：帧数 = 时长 × 预览fps。

    H3 latent 时间下采样约 17 像素帧/5 token；时长 = token 数 × 17/5 / 视频fps。
    短段自然得到实际时长（内容真实速度），长段以 cap 封顶（成本保护）。
    """
    if latent_t <= 0:
        return MIN_PREVIEW_FRAMES
    approx_sec = latent_t * 17 / 5 / max(1.0, float(canvas_fps))
    return max(MIN_PREVIEW_FRAMES, min(cap, int(round(approx_sec * PREVIEW_FPS))))


def preview_frames_for_duration(duration_sec: float) -> int:
    """按片段实际时长精确推算预览帧数：帧数 = 时长 × 预览fps，无上限。

    供 final（executor 有 task.segment.duration_sec，精确）；实际解码帧数还会被
    latent token 数限制（decode 时 min(max_frames, t)），播放时长由前端帧率
    自适应兜底（帧数 ÷ 段时长），二者最终一致。
    """
    if duration_sec <= 0:
        return MIN_PREVIEW_FRAMES
    return max(MIN_PREVIEW_FRAMES, int(round(float(duration_sec) * PREVIEW_FPS)))


# ---------- TAE 解码器（flat TAESD 风格，taeh3 走 TAEHVDecoder） ----------

def build_tae_decoder(sd):
    """从 checkpoint 恢复 flat TAE 解码器结构（keys 是位置模块索引，中间空缺是参数化模块）。
    官方 TAESD 硬编码 64 宽 3 上采样无法加载 taeh3（96 宽 4 上采样），必须动态构建。"""
    by_index: dict[int, dict] = {}
    for k, v in sd.items():
        head, _, rest = k.partition(".")
        if not head.isdigit():
            raise ValueError(f"not a flat TAE decoder state dict (unexpected key '{k}')")
        by_index.setdefault(int(head), {})[rest] = v

    from comfy.taesd.taesd import Block, Clamp, conv

    modules = []
    for i in range(max(by_index) + 1):
        entry = by_index.get(i)
        if entry is None:
            # index 0 是输入 Clamp，2 是输入卷积后的 ReLU，其余是上采样
            modules.append(Clamp() if i == 0 else nn.ReLU() if i == 2 else nn.Upsample(scale_factor=2))
        elif "conv.0.weight" in entry:
            w = entry["conv.0.weight"]
            if "pool.0.weight" in entry:
                modules.append(Block(w.shape[1], w.shape[0], use_midblock_gn=True))
            else:
                modules.append(Block(w.shape[1], w.shape[0]))
        elif "weight" in entry:
            w = entry["weight"]
            modules.append(conv(w.shape[1], w.shape[0], bias="bias" in entry))
        else:
            raise ValueError(f"unrecognized TAE decoder module at index {i}: {sorted(entry)}")
    return nn.Sequential(*modules)


class TinyVAEDecoder:
    """flat TAESD 风格解码器（非 temporal；仅兜底，H3 走 TAEHVDecoder）。"""

    def __init__(self, sd, device=None, dtype=None):
        import comfy.model_management

        prefix = ""
        first = next(iter(sd))
        if not first.split(".")[0].isdigit():
            prefix = first.split(".")[0] + "."
            sd = {k[len(prefix):]: v for k, v in sd.items() if k.startswith(prefix)}
        self.device = device if device is not None else comfy.model_management.vae_device()
        self.dtype = dtype if dtype is not None else comfy.model_management.vae_dtype(
            self.device, [torch.float16, torch.bfloat16])
        self.model = build_tae_decoder(sd)
        self.model.load_state_dict(sd)
        self.model = self.model.eval().to(device=self.device, dtype=self.dtype)
        self.latent_channels = self.model[1].weight.shape[1]
        self.upscale_ratio = 2 ** sum(isinstance(m, nn.Upsample) for m in self.model)

    def decode(self, latent):
        """[B, C, H, W] -> [B, 3, H*r, W*r] float32。"""
        with torch.no_grad():
            out = self.model(latent.to(device=self.device, dtype=self.dtype))
        return out.to(device=latent.device, dtype=torch.float32)

    def decode_video(self, latent, frame_indices=None):
        """[B, C, T, H, W] -> [n, H*r, W*r, 3]，逐帧解码（flat 模型无时间结构）。"""
        x = latent[0]
        indices = range(x.shape[1]) if frame_indices is None else frame_indices
        frames = [self.decode(x[:, t].unsqueeze(0))[0].movedim(0, -1) for t in indices]
        return torch.stack(frames, dim=0)


class TAEHVDecoder:
    """temporal tiny VAE（taeh3 所属家族），decode only。

    与官方 TAEHV 的差异：官方从通道数推导 patch_size，无 H3 24 通道条目，
    需按 checkpoint 修正（patch_size=2 的两处 conv）。
    """

    def __init__(self, sd, device=None, dtype=None):
        import comfy.model_management
        from comfy.taesd.taehv import TAEHV, conv

        latent_channels = sd["decoder.1.weight"].shape[1]
        patch_size = max(1, int(round((sd["decoder.22.bias"].shape[0] / 3) ** 0.5)))
        model = TAEHV(latent_channels=latent_channels)
        if model.patch_size != patch_size:
            model.patch_size = patch_size
            model.encoder[0] = conv(3 * patch_size**2, model.encoder[0].out_channels)
            model.decoder[-1] = conv(model.decoder[-1].in_channels, 3 * patch_size**2)
        model.load_state_dict(sd)
        del model.encoder  # 仅解码，编码器直接卸载（省显存）

        self.device = device if device is not None else comfy.model_management.vae_device()
        self.dtype = dtype if dtype is not None else comfy.model_management.vae_dtype(
            self.device, [torch.float16, torch.bfloat16])
        self.model = model.eval().to(device=self.device, dtype=self.dtype)
        self.latent_channels = latent_channels
        self.is_h3 = latent_channels == 24 and patch_size == 2

    def _decode(self, latent):
        """[B, C, T, H, W] -> [B, 3, T', H*r, W*r]（T' 为 17 像素帧/5 token 比例，偏多）。"""
        with torch.no_grad():
            out = self.model.decode(latent.to(device=self.device, dtype=self.dtype))
        return out.to(device=latent.device, dtype=torch.float32)

    def decode_video(self, latent, frame_indices=None):
        """[B, C, T, H, W] -> [n, H*r, W*r, 3]。

        MemBlock 状态链式前传，跨段抽帧必须先解码前缀——取前 n 个 token 解码
        一次前向，再 linspace 均匀抽 n 帧（预览只要 ≤8 帧，成本可控）。
        """
        t_total = latent.shape[2]
        n = t_total if frame_indices is None else max(1, min(len(frame_indices), t_total))
        out = self._decode(latent[:1, :, :n])[0].movedim(0, -1)
        if out.shape[0] > n:
            out = out[torch.linspace(0, out.shape[0] - 1, n).round().long()]
        return out


# ---------- 加载（懒加载单例，线程安全） ----------

_tae_lock = threading.Lock()
_tae_previewer = None
_tae_name = ""


def load_tae_previewer(name: str = "taeh3.safetensors"):
    """懒加载 vae_approx 下的 tiny VAE 解码器（进程内单例复用）。失败返回 None（预览静默降级）。"""
    global _tae_previewer, _tae_name
    if _tae_previewer is not None and _tae_name == name:
        return _tae_previewer
    with _tae_lock:
        if _tae_previewer is not None and _tae_name == name:
            return _tae_previewer
        _tae_previewer = _load(name)
        _tae_name = name
        return _tae_previewer


def _load(name: str):
    import comfy.utils
    import folder_paths

    path = folder_paths.get_full_path("vae_approx", name)
    if path is None:
        log.warning("TAE 预览解码器 '%s' 不存在于 models/vae_approx，预览禁用", name)
        return None
    try:
        sd = comfy.utils.load_torch_file(path, safe_load=True)
        if "decoder.1.weight" in sd and "decoder.22.bias" in sd:
            return TAEHVDecoder(sd)
        return TinyVAEDecoder(sd)
    except Exception as exc:  # noqa: BLE001
        log.warning("TAE 预览解码器加载失败（预览禁用）: %s", exc)
        return None


# ---------- 预览生成 ----------

def video_stream(x0):
    """从 AV latent 取 video 流：兼容 NestedTensor / tuple / 普通 tensor。

    H3 的 AV latent 是 NestedTensor(video 24ch, audio 8ch)，video 在 tensors[0]。
    返回 5D [B, C, T, H, W] tensor（可能仍在 GPU）；无法识别返回 None。
    """
    if x0 is None:
        return None
    if hasattr(x0, "tensors"):  # comfy.nested_tensor.NestedTensor
        parts = x0.tensors
        return parts[0] if parts else None
    if isinstance(x0, (tuple, list)):
        return x0[0] if x0 else None
    if torch.is_tensor(x0):
        return x0
    return None


def decode_preview_frames(latent_video, *, max_frames: int = MIN_PREVIEW_FRAMES):
    """TAE 解码 video latent 流 → PIL 帧列表（均匀抽 ≤max_frames 帧）。失败返回 []。"""
    decoder = load_tae_previewer()
    if decoder is None or latent_video is None or latent_video.ndim != 5:
        return []
    t = int(latent_video.shape[2])
    if t <= 0:
        return []
    n = min(max_frames, t)
    indices = np.linspace(0, t - 1, n).round().astype(int).tolist()
    try:
        rgb = decoder.decode_video(latent_video[:1], frame_indices=indices)
        if rgb is None or rgb.ndim != 4:
            return []
        u8 = rgb.float().clamp(0.0, 1.0).mul(255.0).to(torch.uint8).cpu().numpy()
        return [Image.fromarray(u8[i]) for i in range(u8.shape[0])]
    except Exception as exc:  # noqa: BLE001 预览失败不阻断采样
        log.debug("TAE 预览解码失败: %s", exc)
        return []


def encode_preview_webp(
    frames,
    *,
    fps: int = PREVIEW_FPS,
    max_res: int = PREVIEW_MAX_RES,
    quality: int = PREVIEW_QUALITY,
):
    """PIL 帧列表 → base64 动画 WebP（等比缩到 max_res 内）。失败返回 None。"""
    if not frames:
        return None
    try:
        pil: list[Image.Image] = []
        for f in frames:
            pf = f.convert("RGB") if f.mode != "RGB" else f
            if max_res > 0 and (pf.width > max_res or pf.height > max_res):
                pf = ImageOps.contain(pf, (max_res, max_res), Image.LANCZOS)
            pil.append(pf)
        buf = io.BytesIO()
        duration_ms = max(1, int(round(1000 / max(1, fps))))
        pil[0].save(
            buf, format="WEBP", save_all=True, append_images=pil[1:],
            duration=duration_ms, loop=0, quality=quality, method=4,
        )
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        log.debug("动画 WebP 编码失败: %s", exc)
        return None


def save_preview_file(path, frames, *, fps: int = PREVIEW_FPS, max_res: int = PREVIEW_MAX_RES):
    """final preview 落盘（best-effort，失败不阻断采样）。返回是否成功。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        b64 = encode_preview_webp(frames, fps=fps, max_res=max_res)
        if not b64:
            return False
        path.write_bytes(base64.b64decode(b64))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("preview 落盘失败: %s", exc)
        return False


# ---------- 异步编码器（live 预览：编码/发送不阻塞采样） ----------

class AsyncPreviewEncoder:
    """独立线程 + 有界队列：满则丢弃（丢预览不丢采样）。进程级单例，不复用 shutdown。"""

    def __init__(self, max_in_flight: int = 2):
        self.q: queue.Queue = queue.Queue(maxsize=max_in_flight)
        self.thread = threading.Thread(target=self._run, name="studio_preview_encoder", daemon=True)
        self.thread.start()

    def submit(self, fn) -> bool:
        try:
            self.q.put_nowait(fn)
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is self._STOP:
                return
            try:
                item()
            except Exception:  # noqa: BLE001 编码/发送异常不影响采样
                log.debug("异步预览编码失败", exc_info=True)

    _STOP = object()


_encoder_singleton: AsyncPreviewEncoder | None = None


def get_encoder() -> AsyncPreviewEncoder:
    """进程级单例（daemon 线程随进程退出，不 shutdown）。"""
    global _encoder_singleton
    if _encoder_singleton is None:
        _encoder_singleton = AsyncPreviewEncoder()
    return _encoder_singleton
