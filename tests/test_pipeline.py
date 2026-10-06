"""采样流程（子图）执行层验证：摊平图校验、骨架绑定、拓扑调度、真实执行。

分两部分：
- **纯函数部分**（validate_graph / topo_order / bind_studio_inputs）不依赖 ComfyUI，任何环境都能跑
- **执行部分**需要 ComfyUI 的 execution/nodes（真实节点映射 + 官方输入解析），
  因此会把 ComfyUI 根目录加进 sys.path；导入失败时自动跳过并退出 0

运行（推荐用 ComfyUI venv）：
    & "D:\\Python\\ComfyUI\\.venv\\Scripts\\python.exe" tests/test_pipeline.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studio.payload import (
    ClipPipeline,
    PayloadValidationError,
    PipelineGraph,
    PipelineNode,
    load as load_payload,
)
from studio.pipeline import (
    PipelineError,
    PipelineRuntime,
    STUDIO_SLOTS,
    bind_studio_inputs,
    run_pipeline_graph,
    topo_order,
    validate_graph,
)

# ---------- ComfyUI 环境探测（执行部分需要） ----------

COMFY_ROOT = Path(__file__).resolve().parents[3]  # custom_nodes/<plugin>/tests → ComfyUI 根
_HAS_COMFY = False
try:
    if COMFY_ROOT.is_dir() and str(COMFY_ROOT) not in sys.path:
        sys.path.insert(0, str(COMFY_ROOT))
    import execution  # noqa: F401
    import nodes  # noqa: F401

    _HAS_COMFY = True
except Exception as _exc:  # noqa: BLE001
    print(f"[skip] 未找到可用的 ComfyUI 运行时（{COMFY_ROOT}）：{_exc}")


def graph_of(nodes: dict, output, inputs=None, warnings=()):
    return PipelineGraph(
        version=1,
        inputs=tuple(inputs or ()),
        nodes={k: PipelineNode(class_type=v[0], inputs=v[1]) for k, v in nodes.items()},
        output=output,
        warnings=tuple(warnings),
    )


def runtime_of(**values):
    return PipelineRuntime(providers={k: (lambda v=v: v) for k, v in values.items()})


class TestGraphValidation(unittest.TestCase):
    """静态校验（纯函数）。"""

    def test_missing_graph_reports_frontend(self):
        with self.assertRaises(PipelineError) as ctx:
            validate_graph(None)
        self.assertIn("摊平图", str(ctx.exception))

    def test_empty_nodes(self):
        with self.assertRaises(PipelineError) as ctx:
            validate_graph(graph_of({}, None))
        self.assertIn("没有节点", str(ctx.exception))

    def test_missing_output_slot_link(self):
        g = graph_of({"1": ("BasicGuider", {})}, None, warnings=("子图输出槽没有连线",))
        with self.assertRaises(PipelineError) as ctx:
            validate_graph(g)
        msg = str(ctx.exception)
        self.assertIn("输出槽", msg)
        self.assertIn("前端提示", msg)  # 前端摊平时的告警要一并回显

    def test_output_points_to_unknown_node(self):
        g = graph_of({"1": ("BasicGuider", {})}, ("9", 0))
        with self.assertRaises(PipelineError) as ctx:
            validate_graph(g)
        self.assertIn("不存在的节点", str(ctx.exception))

    def test_unknown_studio_slot(self):
        g = graph_of(
            {"1": ("BasicGuider", {"conditioning": ["__studio__", "negative"]})},
            ("1", 0),
        )
        with self.assertRaises(PipelineError) as ctx:
            validate_graph(g)
        self.assertIn("negative", str(ctx.exception))

    def test_legacy_index_reference(self):
        g = graph_of(
            {"1": ("BasicGuider", {"conditioning": ["__studio__", 0]})}, ("1", 0)
        )
        with self.assertRaises(PipelineError) as ctx:
            validate_graph(g)
        self.assertIn("按序号", str(ctx.exception))

    def test_bind_replaces_studio_refs(self):
        g = graph_of(
            {
                "1": ("BasicGuider", {"conditioning": ["__studio__", "conditioning"]}),
                "2": ("SamplerCustomAdvanced", {"latent_image": ["__studio__", "latent"]}),
            },
            ("2", 0),
        )
        prompt = bind_studio_inputs(g.nodes, runtime_of(conditioning="COND", latent="LAT"))
        self.assertEqual(prompt["1"]["inputs"]["conditioning"], "COND")
        self.assertEqual(prompt["2"]["inputs"]["latent_image"], "LAT")

    def test_topo_order_visits_upstream_first(self):
        g = graph_of(
            {
                "1": ("A", {}),
                "2": ("B", {"x": ["1", 0]}),
                "3": ("C", {"x": ["2", 0]}),
            },
            ("3", 0),
        )
        prompt = bind_studio_inputs(g.nodes, runtime_of())
        self.assertEqual(topo_order(prompt, "3"), ["1", "2", "3"])

    def test_topo_order_detects_cycle(self):
        g = graph_of(
            {"1": ("A", {"x": ["2", 0]}), "2": ("B", {"x": ["1", 0]})},
            ("1", 0),
        )
        prompt = bind_studio_inputs(g.nodes, runtime_of())
        with self.assertRaises(PipelineError) as ctx:
            topo_order(prompt, "1")
        self.assertIn("环", str(ctx.exception))

    def test_runtime_unknown_slot(self):
        with self.assertRaises(PipelineError) as ctx:
            runtime_of(latent="L").get("model")
        self.assertIn("model", str(ctx.exception))


class TestPayloadParsing(unittest.TestCase):
    """契约解析（纯函数）：pipeline 结构校验。"""

    def payload(self, pipeline):
        return {
            "version": 1,
            "canvas": {"fps": 24, "width": 864, "height": 480},
            "clips": [
                {
                    "id": "c1",
                    "mode": "t2v",
                    "prompt": "x",
                    "durationSec": 4,
                    "enabled": True,
                    "pipeline": pipeline,
                }
            ],
        }

    def test_no_pipeline(self):
        self.assertIsNone(load_payload(self.payload(None)).clips[0].pipeline)

    def test_pipeline_without_graph_is_allowed(self):
        p = load_payload(self.payload({"id": "sg", "name": "n"})).clips[0].pipeline
        self.assertIsInstance(p, ClipPipeline)
        self.assertIsNone(p.graph)  # 前端旧产物：执行时给可读报错，不静默降级

    def test_pipeline_graph_round_trip(self):
        p = load_payload(
            self.payload(
                {
                    "id": "sg",
                    "name": "n",
                    "graph": {
                        "version": 1,
                        "inputs": [{"name": "latent", "type": "LATENT"}],
                        "nodes": {"5": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}},
                        "output": ["5", 0],
                        "warnings": ["w"],
                    },
                }
            )
        ).clips[0].pipeline
        assert p is not None and p.graph is not None
        self.assertEqual(p.graph.nodes["5"].class_type, "KSamplerSelect")
        self.assertEqual(p.graph.nodes["5"].inputs["sampler_name"], "res_multistep")
        self.assertEqual(p.graph.output, ("5", 0))
        self.assertEqual(p.graph.warnings, ("w",))

    def test_bad_graph_version(self):
        with self.assertRaises(PayloadValidationError) as ctx:
            load_payload(self.payload({"id": "sg", "graph": {"version": 2, "nodes": {}}}))
        self.assertIn("版本", str(ctx.exception))

    def test_bad_nodes_shape(self):
        with self.assertRaises(PayloadValidationError):
            load_payload(self.payload({"id": "sg", "graph": {"version": 1, "nodes": []}}))

    def test_bad_output_shape(self):
        with self.assertRaises(PayloadValidationError):
            load_payload(
                self.payload(
                    {"id": "sg", "graph": {"version": 1, "nodes": {}, "output": ["1"]}}
                )
            )


# ---------- 真实执行（需要 ComfyUI 运行时） ----------


class _FakeNode:
    """测试用纯计算节点（V1 风格，走官方 get_input_data 映射）。"""

    CATEGORY = "studio/test"
    FUNCTION = "run"

    def __init__(self, **opts):
        pass


@unittest.skipUnless(_HAS_COMFY, "需要 ComfyUI 运行时")
def run_graph_sync(*args, **kwargs):
    """同步驱动 async 的 run_pipeline_graph（真机里它由 Studio 的 async 节点 await）。"""
    import asyncio

    return asyncio.run(run_pipeline_graph(*args, **kwargs))


class TestExecution(unittest.TestCase):
    def setUp(self):
        import nodes

        self.nodes_mod = nodes

        class Sum(_FakeNode):
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"a": ("INT", {"default": 0}), "b": ("INT", {"default": 0})}}

            RETURN_TYPES = ("INT",)
            RETURN_NAMES = ("sum",)

            def run(self, a, b):
                return (a + b,)

        class Double(_FakeNode):
            @classmethod
            def INPUT_TYPES(cls):
                return {"required": {"value": ("INT", {"default": 0})}}

            RETURN_TYPES = ("INT",)
            RETURN_NAMES = ("value",)

            def run(self, value):
                return (value * 2,)

        self.Sum = Sum
        self.Double = Double
        nodes.NODE_CLASS_MAPPINGS["StudioTestSum"] = Sum
        nodes.NODE_CLASS_MAPPINGS["StudioTestDouble"] = Double

        # 真实官方节点：comfy_extras 的注册发生在 ComfyUI 启动时（init_extra_nodes），
        # 单跑测试要手工登记，等价于运行时环境里的状态
        from comfy_extras.nodes_custom_sampler import KSamplerSelect, RandomNoise

        self._extra = (("RandomNoise", RandomNoise), ("KSamplerSelect", KSamplerSelect))
        for name, cls in self._extra:
            nodes.NODE_CLASS_MAPPINGS[name] = cls

    def tearDown(self):
        self.nodes_mod.NODE_CLASS_MAPPINGS.pop("StudioTestSum", None)
        self.nodes_mod.NODE_CLASS_MAPPINGS.pop("StudioTestDouble", None)
        for name, _cls in getattr(self, "_extra", ()):
            self.nodes_mod.NODE_CLASS_MAPPINGS.pop(name, None)

    def test_end_to_end_boundary_links_and_literals(self):
        """骨架输入 + 节点间连线 + widget 字面量 + 输出槽脱壳。"""
        g = graph_of(
            {
                "n1": ("StudioTestSum", {"a": ["__studio__", "latent"], "b": 5}),
                "n2": ("StudioTestDouble", {"value": ["n1", 0]}),
            },
            ("n2", 0),
        )
        out = run_graph_sync(g, runtime_of(latent=10))
        self.assertEqual(out, 30)  # (10 + 5) * 2

    def test_real_comfy_nodes(self):
        """真实官方节点（RandomNoise / KSamplerSelect，无需模型）跑通官方输入映射。"""
        g = graph_of(
            {
                "noise": ("RandomNoise", {"noise_seed": 123}),
                "sampler": ("KSamplerSelect", {"sampler_name": "res_multistep"}),
            },
            ("noise", 0),
        )
        out = run_graph_sync(g, runtime_of())
        self.assertTrue(hasattr(out, "generate_noise"), f"NOISE 对象异常：{type(out)}")

    def test_unknown_node_type(self):
        g = graph_of({"1": ("NoSuchNodeType_xyz", {})}, ("1", 0))
        with self.assertRaises(PipelineError) as ctx:
            run_graph_sync(g, runtime_of())
        self.assertIn("NoSuchNodeType_xyz", str(ctx.exception))

    def test_output_slot_out_of_range(self):
        g = graph_of({"n1": ("StudioTestSum", {"a": 1, "b": 1})}, ("n1", 3))
        with self.assertRaises(PipelineError) as ctx:
            run_graph_sync(g, runtime_of())
        self.assertIn("输出槽", str(ctx.exception))

    def test_studio_slot_providers_are_lazy(self):
        """没被用到的骨架槽不该被求值（模型偏移/sigma 只有需要时才算）。"""
        called: list[str] = []
        rt = PipelineRuntime(
            providers={
                "latent": lambda: 7,
                "model": lambda: called.append("model") or "M",
            }
        )
        g = graph_of(
            {"n1": ("StudioTestDouble", {"value": ["__studio__", "latent"]})}, ("n1", 0)
        )
        self.assertEqual(run_graph_sync(g, rt), 14)
        self.assertEqual(called, [])


class TestStudioSlotsContract(unittest.TestCase):
    def test_slot_names_match_frontend_skeleton(self):
        """骨架槽名单要与前端 SKELETON_INPUTS 对齐（改前端记得同步这里）。"""
        self.assertEqual(
            list(STUDIO_SLOTS),
            ["conditioning", "latent", "noise", "model", "sampler", "scheduler"],
        )


class TestSamplerProgressWrapper(unittest.TestCase):
    """逐步回调包装器（挂在 model 上，链上每段采样都触发）——纯 Python，无需 GPU。"""

    def _run(self, total_steps=4, official_callback=None, latent_shapes=None, x0_maker=None):
        from studio.sampling import SamplerProgressWrapper

        seen: list[tuple] = []
        wrapper = SamplerProgressWrapper(lambda step, x0, x, total: seen.append((step, x0, total)))

        calls: dict = {}

        def executor(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed, latent_shapes=None):
            calls["callback"] = callback
            calls["latent_shapes"] = latent_shapes
            for step in range(total_steps):
                x0 = x0_maker(step) if x0_maker else f"x0-{step}"
                callback(step, x0, "x", total_steps)
            return "samples"

        out = wrapper(
            executor,
            "noise",
            "latent",
            "sampler",
            object(),
            None,
            official_callback,
            False,
            123,
            latent_shapes=latent_shapes,
        )
        return out, seen, calls

    def test_forwards_every_step_to_studio_and_official_callback(self):
        official: list[tuple] = []
        out, seen, calls = self._run(
            4,
            lambda step, x0, x, total: official.append((step, x0, total)),
            latent_shapes=("shape",),
        )
        self.assertEqual(out, "samples")
        self.assertEqual([s[0] for s in seen], [0, 1, 2, 3])  # 每步都转给 studio
        self.assertEqual(seen[0][1], "x0-0")  # x0 原样透传（live 预览用）
        self.assertEqual(seen[0][2], 4)  # total_steps 透传（步数显示用）
        self.assertEqual([s[0] for s in official], [0, 1, 2, 3])  # 官方预览链不受影响
        self.assertEqual(calls["latent_shapes"], ("shape",))  # 关键字参数原样透传

    def test_studio_callback_failure_does_not_break_sampling(self):
        from studio.sampling import SamplerProgressWrapper

        official: list[int] = []

        def boom(step, x0, x, total):
            raise RuntimeError("预览解码炸了")

        wrapper = SamplerProgressWrapper(boom)

        def executor(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed, latent_shapes=None):
            callback(0, "x0", "x", 1)
            return "ok"

        out = wrapper(
            executor,
            None, None, None, None, None,
            lambda step, x0, x, total: official.append(step),
            False, None, latent_shapes=None,
        )
        self.assertEqual(out, "ok")  # 采样照常完成
        self.assertEqual(official, [0])  # 官方回调仍然被调用

    def test_av_packed_x0_is_restored_to_nested_view(self):
        """AV 嵌套 latent：包装器在 sampled 打包内层，拿到的 x0 是扁平张量，必须还原成嵌套视图。

        否则 studio 的 TAE 预览 `video_stream(x0)` 拿到非 5D 张量 → 静默不出预览
        （进度条却正常，正是这次的现象）。
        """
        import torch

        from studio.sampling import SamplerProgressWrapper

        # video: (1, 4, 2, 3, 3) / audio: (1, 2, 2) → 打包后扁平 (1, 1, video+audio 元素数)
        video = torch.zeros(1, 4, 2, 3, 3)
        audio = torch.zeros(1, 2, 2)
        packed, shapes = None, [tuple(video.shape), tuple(audio.shape)]
        import comfy.utils

        packed, _ = comfy.utils.pack_latents([video, audio])

        seen: list = []
        wrapper = SamplerProgressWrapper(lambda step, x0, x, total: seen.append(x0))

        def executor(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed, latent_shapes=None):
            callback(0, packed, packed, 1)
            return "ok"

        wrapper(executor, None, None, None, None, None, None, False, None, latent_shapes=shapes)
        self.assertEqual(len(seen), 1)
        got = seen[0]
        self.assertTrue(hasattr(got, "tensors"), f"应为嵌套视图，实际 {type(got)}")
        self.assertEqual(len(got.tensors), 2)
        self.assertEqual(tuple(got.tensors[0].shape), tuple(video.shape))  # video 流恢复成 5D
        self.assertEqual(tuple(got.tensors[1].shape), tuple(audio.shape))

    def test_single_stream_x0_untouched(self):
        """单流（video-only / 图像）latent：不做还原，原样透传。"""
        import torch

        from studio.sampling import SamplerProgressWrapper

        t = torch.zeros(1, 4, 2, 3, 3)
        seen: list = []
        wrapper = SamplerProgressWrapper(lambda step, x0, x, total: seen.append(x0))

        def executor(noise, latent_image, sampler, sigmas, denoise_mask, callback, disable_pbar, seed, latent_shapes=None):
            callback(0, t, t, 1)
            return "ok"

        wrapper(executor, None, None, None, None, None, None, False, None,
                latent_shapes=[tuple(t.shape)])
        self.assertIs(seen[0], t)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
