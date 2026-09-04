"""纯函数层验证：契约解析/校验、Task 注入与条件构建、Motion Context 网格、缓存工具。

运行（无需 ComfyUI 环境）：
    python tests/test_studio.py
"""

from __future__ import annotations

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
        }
        out = _strip_sample_locks(timeline)
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


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
