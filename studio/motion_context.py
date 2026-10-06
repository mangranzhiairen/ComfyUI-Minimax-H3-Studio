"""段间引导 —— Motion Context 的 latent 直接传递方案（不 VAE 解码）。

上一段采样输出的 AV latent 尾部按 latent step 直接切成 keyframes，
钉入下一段 conditioning（minimax_keyframes，含每步真实帧偏移）；
音频同样从 latent 直接切尾部。采样长度 = 可见帧 + 上下文帧（网格对齐），
解码后裁掉钉入的前缀（trim）。

**钉入内容的网格 = 当级采样的网格**：条件行是按 patch 贴到采样画布上的，尺寸必须与
"正在被采样的那张 latent"一致。单级流程里两者天然一致；但采样流程（子图）可以在中途
放大 latent（二采），那时第二级采样换了画布，而钉入内容是在跑图前按第一级画布做好的
→ 贴不上（core 报 shape mismatch）。

因此钉入时**保留原始块**（FIT_SOURCE_KEY），并由 install_cond_grid_fit() 在
MiniMaxH3.extra_conds 编码条件之前（每级采样一次，参数里正好带着当级 latent_shapes）
按当级网格重缩一次：第一级缩到目标网格，第二级若是原生网格则原样使用（零重采样，
不会因为"先缩后放"而变糊）。

实现思路参考 NikoDemon80 的 ComfyUI-H3-Motion-Context 项目（致谢见 README，
该项目为 GPL-3.0；本仓库整体亦以 GPL-3.0 发布，见仓库根 LICENSE）。
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

import logging
import threading

import torch

from .payload import align_frame_count

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.motion_context")

FPS = 24.0
AUDIO_HZ = 40.0
FRAME_RESCALE = 5.0 / 3.0
# H3 VAE 每个 latent step 覆盖的像素帧数（5 帧周期）
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

# 像素窗口 ↔ 整数字符串 step 的可选上下文长度
CONTEXT_FRAME_CHOICES = (5, 22, 39, 56)
DEFAULT_CONTEXT_FRAMES = 22
DEFAULT_AUDIO_CONTEXT_FRAMES = 24
# 可选 pin 窗口（必须能拆成整数 latent steps）
VIDEO_RUN_GRID = (124, 107, 90, 73, 56, 39, 22, 5, 1)

# 钉入条目里的两个簿记键（见 fit_cond_video_latents）：
#   FIT_SOURCE_KEY —— 上一段的**原始块**，按级缩放永远从它出发
#   FIT_TAG_KEY    —— 当前 latent 是按哪个网格得到的，用来跳过本级的重复缩放
FIT_SOURCE_KEY = "_studio_grid_source"
FIT_TAG_KEY = "_studio_grid_fit"
# 按级缩放时打印过的网格（避免正/负条件各打一遍）
_FIT_LOGGED: set[tuple[int, int, int, int]] = set()
_GRID_FIT_LOCK = threading.Lock()
_GRID_FIT_STATE: dict = {"depth": 0, "cls": None, "orig": None, "patched": None}


def snap_context_frames(raw: int | float | None) -> int:
    """吸附到支持的上下文窗口（默认 22）。"""
    try:
        n = int(raw or DEFAULT_CONTEXT_FRAMES)
    except (TypeError, ValueError):
        n = DEFAULT_CONTEXT_FRAMES
    return int(min(CONTEXT_FRAME_CHOICES, key=lambda g: (abs(g - n), -g)))


def pixel_frames_for_latent_t(latent_t: int) -> int:
    """latent 时间步 → 覆盖的像素帧数。"""
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(int(latent_t)))


def steps_for_frames(n: int) -> int | None:
    """n 帧恰好是几个 latent step；不是整步返回 None。"""
    k, covered = 0, 0
    while covered < n:
        covered += FRAME_PER_TOKEN[k % 5]
        k += 1
    return k if covered == n else None


def step_offsets(latent_t: int) -> list[int]:
    """每个 latent step 对应的像素帧起点（0 起）。"""
    out, acc = [], 0
    for k in range(int(latent_t)):
        out.append(acc)
        acc += FRAME_PER_TOKEN[k % 5]
    return out


def _streams_from_latent(latent: dict) -> list[torch.Tensor]:
    samples = latent["samples"]
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = list(samples)
    else:
        raise ValueError(f"AV latent 无法拆分流: {type(samples)!r}")
    if not parts:
        raise ValueError("AV latent 没有流")
    return parts


def video_from_latent(latent: dict) -> torch.Tensor:
    """AV latent 的第一流 → 视频 latent [B,C,T,H,W]。"""
    video = _streams_from_latent(latent)[0]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if video.ndim != 5:
        raise ValueError(f"期望视频 latent [B,C,T,H,W]，得到 {tuple(video.shape)}")
    return video


def _video_tail_blocks(latent: dict, n: int) -> tuple[list[torch.Tensor], list[int], int]:
    """从 latent 末尾切出 n 帧对应的 latent 块（每步 [1,C,1,H,W]）+ 偏移 + 覆盖帧数。"""
    video = video_from_latent(latent)
    total = int(video.shape[2])
    steps = steps_for_frames(n)
    if steps is None:
        raise ValueError(
            f"{n} 帧不是整数 latent steps（可用: {', '.join(str(x) for x in CONTEXT_FRAME_CHOICES)}）"
        )
    if steps > total:
        raise ValueError(f"上下文只有 {total} 步，无法切 {steps} 步")
    start = total - steps
    # 相位断言：tail 起始必须在 5 帧周期位置 0（17k+5 帧 → 5g+2 步，窗口 2/7/12/17 步
    # 恒满足；若 VAE 网格变化则拒绝，避免 join 相位错位静默偏移）
    if start % 5 != 0:
        raise RuntimeError(
            f"段间引导: {steps} 步 tail 从 {total} 步 latent 切出的起点在周期位置 "
            f"{start % 5}（应为 0），VAE 网格与预期不符，拒绝错位 join"
        )
    blocks = [video[:1, :, start + k : start + k + 1].clone() for k in range(steps)]
    covered = pixel_frames_for_latent_t(steps)
    if covered != n:
        raise RuntimeError(
            f"段间引导: {steps} 步覆盖 {covered} 帧，预期 {n} 帧（VAE 网格变化？）"
        )
    return blocks, step_offsets(steps), covered


def _audio_tail_from_latent(latent: dict, a_frames: int) -> tuple[torch.Tensor, int]:
    """从 AV latent 的音频流末尾切出音频 latent（stock ref 模式）。"""
    parts = _streams_from_latent(latent)
    if len(parts) < 2:
        raise ValueError("上下文 latent 没有音频流")
    video, audio = parts[0], parts[1]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if audio.ndim == 3:
        audio = audio.unsqueeze(0)
    if audio.ndim != 4:
        raise ValueError(f"期望音频 latent [B,C,2,T]，得到 {tuple(audio.shape)}")
    total_t = int(audio.shape[-1])
    frames = pixel_frames_for_latent_t(int(video.shape[2]))
    rt = int(round(a_frames / FPS * AUDIO_HZ))
    rt = max(1, min(rt, total_t))
    return audio[:1, ..., total_t - rt :].clone(), rt


def _latent_hw(tensor) -> tuple[int, int] | None:
    """视频 latent 的空间网格 (H, W)（latent 单位）。"""
    if not torch.is_tensor(tensor) or tensor.ndim < 4:
        return None
    return int(tensor.shape[-2]), int(tensor.shape[-1])


def _fit_video_latent(tensor, dst_h: int, dst_w: int):
    """把视频 latent 缩到目标网格；已经一致时原样返回（零拷贝）。"""
    if _latent_hw(tensor) == (int(dst_h), int(dst_w)):
        return tensor
    from .context_conform import resize_video_latent

    return resize_video_latent(tensor, int(dst_h), int(dst_w))


def fit_cond_video_latents(kwargs: dict, latent_shapes) -> dict:
    """把 conditioning 里**钉入的 keyframes** 缩到当级采样网格（每级一次）。

    在 MiniMaxH3.extra_conds 编码条件之前调用：那一刻条件才真正变成条件行，而参数里
    正好带着当级的 latent_shapes（comfy/samplers.py: process_conds → encode_model_conds
    → extra_conds）。keyframes 从 FIT_SOURCE_KEY 保存的原始块出发缩放（图里放大过时，
    第二级若正好是原生网格就是零重采样）；用户自带的锚点没有原始块，就从当前值缩一次
    （条件本身不被就地改写，所以每一级都从同一份出发，可重复）。

    ⚠️ 只有 minimax_keyframes 吃目标网格（PackedLayout：n = vt * frame_rows，注释写明
    "sharing the target spatial grid"）。**minimax_refs（参考图/参考视频）用的是它自己
    的网格**（_frame_grid(blk["latent_h"], blk["latent_w"])），把它们的 latent 缩了却不同步
    latent_h/latent_w 会让行数对不上（实测报 [3726,96] vs [3543,96]），所以这里绝不碰 refs。
    """
    try:
        shapes = list(latent_shapes or [])
        vs = shapes[0] if shapes else None
        if vs is None:
            return kwargs
        dst_h, dst_w = int(vs[-2]), int(vs[-1])
        if dst_h < 1 or dst_w < 1:
            return kwargs
    except Exception:  # noqa: BLE001 形状不可读就完全不插手
        return kwargs

    out = kwargs
    # 只处理 keyframes：refs 自带网格元数据（latent_h/latent_w），与目标网格无关，不能缩
    for key in ("minimax_keyframes",):
        items = kwargs.get(key)
        if not isinstance(items, list) or not items:
            continue
        key_changed = False
        new_items: list = []
        for item in items:
            if not isinstance(item, dict):
                new_items.append(item)
                continue
            current = item.get("latent")
            if not torch.is_tensor(current):
                new_items.append(item)
                continue
            target = (dst_h, dst_w)
            if item.get(FIT_TAG_KEY) == target:
                # 本级已经按这个网格缩过（钉入时那份静态兜底也带标记）
                new_items.append(item)
                continue
            source = item.get(FIT_SOURCE_KEY)
            base = source if torch.is_tensor(source) else current
            src_hw = _latent_hw(base)
            fitted = dict(item)
            # 原件网格 == 目标网格时**原样用**（图里放大后第二级正好是原生网格，零重采样）
            fitted["latent"] = _fit_video_latent(base, dst_h, dst_w)
            fitted[FIT_TAG_KEY] = target
            new_items.append(fitted)
            key_changed = True
            sig = (
                int(src_hw[0]) if src_hw else -1,
                int(src_hw[1]) if src_hw else -1,
                dst_h,
                dst_w,
            )
            if sig not in _FIT_LOGGED:
                _FIT_LOGGED.add(sig)
                same_grid = src_hw == target
                log.info(
                    "段间续接: 本级采样网格 %dx%d ← 钉入内容 %dx%d（%s，%s）",
                    dst_h, dst_w,
                    int(src_hw[0]) if src_hw else -1, int(src_hw[1]) if src_hw else -1,
                    "原生网格，原样使用" if same_grid else "按级缩放",
                    key,
                )
        if key_changed:
            if out is kwargs:
                out = dict(kwargs)
            out[key] = new_items
    return out


def _patch_target_class(model):
    """从 model（ModelPatcher / BaseModel 实例 / 类）找出真正定义了 extra_conds 的类。"""
    for candidate in (
        model,
        getattr(model, "model", None),
        getattr(getattr(model, "model", None), "model", None),
    ):
        if candidate is None:
            continue
        cls = candidate if isinstance(candidate, type) else type(candidate)
        if hasattr(cls, "extra_conds"):
            return cls
    return None


def install_cond_grid_fit(model) -> bool:
    """临时让模型类的 extra_conds 在编码条件前按当级网格重缩视频 latent。

    只在跑续接采样期间生效，uninstall_cond_grid_fit() 会恢复原方法；网格一致时是 no-op。
    返回是否装上（装不上时行为退回"钉入时按本段网格缩一次"的静态兜底）。
    """
    cls = _patch_target_class(model)
    if cls is None:
        return False
    with _GRID_FIT_LOCK:
        if _GRID_FIT_STATE["depth"] > 0:
            _GRID_FIT_STATE["depth"] += 1
            return True
        orig = getattr(cls, "extra_conds", None)
        if orig is None:
            return False

        def _patched(self, **kwargs):
            try:
                kwargs = fit_cond_video_latents(kwargs, kwargs.get("latent_shapes"))
            except Exception:  # noqa: BLE001 缩放失败不能阻断采样
                log.debug("段间续接: 按级网格缩放跳过", exc_info=True)
            return orig(self, **kwargs)

        try:
            cls.extra_conds = _patched  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001
            log.warning("段间续接: 按级网格缩放未装上（%s）", exc)
            return False
        _GRID_FIT_STATE.update(depth=1, cls=cls, orig=orig, patched=_patched)
        _FIT_LOGGED.clear()
        log.debug("段间续接: 按级网格缩放已装上（%s.extra_conds）", cls.__name__)
        return True


def uninstall_cond_grid_fit() -> None:
    """恢复 install_cond_grid_fit() 改过的 extra_conds（嵌套安装按层计数）。"""
    with _GRID_FIT_LOCK:
        if _GRID_FIT_STATE["depth"] <= 0:
            return
        _GRID_FIT_STATE["depth"] -= 1
        if _GRID_FIT_STATE["depth"] > 0:
            return
        cls = _GRID_FIT_STATE["cls"]
        orig = _GRID_FIT_STATE["orig"]
        patched = _GRID_FIT_STATE["patched"]
        try:
            if cls is not None and orig is not None:
                if getattr(cls, "extra_conds", None) is patched:
                    cls.extra_conds = orig  # type: ignore[assignment]
                else:
                    log.warning(
                        "段间续接: %s.extra_conds 已被别人改过，跳过恢复",
                        getattr(cls, "__name__", cls),
                    )
        finally:
            _GRID_FIT_STATE.update(depth=0, cls=None, orig=None, patched=None)
            _FIT_LOGGED.clear()


def apply_motion_context(
    positive,
    latent: dict,
    context_latent: dict,
    context_length: int,
    audio_context_length: int | None = None,
    conform: bool = True,
) -> tuple[list, int]:
    """把上一段 latent 尾部钉入当前 conditioning，返回 (positive, trim_frames)。

    自动检测：上一段 latent 与本节目标网格一致时零拷贝直通、不做任何整形；不一致时
    （例如上一段走二采/放大后是 1.0 网格，本段低清阶段是 0.4 目标网格）自动做通用
    context 空间整形，只缩视频流，音频/时间轴不动（见 context_conform）。
    conform=False 仅供排查时强制严格报错。
    """
    import node_helpers

    ctx_frames = snap_context_frames(context_length)

    video = video_from_latent(latent)
    width = int(video.shape[4]) * 16
    height = int(video.shape[3]) * 16
    frame_count = pixel_frames_for_latent_t(int(video.shape[2]))

    # 自动检测：前后画布不一致才整形。一致时提前判掉，省一次拆/装流的开销，
    # 也避免对本来匹配的 context 做无意义的插值（conform_context_latent 本身
    # 匹配时零拷贝返回，这里是显式的提前判定 + 日志）。
    src = video_from_latent(context_latent)
    src_w, src_h = int(src.shape[4]) * 16, int(src.shape[3]) * 16
    if (src_w, src_h) != (width, height):
        if not conform:
            raise ValueError(
                f"段间引导: 上一段 latent 是 {src_w}x{src_h}，当前段是 {width}x{height}；"
                "conform=False（排查用）时拒绝缩放，请保证各段画布一致"
            )
        old_w, old_h = src_w, src_h
        # 例：上一段走二采/放大后是 1.0 网格，本段低清阶段是 0.4 目标网格。
        # 只对视频流做空间整形，音频与时间轴不动（见 context_conform）。
        from .context_conform import conform_context_latent

        # 静态兜底：按本段目标网格缩一份（不装按级钩子时的行为与旧版一致）
        fitted_context = conform_context_latent(context_latent, target_latent=latent)
        src_fitted = video_from_latent(fitted_context)
        if (int(src_fitted.shape[4]) * 16, int(src_fitted.shape[3]) * 16) != (width, height):
            raise ValueError(
                f"段间引导: context 整形后仍是 "
                f"{int(src_fitted.shape[4]) * 16}x{int(src_fitted.shape[3]) * 16}，"
                f"当前段 {width}x{height}；context_conform 未能对齐目标网格"
            )
        log.info(
            "段间引导: 自动检测到画布不一致，context 视频流 %dx%d → %dx%d"
            "（本段网格；每级采样前还会按当级网格重缩一次，音频/时间轴不动）",
            old_w, old_h, width, height,
        )
    else:
        fitted_context = context_latent
        log.debug("段间引导: 前后画布一致（%dx%d），不做空间整形", width, height)
    if int(src.shape[1]) != int(video.shape[1]):
        raise ValueError(
            f"段间引导: 上一段 latent 有 {int(src.shape[1])} 个通道，当前段有 "
            f"{int(video.shape[1])} 个；这不是同一模型产出的 H3 视频 latent"
        )

    available = pixel_frames_for_latent_t(int(src.shape[2]))
    n = min(ctx_frames, available)
    n = next(g for g in VIDEO_RUN_GRID if g <= n)
    if n >= frame_count:
        raise ValueError(f"段间引导: 无法把 {n} 帧钉进 {frame_count} 帧的片段")

    # 钉入块：静态兜底取自"按本段网格缩过的"那份；FIT_SOURCE_KEY 保留**原始**块，
    # 供按级缩放（install_cond_grid_fit）在图里放大后重缩 —— 原件丢了就只能从缩小版
    # 放大回去，第二级会糊。
    blocks_fitted, offsets, covered = _video_tail_blocks(fitted_context, n)
    blocks_source, _, _ = _video_tail_blocks(context_latent, n)
    target_hw = (int(video.shape[3]), int(video.shape[4]))
    ctx_keyframes = [
        {
            "resolved_frame_index": int(p),
            "latent": fitted,
            FIT_SOURCE_KEY: source,
            FIT_TAG_KEY: target_hw,
        }
        for p, fitted, source in zip(offsets, blocks_fitted, blocks_source)
    ]

    # 音频：从上一段 latent 直接切尾部，作为 stock reference 追加
    audio_ctx = audio_context_length if audio_context_length is not None else DEFAULT_AUDIO_CONTEXT_FRAMES
    audio_latent, rt = _audio_tail_from_latent(context_latent, int(audio_ctx))
    audio_ref = {"kind": "audio", "ref_audio_t": rt, "audio_latent": audio_latent}

    # 合并 keyframes：保留已有非 0 锚（如 fl2v 尾帧），丢弃 0 处首帧锚
    merged = list(ctx_keyframes)
    out: list = []
    for emb, extra in positive:
        d = extra.copy()
        prior = d.get("minimax_keyframes") or []
        kept = []
        for kf in prior:
            p = int(kf.get("resolved_frame_index", 0))
            if p >= frame_count:
                raise ValueError(
                    f"段间引导: conditioning 携带的锚点位于帧 {p}，但本片段只有 "
                    f"{frame_count} 帧（conditioning 与 latent 应来自同一节点）"
                )
            if p != 0:
                kept.append(dict(kf))
        d["minimax_keyframes"] = kept + merged
        out.append([emb, d])

    out = node_helpers.conditioning_set_values(out, {"minimax_refs": [audio_ref]}, append=True)

    trim = covered
    log.info(
        "段间引导: 钉入 %d 帧（%d latent steps @ %dx%d），音频 %d 步，解码后裁 %d 帧",
        n, len(blocks_fitted), width, height, rt, trim,
    )
    return out, trim


def generation_frame_budget(visible_frames: int, context_frames: int) -> tuple[int, int]:
    """采样长度预算：(采样帧数, 解码后要裁的上下文帧数)。

    采样 = align(visible + context)，裁掉 context 帧后正好导出 visible 帧。
    """
    visible = align_frame_count(max(5, int(visible_frames)))
    ctx = snap_context_frames(context_frames) if context_frames else 0
    if ctx <= 0:
        return visible, 0
    sample = align_frame_count(visible + ctx)
    if ctx >= sample:
        raise ValueError(f"段间引导: 上下文 {ctx} 帧必须小于采样长度 {sample} 帧")
    return sample, ctx


def trim_context_prefix(
    images: torch.Tensor,
    audio: dict | None,
    trim_frames: int,
    *,
    fps: float = FPS,
) -> tuple[torch.Tensor, dict | None]:
    """从解码结果裁掉钉入的前缀（画面与音频同步）。"""
    trim = max(0, int(trim_frames))
    if trim > 0:
        if int(images.shape[0]) <= trim:
            raise ValueError(f"段间引导: 无法从 {int(images.shape[0])} 帧解码中裁 {trim} 帧")
        images = images[trim:]
    if not isinstance(audio, dict) or audio.get("waveform") is None:
        return images, audio
    waveform = audio["waveform"]
    sr = int(audio.get("sample_rate") or 32000)
    drop = int(round((trim / float(fps)) * sr)) if trim > 0 else 0
    if drop > 0 and int(waveform.shape[-1]) > drop:
        waveform = waveform[..., drop:]
    want = int(round((int(images.shape[0]) / float(fps)) * sr))
    if int(waveform.shape[-1]) > want:
        waveform = waveform[..., :want]
    return images, {"waveform": waveform, "sample_rate": sr}
