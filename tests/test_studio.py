"""纯函数层验证：契约解析/校验、Task 注入与条件构建、Motion Context 网格、任务库与缓存工具。

运行（无需 ComfyUI 环境）：
    python tests/test_studio.py
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio.payload import CanvasConfig, PayloadValidationError, load as load_payload
from studio.tasks import SamplingConfig, TaskContext, create_task
from studio.tasks.base import MAX_REF_AUDIOS, MAX_REF_IMAGES, _check_ref_limits


def frontend_sample_payload() -> dict:
    """模拟前端 store.serialize() 的完整输出（全模式 6 段）。"""
    return {
        "version": 1,
        "canvas": {"fps": 24, "width": 864, "height": 480},
        "clips": [
            {
                "id": "seg_1",
                "mode": "t2v",
                "prompt": "清晨的森林，薄雾中一缕阳光穿过树梢",
                "durationSec": 5.0,
                "enabled": True,
            },
            {
                "id": "seg_2",
                "mode": "i2v",
                "prompt": "镜头推向溪流，水面波光粼粼",
                "durationSec": 4.0,
                "enabled": True,
                "firstFrame": {"path": "溪流.png", "kind": "image"},
            },
            {
                "id": "seg_3",
                "mode": "fl2v",
                "prompt": "仰拍瀑布，水花飞溅",
                "durationSec": 6.0,
                "enabled": True,
                "firstFrame": {"path": "瀑布起点.png", "kind": "image"},
                "lastFrame": {"path": "瀑布终点.png", "kind": "image"},
            },
            {
                "id": "seg_4",
                "mode": "r2v",
                "prompt": "村庄黄昏，人物保持 <Picture 1> 外观",
                "durationSec": 5.5,
                "enabled": True,
                "refImages": [{"path": "角色.png", "kind": "image"}],
                "refAudios": [{"path": "ambient_loop.wav", "kind": "audio"}],
            },
            {
                "id": "seg_5",
                "mode": "v2v",
                "prompt": "赛博朋克风格改造 <Video 1>",
                "durationSec": 4.5,
                "enabled": False,  # 选择运行：跳过
                "sourceVideo": {"path": "city_timelapse.mp4", "kind": "video"},
            },
            {
                "id": "seg_6",
                "mode": "rv2v",
                "prompt": "<Video 1> 加上 <Picture 1> 的角色",
                "durationSec": 3.5,
                "enabled": True,
                "sourceVideo": {"path": "base_clip.mp4", "kind": "video"},
                "refImages": [{"path": "角色2.png", "kind": "image"}],
            },
        ],
        "totalDurationSec": 28.5,
    }


def make_ctx() -> TaskContext:
    return TaskContext(
        canvas=CanvasConfig(fps=24, width=864, height=480),
        sampling=SamplingConfig(seed=42, steps=25),
    )


class PayloadParseTest(unittest.TestCase):
    def test_full_payload_roundtrip(self):
        """前端 JSON → 反序列化，段数/画布/总时长正确。"""
        payload = load_payload(frontend_sample_payload())
        self.assertEqual(len(payload.clips), 6)
        self.assertEqual(payload.canvas.width, 864)
        self.assertAlmostEqual(payload.total_duration_sec, 28.5)

    def test_version_mismatch(self):
        payload = frontend_sample_payload()
        payload["version"] = 999
        with self.assertRaises(PayloadValidationError):
            load_payload(payload)

    def test_unknown_mode_rejected(self):
        payload = frontend_sample_payload()
        payload["clips"][0]["mode"] = "unknown_mode"
        with self.assertRaises(PayloadValidationError):
            load_payload(payload)

    def test_sample_fp_parsed(self):
        """sampleFp：合法 16 位 hex 解析到段对象（可选，默认 None）。"""
        payload = frontend_sample_payload()
        self.assertIsNone(load_payload(payload).clips[0].sample_fp)
        payload["clips"][0]["sampleFp"] = "abcdef0123456789"
        seg = load_payload(payload).clips[0]
        self.assertEqual(seg.sample_fp, "abcdef0123456789")

    def test_sample_fp_invalid_rejected(self):
        """sampleFp：非 16 位 hex 拒绝（防止脏数据进执行流）。"""
        for bad in ("zzzz", "1234567890abcdefg", ""):
            payload = frontend_sample_payload()
            payload["clips"][0]["sampleFp"] = bad
            if bad:
                with self.assertRaises(PayloadValidationError):
                    load_payload(payload)
            else:
                # 空串视为未指定
                self.assertIsNone(load_payload(payload).clips[0].sample_fp)

    def test_clip_to_snapshot(self):
        """提示词条目快照：纯画面语义（不含时长——时长随样本），执行态与采样指纹不进快照。"""
        from studio.payload import clip_to_snapshot

        payload = load_payload(frontend_sample_payload())
        snap = clip_to_snapshot(payload.clips[3])  # r2v 段
        self.assertEqual(snap["mode"], "r2v")
        self.assertEqual(snap["refImages"][0]["path"], "角色.png")
        self.assertEqual(snap["refAudios"][0]["path"], "ambient_loop.wav")
        # 画面语义快照：执行态/规格/采样指纹不是内容，不进历史条目
        self.assertNotIn("sampleFp", snap)
        self.assertNotIn("enabled", snap)
        self.assertNotIn("continuity", snap)
        self.assertNotIn("durationSec", snap)

    def test_continuity_parsed(self):
        """continuity：片段级续接开关，默认 False，随契约解析。"""
        payload = frontend_sample_payload()
        self.assertFalse(load_payload(payload).clips[0].continuity)
        payload["clips"][0]["continuity"] = True
        self.assertTrue(load_payload(payload).clips[0].continuity)


class TaskInjectionTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.payload = load_payload(frontend_sample_payload())

    def test_node_mapping(self):
        """各模式注入正确 Task 子类，条件构建映射到官方节点。"""
        nodes = {}
        for seg in self.payload.clips:
            task = create_task(seg, self.ctx)
            nodes[seg.mode] = task.build_conditioning().node
        self.assertEqual(nodes["t2v"], "MiniMaxH3ImageToVideo")
        self.assertEqual(nodes["i2v"], "MiniMaxH3ImageToVideo")
        self.assertEqual(nodes["fl2v"], "MiniMaxH3ImageToVideo")
        self.assertEqual(nodes["r2v"], "MiniMaxH3ReferenceToVideo")
        self.assertEqual(nodes["rv2v"], "MiniMaxH3ReferenceToVideo")

    def test_frame_grid_alignment(self):
        """帧网格：5s@24fps=120 → 对齐 17k+5 → 124；6s → 158。"""
        by_mode = {seg.mode: seg for seg in self.payload.clips}
        t2v = create_task(by_mode["t2v"], self.ctx).build_conditioning()
        self.assertEqual(t2v.length, 124)
        fl2v = create_task(by_mode["fl2v"], self.ctx).build_conditioning()
        self.assertEqual(fl2v.length, 158)

    def test_material_mapping(self):
        """素材路径映射到官方 ref 键。"""
        by_mode = {seg.mode: seg for seg in self.payload.clips}
        rv2v = create_task(by_mode["rv2v"], self.ctx).build_conditioning()
        self.assertEqual(rv2v.ref_videos, {"ref_video_0": "base_clip.mp4"})
        self.assertEqual(rv2v.ref_images, {"ref_image_0": "角色2.png"})

    def test_i2v_validate_missing_first_frame(self):
        """i2v 缺首帧图：validate 拦截。"""
        seg = self.payload.clips[1]
        seg.first_frame = None
        task = create_task(seg, self.ctx)
        with self.assertRaises(ValueError):
            task.validate()


class RefLimitTest(unittest.TestCase):
    def test_within_limits_ok(self):
        _check_ref_limits(
            self,  # 仅用 .id 属性
            {f"k{i}": "p" for i in range(MAX_REF_IMAGES)},
            None,
            {f"k{i}": "p" for i in range(MAX_REF_AUDIOS)},
        )  # 不抛

    def test_over_image_limit(self):
        with self.assertRaises(ValueError):
            _check_ref_limits(self, {f"k{i}": "p" for i in range(MAX_REF_IMAGES + 1)}, None, None)

    def test_over_audio_limit(self):
        with self.assertRaises(ValueError):
            _check_ref_limits(self, None, None, {f"k{i}": "p" for i in range(MAX_REF_AUDIOS + 1)})


class MotionContextGridTest(unittest.TestCase):
    def test_pixel_frames_per_latent_t(self):
        from studio.motion_context import pixel_frames_for_latent_t

        # 5 步周期 (1,4,4,4,4)：5 步=17 帧，10 步=34 帧
        self.assertEqual(pixel_frames_for_latent_t(5), 17)
        self.assertEqual(pixel_frames_for_latent_t(10), 34)

    def test_steps_for_frames(self):
        from studio.motion_context import steps_for_frames

        self.assertEqual(steps_for_frames(22), 7)  # 22 帧 = 7 步
        self.assertIsNone(steps_for_frames(21))  # 非整步

    def test_generation_frame_budget(self):
        from studio.motion_context import generation_frame_budget

        # 124 可见 + 22 上下文 → 采样 146 对齐 → 158？ 124+22=146 → align → 158? 146%17=10 → 158
        sample, trim = generation_frame_budget(124, 22)
        self.assertEqual(trim, 22)
        self.assertGreaterEqual(sample, 124 + 22)

    def test_snap_context_frames(self):
        from studio.motion_context import snap_context_frames

        self.assertEqual(snap_context_frames(22), 22)
        self.assertEqual(snap_context_frames(None), 22)
        self.assertEqual(snap_context_frames(20), 22)


class ContextConformTest(unittest.TestCase):
    """通用 context 空间整形：任意来源分辨率 → 目标网格（时间轴/音频不动）。"""

    def setUp(self):
        import types

        if "node_helpers" not in sys.modules:  # 纯函数测试环境没有 ComfyUI 根目录
            stub = types.ModuleType("node_helpers")

            def conditioning_set_values(cond, values, append=False):
                out = []
                for emb, d in cond:
                    d = dict(d)
                    for k, v in values.items():
                        d[k] = list(d.get(k) or []) + list(v) if append else v
                    out.append([emb, d])
                return out

            stub.conditioning_set_values = conditioning_set_values
            sys.modules["node_helpers"] = stub

    @staticmethod
    def _nested(video, audio):
        class FakeNested:
            def __init__(self, tensors):
                self.tensors = list(tensors)
                self.is_nested = True

            def unbind(self):
                return self.tensors

        return FakeNested((video, audio))

    def _latent(self, t=12, h=6, w=8, audio_t=40):
        import torch

        video = torch.arange(t * h * w, dtype=torch.float32).reshape(1, 1, t, h, w)
        video = video.expand(1, 24, t, h, w).contiguous()
        audio = torch.zeros(1, 32, 2, audio_t)
        return {"samples": self._nested(video, audio)}

    def test_resize_video_latent_spatial_only_and_mean_preserving(self):
        import torch

        from studio.context_conform import resize_video_latent

        src = self._latent(12, 6, 8)["samples"].tensors[0]
        out = resize_video_latent(src, 3, 4)
        self.assertEqual(tuple(out.shape), (1, 24, 12, 3, 4))
        # 时间轴不动：逐帧插值，没有跨时间混合（相邻帧内容仍不同）
        for t in range(12):
            self.assertFalse(torch.equal(out[0, 0, t], out[0, 0, (t + 1) % 12]))
        # 均值保持：每帧/每通道缩放后均值 = 源均值
        src_mean = src.float().mean(dim=(-2, -1))
        out_mean = out.float().mean(dim=(-2, -1))
        self.assertTrue(torch.allclose(src_mean, out_mean, atol=1e-5))

    def test_conform_is_noop_when_matching(self):
        from studio.context_conform import conform_context_latent, video_hw

        latent = self._latent(12, 6, 8)
        self.assertIs(conform_context_latent(latent, target_video_hw=(6, 8)), latent)
        self.assertEqual(video_hw(latent), (6, 8))

    def test_conform_preserves_audio_and_metadata(self):
        import torch

        from studio.context_conform import conform_context_latent

        latent = self._latent(12, 6, 8)
        latent["foo"] = 7
        audio_before = latent["samples"].tensors[1].clone()
        out = conform_context_latent(latent, target_video_hw=(3, 4))
        self.assertEqual(out["foo"], 7)
        self.assertEqual(tuple(out["samples"].unbind()[0].shape), (1, 24, 12, 3, 4))
        self.assertTrue(torch.equal(out["samples"].unbind()[1], audio_before))
        # 输入不被就地改写
        self.assertEqual(tuple(latent["samples"].tensors[0].shape), (1, 24, 12, 6, 8))

    def test_conform_resizes_noise_mask_video_stream(self):
        import torch

        from studio.context_conform import conform_context_latent

        latent = self._latent(12, 6, 8)
        latent["noise_mask"] = self._nested(
            torch.ones(1, 1, 12, 6, 8), torch.ones(1, 1, 1, 40)
        )
        out = conform_context_latent(latent, target_video_hw=(3, 4))
        mask = out["noise_mask"].unbind()
        self.assertEqual(tuple(mask[0].shape), (1, 1, 12, 3, 4))
        self.assertEqual(tuple(mask[1].shape), (1, 1, 1, 40))

    def test_conform_image_latent_4d(self):
        import torch

        from studio.context_conform import conform_context_latent

        image = torch.arange(4 * 6 * 8, dtype=torch.float32).reshape(1, 4, 6, 8)
        out = conform_context_latent({"samples": image}, target_video_hw=(3, 4))
        self.assertEqual(tuple(out["samples"].shape), (1, 4, 3, 4))

    def test_resize_rejects_unknown_mode(self):
        from studio.context_conform import resize_video_latent

        with self.assertRaises(ValueError):
            resize_video_latent(self._latent(12, 6, 8)["samples"].tensors[0], 3, 4, mode="bogus")

    def test_motion_context_auto_conforms_mismatched_canvas(self):
        """画布不一致时自动缩放 context 视频流，而不是报错。"""
        import torch

        from studio.motion_context import apply_motion_context

        # 当前段：高 6 / 宽 8 latent；上一段：高 12 / 宽 16（放大后的网格）
        def av(t, h, w, audio_t):
            return {
                "samples": self._nested(
                    torch.zeros(1, 24, t, h, w), torch.zeros(1, 32, 2, audio_t)
                )
            }

        latent = av(37, 6, 8, 207)  # 37 步 = 124 帧
        context = av(7, 12, 16, 40)  # 7 步 = 22 帧，且是放大后的网格
        out, trim = apply_motion_context([[None, {}]], latent, context, 22)
        self.assertEqual(trim, 22)
        kfs = out[0][1]["minimax_keyframes"]
        video_kfs = [k for k in kfs if "latent" in k]
        self.assertEqual(len(video_kfs), 7)
        # 钉入块已整形到目标网格
        self.assertEqual(tuple(video_kfs[0]["latent"].shape)[-2:], (6, 8))

    def test_motion_context_matching_canvas_skips_scaling(self):
        """前后画布一致时自动判定为「无需整形」：钉入块就是上一段尾部的原样切片。"""
        import torch

        from studio.motion_context import apply_motion_context

        def av(t, h, w, audio_t):
            video = (
                torch.arange(t * h * w, dtype=torch.float32)
                .reshape(1, 1, t, h, w)
                .expand(1, 24, t, h, w)
                .contiguous()
            )
            return {"samples": self._nested(video, torch.zeros(1, 32, 2, audio_t))}

        latent = av(37, 6, 8, 207)
        context = av(7, 6, 8, 40)  # 与本节同网格
        src_video = context["samples"].tensors[0]
        out, trim = apply_motion_context([[None, {}]], latent, context, 22)
        self.assertEqual(trim, 22)
        blocks = [k["latent"] for k in out[0][1]["minimax_keyframes"] if "latent" in k]
        self.assertEqual(len(blocks), 7)
        # 逐块等于上一段对应步的原样切片（没有被插值）
        for i, blk in enumerate(blocks):
            self.assertTrue(torch.equal(blk, src_video[:, :, i : i + 1]))

    def test_motion_context_strict_mode_still_raises(self):
        import torch

        from studio.motion_context import apply_motion_context

        def av(t, h, w):
            return {
                "samples": self._nested(
                    torch.zeros(1, 24, t, h, w), torch.zeros(1, 32, 2, 40)
                )
            }

        with self.assertRaises(ValueError):
            apply_motion_context(
                [[None, {}]], av(7, 6, 8), av(7, 12, 16), 22, conform=False
            )

    def test_motion_context_keeps_source_block_for_per_stage_fit(self):
        """钉入条目要同时带「本段网格那份」和「原始那块」。

        采样流程（子图）可以中途放大 latent（二采），第二级靠原始块重新缩放；
        原件丢了就只能从缩小版放大回去（糊）。
        """
        import torch

        from studio.motion_context import FIT_SOURCE_KEY, apply_motion_context

        def av(t, h, w):
            video = (
                torch.arange(t * h * w, dtype=torch.float32)
                .reshape(1, 1, t, h, w)
                .expand(1, 24, t, h, w)
                .contiguous()
            )
            return {"samples": self._nested(video, torch.zeros(1, 32, 2, 207))}

        latent = av(37, 6, 8)     # 本段（第一级）网格 6x8
        context = av(7, 12, 16)   # 上一段：放大后的网格 12x16
        out, trim = apply_motion_context([[None, {}]], latent, context, 22)
        self.assertEqual(trim, 22)
        kfs = [k for k in out[0][1]["minimax_keyframes"] if "latent" in k]
        self.assertEqual(len(kfs), 7)
        for kf in kfs:
            # 静态兜底那份 = 本段（第一级）网格
            self.assertEqual(tuple(kf["latent"].shape)[-2:], (6, 8))
            # 原件 = 上一段自己的网格
            self.assertEqual(tuple(kf[FIT_SOURCE_KEY].shape)[-2:], (12, 16))

    def test_fit_cond_video_latents_follows_stage_grid(self):
        """按级缩放：第一级缩到目标网格；第二级若正好是原生网格则原样（零重采样）。"""
        import torch

        from studio.motion_context import FIT_SOURCE_KEY, fit_cond_video_latents

        src = torch.arange(1 * 1 * 1 * 12 * 16, dtype=torch.float32).reshape(1, 1, 1, 12, 16)
        kf = {
            "resolved_frame_index": 0,
            "latent": torch.zeros(1, 1, 1, 6, 8),
            FIT_SOURCE_KEY: src,
        }
        kwargs = {"minimax_keyframes": [kf]}

        stage1 = fit_cond_video_latents(kwargs, [(1, 24, 37, 6, 8), (1, 32, 2, 207)])
        self.assertEqual(tuple(stage1["minimax_keyframes"][0]["latent"].shape)[-2:], (6, 8))

        stage2 = fit_cond_video_latents(kwargs, [(1, 24, 57, 12, 16), (1, 32, 2, 300)])
        got2 = stage2["minimax_keyframes"][0]["latent"]
        self.assertEqual(tuple(got2.shape)[-2:], (12, 16))
        self.assertTrue(torch.equal(got2, src))  # 原生网格：逐元素等于原件，没有二次插值
        self.assertIs(got2, src)  # 同网格时连拷贝都没有：直接原样返回原件本身
        # 没有 keyframes 的条件（例如负条件）原样返回，一点动作都没有
        untouched: dict = {}
        self.assertIs(fit_cond_video_latents(untouched, [(1, 24, 57, 6, 8)]), untouched)

        # 入参不被就地改写（每一级都从同一份出发，可重复）
        self.assertEqual(tuple(kwargs["minimax_keyframes"][0]["latent"].shape)[-2:], (6, 8))

    def test_fit_cond_video_latents_leaves_refs_alone(self):
        """参考图/参考视频用**自己的**网格（latent_h/latent_w 记账），绝不能按目标网格缩。

        缩了 latent 却不同步 latent_h/latent_w，PackedLayout 的行数就和条件行对不上
        —— 真机上报过 [3726,96] vs [3543,96]。
        """
        import torch

        from studio.motion_context import fit_cond_video_latents

        ref = {
            "kind": "image",
            "latent": torch.zeros(1, 24, 1, 10, 12),
            "latent_h": 10,
            "latent_w": 12,
        }
        kwargs = {"minimax_refs": [ref]}
        out = fit_cond_video_latents(kwargs, [(1, 24, 57, 6, 8), (1, 32, 2, 300)])
        self.assertIs(out, kwargs)  # 整份原样返回
        self.assertIs(out["minimax_refs"][0]["latent"], ref["latent"])  # 张量也没被替换


    def test_install_cond_grid_fit_patches_and_restores(self):
        """按级缩放钩子：装上后 extra_conds 前会缩；卸下后原方法回来（嵌套按层计数）。"""
        import torch

        from studio.motion_context import (
            FIT_SOURCE_KEY,
            install_cond_grid_fit,
            uninstall_cond_grid_fit,
        )

        class DummyModel:
            def extra_conds(self, **kwargs):
                return {"minimax_keyframes": kwargs.get("minimax_keyframes")}

        class DummyPatcher:
            model = DummyModel()

        original = DummyModel.extra_conds
        self.assertTrue(install_cond_grid_fit(DummyPatcher()))
        self.assertTrue(install_cond_grid_fit(DummyPatcher()))  # 嵌套一层
        try:
            self.assertIsNot(DummyModel.extra_conds, original)
            kf = {
                "resolved_frame_index": 0,
                "latent": torch.zeros(1, 1, 1, 6, 8),
                FIT_SOURCE_KEY: torch.zeros(1, 1, 1, 12, 16),
            }
            out = DummyModel().extra_conds(
                minimax_keyframes=[kf], latent_shapes=[(1, 24, 57, 12, 16), (1, 32, 2, 300)]
            )
            self.assertEqual(
                tuple(out["minimax_keyframes"][0]["latent"].shape)[-2:], (12, 16)
            )
        finally:
            uninstall_cond_grid_fit()
            self.assertIsNot(DummyModel.extra_conds, original)  # 还留着一层
            uninstall_cond_grid_fit()
        self.assertIs(DummyModel.extra_conds, original)  # 已恢复



class SegmentCacheUtilTest(unittest.TestCase):
    def test_fingerprint_stable_and_sensitive(self):
        """两级指纹：内容指纹（纯画面语义）稳定；采样指纹（latent）对工艺敏感；
        内容一变 → 内容指纹变 → 采样指纹变（卡片间零共享的指纹基础）。
        enabled/画布不进内容身份（执行态/环境不分裂历史）；画布进采样指纹（文件防覆盖）。"""
        from studio.segment_cache import content_fingerprint, sample_fingerprint

        class Seg:
            pass

        def make(prompt: str):
            seg = Seg()
            seg.mode = "t2v"
            seg.prompt = prompt
            seg.duration_sec = 5.0
            seg.enabled = True
            seg.ref_images = seg.ref_videos = seg.ref_audios = []
            seg.first_frame = seg.last_frame = seg.source_video = None
            return seg

        canvas = {"fps": 24, "width": 864, "height": 480}
        sampling = {
            "seed": 0, "cfg": 1.0, "steps": 25, "sampler": "res_multistep",
            "scheduler": "simple", "shift_video": 12.0, "shift_audio": 3.0,
        }
        canvas_label = "864x480@24"
        content_fp = content_fingerprint(make("测试"), canvas)
        fp1 = sample_fingerprint("seg_a", content_fp, sampling, continuity_enabled=False, continuity_frames=22, canvas=canvas_label)
        fp2 = sample_fingerprint("seg_a", content_fp, sampling, continuity_enabled=False, continuity_frames=22, canvas=canvas_label)
        fp3 = sample_fingerprint("seg_a", content_fp, sampling, continuity_enabled=True, continuity_frames=22, canvas=canvas_label)
        self.assertEqual(fp1, fp2)  # 同卡片同内容同画布同工艺 → 同采样指纹（条目内复用）
        self.assertNotEqual(fp1, fp3)  # continuity 变 → 采样指纹变

        content_fp2 = content_fingerprint(make("改过的提示词"), canvas)
        self.assertNotEqual(content_fp, content_fp2)  # 内容变 → 新提示词条目
        fp4 = sample_fingerprint("seg_a", content_fp2, sampling, continuity_enabled=False, continuity_frames=22, canvas=canvas_label)
        self.assertNotEqual(fp1, fp4)  # 内容变 → 采样指纹变（卡片间零共享）

        # 卡片归属隔离：同内容不同 clip_id → 不同指纹（跨任务即使内容相同也不共享缓存）
        fp5 = sample_fingerprint("seg_b", content_fp, sampling, continuity_enabled=False, continuity_frames=22, canvas=canvas_label)
        self.assertNotEqual(fp1, fp5)

        # enabled/画布是执行态/环境：不产生新条目（取消勾选参与生成、切画布不分裂历史）
        seg_on = make("测试")
        seg_on.enabled = True
        seg_off = make("测试")
        seg_off.enabled = False
        self.assertEqual(content_fingerprint(seg_on, canvas), content_fingerprint(seg_off, canvas))
        self.assertEqual(content_fingerprint(make("测试"), canvas), content_fingerprint(make("测试"), {"fps": 30, "width": 1280, "height": 720}))

        # 时长同样是规格：不分裂提示词条目（同词 4s/6s 同条目），但落在采样指纹防文件覆盖
        seg_short = make("测试")
        seg_short.duration_sec = 4.0
        seg_long = make("测试")
        seg_long.duration_sec = 6.0
        self.assertEqual(content_fingerprint(seg_short, canvas), content_fingerprint(seg_long, canvas))
        fp7 = sample_fingerprint("seg_a", content_fp, sampling, continuity_enabled=False, continuity_frames=22, canvas=canvas_label, duration_sec=4.0)
        fp8 = sample_fingerprint("seg_a", content_fp, sampling, continuity_enabled=False, continuity_frames=22, canvas=canvas_label, duration_sec=6.0)
        self.assertNotEqual(fp7, fp8)

        # 但画布差异必须落在采样指纹里（同内容跨画布不共享 latent 文件，防覆盖）
        fp6 = sample_fingerprint("seg_a", content_fp, sampling, continuity_enabled=False, continuity_frames=22, canvas="1280x720@24")
        self.assertNotEqual(fp1, fp6)

    def test_merge_audios(self):
        import torch

        from studio.segment_cache import merge_audios

        a1 = {"waveform": torch.zeros(1, 2, 100), "sample_rate": 32000}
        a2 = {"waveform": torch.ones(1, 1, 200), "sample_rate": 32000}
        m = merge_audios([a1, a2])
        self.assertEqual(tuple(m["waveform"].shape), (1, 2, 300))

    def test_strip_sample_locks(self):
        """任务导出清洗：timeline 草稿剥离 clips[].sampleFp（锁定指向本地缓存文件，
        不随导出迁移，保留会造成导入后"已锁定但文件丢失"卡住）。"""
        from studio.segment_cache import _strip_sample_locks

        timeline = {
            "version": 1,
            "canvas": {"fps": 24, "width": 864, "height": 480},
            "clips": [
                {"id": "clip_a", "enabled": True, "sampleFp": "abc123", "prompt": "x"},
                {"id": "clip_b", "enabled": False},
            ],
            # 历史遗留的内联流程定义：全局化之后时间线里不再带定义，清洗时一并丢弃
            "pipelines": [{"id": "p_old", "name": "旧流程", "def": {}}],
        }
        out = _strip_sample_locks(timeline)
        self.assertNotIn("pipelines", out)  # 定义只住全局库一处
        self.assertNotIn("sampleFp", out["clips"][0])
        self.assertEqual(out["clips"][1], {"id": "clip_b", "enabled": False})
        # 非 dict clip 行容错跳过；剥离返回新对象，不就地修改原输入
        timeline["clips"].append("bad")
        out2 = _strip_sample_locks(timeline)
        self.assertEqual(len(out2["clips"]), 3)
        self.assertEqual(out2["clips"][2], "bad")
        self.assertIn("sampleFp", timeline["clips"][0])  # 原输入未被就地改写

    def test_export_payload_shape(self):
        """导出文件顶层结构（type 标记 + 四大段），供导入校验与前端下载契约。"""
        from studio.segment_cache import EXPORT_FORMAT_VERSION, EXPORT_TYPE

        self.assertEqual(EXPORT_TYPE, "minimax-h3-studio-task")
        self.assertEqual(EXPORT_FORMAT_VERSION, 1)


class DatabaseTestBase(unittest.TestCase):
    """任务库相关用例的公共夹具：把 folder_paths 指到仓库内的独占临时目录。"""

    def setUp(self):
        import types

        # 临时库建在仓库内（系统 temp 可能被沙箱 ACL 拒绝创建子目录）；每个用例一个独占目录，
        # 避免上一条用例残留的库文件（句柄未释放时删不掉）串进下一条。
        # 不用 tempfile.TemporaryDirectory：它的 cleanup 会 chmod，在沙箱下非零退出。
        # 目录名用「进程 + 用例名」：init_db 的"已就绪"标记按路径记忆，路径不重复才不会串台
        self.root = Path(__file__).resolve().parent / f".db_test_tmp_{os.getpid()}_{self._testMethodName}"
        self.root.mkdir(parents=True, exist_ok=True)
        stub = types.ModuleType("folder_paths")
        stub.get_user_directory = lambda: str(self.root)
        stub.get_output_directory = lambda: str(self.root)
        self._prev = sys.modules.get("folder_paths")
        sys.modules["folder_paths"] = stub

        import studio.segment_cache as segment_cache

        self.sc = segment_cache

    def tearDown(self):
        if self._prev is not None:
            sys.modules["folder_paths"] = self._prev
        else:
            sys.modules.pop("folder_paths", None)
        gc.collect()  # 释放 sqlite 句柄后再删目录（Windows 上句柄会锁住文件）
        shutil.rmtree(self.root, ignore_errors=True)

    def _raw(self):
        import sqlite3

        return sqlite3.connect(self.sc.db_path())

class DatabaseLifecycleTest(DatabaseTestBase):
    """任务库生命周期：只增不减的就地升级 + 删除时的跨任务文件引用保护。

    latent/preview 文件按采样指纹命名、**同节点跨任务共享**，因此删任务/删片段
    必须先确认没有其他记录还引用它；数据库结构演进只允许 ADD，绝不删库重建。
    """

    def test_fresh_init_creates_schema_and_version(self):
        self.sc.init_db()
        with self._raw() as conn:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"tasks", "clip_versions", "version_samples"} <= tables)
        self.assertEqual(version, self.sc._SCHEMA_VERSION)

    def test_legacy_db_is_upgraded_in_place_without_data_loss(self):
        """旧结构库：缺表补表、缺列补列，**绝不删库**（早期实现是删库重建）。"""
        import sqlite3

        db = self.sc.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as conn:
            # 模拟早期结构：tasks 少列 + clip_versions 少列 + 没有 version_samples 表
            conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, timeline TEXT)")
            conn.execute(
                "INSERT INTO tasks (id, timeline) VALUES (1, ?)",
                ('{"clips": [{"id": "keep_me"}]}',),
            )
            conn.execute(
                "CREATE TABLE clip_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER)"
            )

        self.sc.init_db()

        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
            task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            ver_cols = {r[1] for r in conn.execute("PRAGMA table_info(clip_versions)")}
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertIn("keep_me", row["timeline"])  # 原有数据还在
        self.assertIn("node_id", task_cols)  # 缺列补齐
        self.assertIn("snapshot", ver_cols)
        self.assertIn("version_samples", tables)  # 缺表补齐
        self.assertEqual(row["name"], "")  # 补列带默认值，旧行可读不炸

    def test_init_db_runs_once_per_path_and_self_heals(self):
        """19 个入口都调 init_db()，但真正的初始化每进程每路径只跑一次；库文件被删掉后能自愈。"""
        calls: list[str] = []
        original = self.sc._init_db_once

        def _counted(path):
            calls.append(str(path))
            return original(path)

        self.sc._init_db_once = _counted  # type: ignore[assignment]
        try:
            for _ in range(5):
                self.sc.init_db()
            self.assertEqual(len(calls), 1)  # 后 4 次都是空转

            self.sc.db_path().unlink()  # 运行中库文件没了 → 下次调用必须重建
            self.sc.init_db()
            self.assertEqual(len(calls), 2)
            self.assertTrue(self.sc.db_path().exists())
        finally:
            self.sc._init_db_once = original  # type: ignore[assignment]
            self.sc._schema_ready.clear()

    def test_corrupt_db_is_reported_but_never_deleted(self):
        db = self.sc.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"definitely not a sqlite database")

        with self.assertRaises(RuntimeError) as ctx:
            self.sc.init_db()

        self.assertTrue(db.exists())  # 插件绝不自作主张删库
        self.assertEqual(db.read_bytes(), b"definitely not a sqlite database")
        self.assertIn("不会删除", str(ctx.exception))


class PipelineLibraryTest(DatabaseTestBase):
    """全局采样流程库：跨任务共享、覆盖式整库保存（导入按 id 并入）、导出内嵌被引用定义。"""

    def _entry(self, pid: str, name: str = "流程", payload: str = "a") -> dict:
        return {"id": pid, "name": name, "def": {"id": pid, "nodes": [{"payload": payload}]}}

    def _timeline(self, *pipeline_ids: str) -> dict:
        return {
            "version": 1,
            "canvas": {"fps": 24, "width": 864, "height": 480},
            "clips": [
                {"id": f"clip_{i}", "mode": "t2v", "pipelineId": pid}
                for i, pid in enumerate(pipeline_ids)
            ],
            "totalDurationSec": 4,
        }

    def test_save_and_list_round_trip(self):
        """整库覆盖语义：后一次提交就是全库内容；def 以对象形态读回。"""
        self.assertEqual(self.sc.save_pipeline_library([self._entry("p1", "流程一")]), 1)
        self.sc.save_pipeline_library([self._entry("p1", "流程一"), self._entry("p2", "流程二", "b")])

        items = self.sc.list_pipeline_library()
        self.assertEqual({p["id"] for p in items}, {"p1", "p2"})
        self.assertEqual(
            next(p for p in items if p["id"] == "p2")["def"]["nodes"][0]["payload"], "b"
        )

        self.sc.save_pipeline_library([self._entry("p2", "流程二", "b")])  # 只留一份
        self.assertEqual([p["id"] for p in self.sc.list_pipeline_library()], ["p2"])

    def test_invalid_entries_are_dropped(self):
        """非法条目（缺 id / 缺名字 / def 不是对象）直接丢弃，不写坏库。"""
        bad = [
            {"id": "", "name": "无 id", "def": {}},
            {"id": "no_name", "name": "  ", "def": {}},
            {"id": "no_def", "name": "无定义"},
            {"id": "bad_def", "name": "定义不是对象", "def": "x"},
            "not a dict",
        ]
        self.assertEqual(self.sc.save_pipeline_library(bad), 0)
        self.assertEqual(self.sc.list_pipeline_library(), [])

    def test_empty_overwrite_needs_confirm_clear(self):
        """空库覆盖非空库要显式确认——防"还没加载就整库提交"把用户的流程删光。"""
        self.sc.save_pipeline_library([self._entry("p1")])
        with self.assertRaises(ValueError):
            self.sc.save_pipeline_library([])
        self.assertEqual(len(self.sc.list_pipeline_library()), 1)  # 被拦下，库完好

        self.assertEqual(self.sc.save_pipeline_library([], confirm_clear=True), 0)
        self.assertEqual(self.sc.list_pipeline_library(), [])

    def test_inline_pipelines_migrated_from_legacy_timeline(self):
        """历史任务把定义内联在 timeline.pipelines → 一次性收编进全局库并从 timeline 删掉。"""
        timeline = self._timeline("p_old")
        timeline["pipelines"] = [{"id": "p_old", "name": "旧流程", "def": {"id": "p_old", "nodes": []}}]
        tid = self.sc.create_task("node1", json.dumps(timeline, ensure_ascii=False), {}, name="旧任务")

        # 模拟"升级前就存在的库"：结构版本退回 2，让 init_db 再跑一次迁移
        with self._raw() as conn:
            conn.execute("PRAGMA user_version = 2")
        self.sc._schema_ready.clear()
        self.sc.init_db()

        self.assertEqual([p["id"] for p in self.sc.list_pipeline_library()], ["p_old"])
        stored = json.loads(self.sc.get_task(tid)["timeline"])
        self.assertNotIn("pipelines", stored)  # 定义只留全局库一处
        self.assertEqual(stored["clips"][0]["pipelineId"], "p_old")  # 引用不动

        # 幂等：再跑一次不会重复收编、也不会报错
        self.sc._schema_ready.clear()
        self.sc.init_db()
        self.assertEqual(len(self.sc.list_pipeline_library()), 1)

    def test_export_embeds_only_referenced_pipelines(self):
        self.sc.save_pipeline_library([self._entry("p_keep", "用上的"), self._entry("p_unused", "没用上")])
        tid = self.sc.create_task(
            "node1", json.dumps(self._timeline("p_keep"), ensure_ascii=False), {}, name="带流程"
        )
        exported = self.sc.export_task(tid)
        self.assertEqual([p["id"] for p in exported["pipelines"]], ["p_keep"])
        self.assertNotIn("pipelines", exported["timeline"])  # 时间线里再带一份就是两处真相

    def test_import_merges_embedded_pipelines(self):
        """导入方库里没有该流程 → 按 id 并入；任务时间线只留引用。"""
        imported = {
            "type": self.sc.EXPORT_TYPE,
            "formatVersion": self.sc.EXPORT_FORMAT_VERSION,
            "name": "外来任务",
            "timeline": self._timeline("p_in"),
            "pipelines": [{"id": "p_in", "name": "外来流程", "def": {"id": "p_in", "nodes": []}}],
        }
        tid = self.sc.import_task(imported, "node1")

        self.assertEqual([p["id"] for p in self.sc.list_pipeline_library()], ["p_in"])
        stored = json.loads(self.sc.get_task(tid)["timeline"])
        self.assertNotIn("pipelines", stored)
        self.assertEqual(stored["clips"][0]["pipelineId"], "p_in")

    def test_import_keeps_local_def_on_id_conflict(self):
        """同 id 撞车：**保留导入方本地那份**（别人的同名流程不许覆盖自己的工艺）。"""
        self.sc.save_pipeline_library([self._entry("p1", "本机的", "mine")])
        imported = {
            "type": self.sc.EXPORT_TYPE,
            "formatVersion": self.sc.EXPORT_FORMAT_VERSION,
            "name": "撞车",
            "timeline": self._timeline("p1"),
            "pipelines": [{"id": "p1", "name": "外来的", "def": {"id": "p1", "nodes": [{"payload": "theirs"}]}}],
        }
        tid = self.sc.import_task(imported, "node1")

        items = self.sc.list_pipeline_library()
        self.assertEqual(len(items), 1)  # 不新增、不覆盖
        self.assertEqual(items[0]["def"]["nodes"][0]["payload"], "mine")
        self.assertEqual(items[0]["name"], "本机的")

        stored = json.loads(self.sc.get_task(tid)["timeline"])
        self.assertEqual(stored["clips"][0]["pipelineId"], "p1")  # 引用照旧

    def test_import_renames_on_name_collision(self):
        """新 id 但重名 → 自动加「（N）」后缀，不出现两条同名流程。"""
        self.sc.save_pipeline_library([self._entry("p1", "通用流程", "mine")])
        imported = {
            "type": self.sc.EXPORT_TYPE,
            "formatVersion": self.sc.EXPORT_FORMAT_VERSION,
            "name": "重名",
            "timeline": self._timeline("p2"),
            "pipelines": [{"id": "p2", "name": "通用流程", "def": {"id": "p2", "nodes": []}}],
        }
        self.sc.import_task(imported, "node1")

        names = sorted(p["name"] for p in self.sc.list_pipeline_library())
        self.assertEqual(names, ["通用流程", "通用流程（2）"])

    def test_export_import_round_trip(self):
        """导出 → 清空全局库 → 导入：流程定义完整回到全局库，引用可解析。"""
        self.sc.save_pipeline_library([self._entry("p_rt", "往返", "round")])
        tid = self.sc.create_task(
            "node1", json.dumps(self._timeline("p_rt"), ensure_ascii=False), {}, name="往返"
        )
        exported = self.sc.export_task(tid)
        self.sc.save_pipeline_library([], confirm_clear=True)
        self.assertEqual(self.sc.list_pipeline_library(), [])

        new_tid = self.sc.import_task(exported, "node1")
        items = self.sc.list_pipeline_library()
        self.assertEqual([p["id"] for p in items], ["p_rt"])
        self.assertEqual(items[0]["def"]["nodes"][0]["payload"], "round")
        stored = json.loads(self.sc.get_task(new_tid)["timeline"])
        self.assertEqual(stored["clips"][0]["pipelineId"], "p_rt")


class OfficialNodeCallTest(unittest.TestCase):
    """官方 conditioning 节点调用：跨 ComfyUI 版本签名漂移的回归测试。

    官方 execute 签名跨版本变过（vae/audio_vae 从位置参数变成 optional 关键字，
    位置挪到了 ref_image_size 之后）。位置传参在版本不匹配时会静默错位：
    width 收到 VAE 对象、height 收到 prompt 字符串 → core 里炸出
    "unsupported operand type(s) for //: 'str' and 'int'"。
    """

    def setUp(self):
        import types

        if "folder_paths" not in sys.modules:  # 纯函数测试环境没有 ComfyUI 根目录
            stub = types.ModuleType("folder_paths")
            stub.get_input_directory = lambda: str(Path.cwd())
            sys.modules["folder_paths"] = stub

        import studio.media_loader as media_loader
        import studio.sampling as sampling

        self.sampling = sampling
        self.media_loader = media_loader
        self._orig_load_nodes = sampling._load_minimax_nodes
        self._orig_images = (media_loader.load_image, media_loader.load_video, media_loader.load_audio)
        self.clip = object()
        self.video_vae = object()
        self.audio_vae = object()
        # 素材加载：不碰磁盘/ComfyUI，返回哨兵对象
        media_loader.load_image = lambda p: ("image", p)
        media_loader.load_video = lambda p: ("video", p)
        media_loader.load_audio = lambda p: ("audio", p)

    def tearDown(self):
        self.sampling._load_minimax_nodes = self._orig_load_nodes
        (
            self.media_loader.load_image,
            self.media_loader.load_video,
            self.media_loader.load_audio,
        ) = self._orig_images

    def _ctx_and_cr(self, **cr_overrides):
        from studio.tasks import ConditioningResult, TaskContext
        from studio.tasks import SamplingConfig

        ctx = TaskContext(
            canvas=CanvasConfig(fps=24, width=864, height=480),
            sampling=SamplingConfig(),
            model=object(),
            video_vae=self.video_vae,
            audio_vae=self.audio_vae,
            clip=self.clip,
        )
        cr = ConditioningResult(
            node="MiniMaxH3ReferenceToVideo",
            prompt="参考图上的角色走过街道",
            width=864,
            height=480,
            length=124,
            ref_images={"ref_image_1": "a.png"},
        )
        for key, value in cr_overrides.items():
            object.__setattr__(cr, key, value)
        return ctx, cr

    def _run_with_signature(self, build_execute):
        """用 builder 产出的 execute 签名跑一遍 run_minimax_conditioning，返回它收到的参数。"""
        received: dict = {}
        execute_fn = build_execute(received)

        FakeRefToVideo = type("FakeRefToVideo", (), {"execute": classmethod(execute_fn)})
        self.sampling._load_minimax_nodes = lambda: (None, FakeRefToVideo, None)

        ctx, cr = self._ctx_and_cr()
        self.sampling.run_minimax_conditioning(ctx, cr)
        return received

    def test_new_core_signature_keyword_call(self):
        """新版签名（vae/audio_vae 在 ref_image_size 之后作为 optional）：值必须落到位。"""

        def build(received):
            def execute(cls, clip, prompt, width, height, length, ref_image_size="match",
                        vae=None, audio_vae=None, ref_images=None, ref_videos=None,
                        ref_video_audios=None, ref_audios=None):
                # 记录**按参数名**收到的值：位置传参错位时这里立刻暴露
                received.update(locals())
                received.pop("cls", None)
                return ("positive", "latent")

            return execute

        got = self._run_with_signature(build)
        self.assertIs(got["vae"], self.video_vae)
        self.assertIs(got["audio_vae"], self.audio_vae)
        self.assertIs(got["clip"], self.clip)
        self.assertIsInstance(got["prompt"], str)   # 不能是 VAE 对象
        self.assertIsInstance(got["width"], int)    # 不能是 VAE 对象
        self.assertIsInstance(got["height"], int)   # 不能是 prompt 字符串
        self.assertEqual((got["width"], got["height"]), (864, 480))
        self.assertEqual(got["length"], 124)
        self.assertEqual(got["ref_image_size"], "match")
        self.assertEqual(got["ref_images"], {"ref_image_1": ("image", "a.png")})

    def test_old_core_signature_keyword_call(self):
        """旧版签名（vae/audio_vae 在前）：同一份调用同样必须正确。"""

        def build(received):
            def execute(cls, clip, vae, audio_vae, prompt, width, height, length,
                        ref_image_size="match", ref_images=None, ref_videos=None,
                        ref_video_audios=None, ref_audios=None):
                received.update(locals())
                received.pop("cls", None)
                return ("positive", "latent")

            return execute

        got = self._run_with_signature(build)
        self.assertIs(got["vae"], self.video_vae)
        self.assertIs(got["audio_vae"], self.audio_vae)
        self.assertIsInstance(got["prompt"], str)
        self.assertIsInstance(got["height"], int)
        self.assertEqual(got["ref_image_size"], "match")

    def test_unknown_parameter_fails_loudly(self):
        """签名对不上时报可读错误，而不是静默错位。"""

        def execute(cls, clip, prompt, width, height, length):  # 缺 vae/audio_vae 等
            return None

        self.sampling._load_minimax_nodes = lambda: (None, type(
            "FakeRefToVideo", (), {"execute": classmethod(execute)}), None)
        ctx, cr = self._ctx_and_cr()
        with self.assertRaises(RuntimeError) as cm:
            self.sampling.run_minimax_conditioning(ctx, cr)
        self.assertIn("FakeRefToVideo.execute 不接受参数", str(cm.exception))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
