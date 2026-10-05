"""采样流程（子图）执行器 —— 前端方案 A 摊平图的运行时。

背景见 `doc/custom-sampling-plan.md`：每个片段卡片可以绑一份自己的采样流程（在 ComfyUI
原生子图编辑器里搭），前端把它摊平成 ComfyUI API 格式的平铺节点图（`clips[].pipeline.graph`）
发过来；本模块负责**执行这张平铺图**，产出一个 AV latent。

执行方式（方案文档 §6.2「做法二：复用零件，自写调度循环」）：
- 节点映射 / 输入解析 / V1·V3 分派 / 列表输出拆包 —— 复用官方
  `execution.get_input_data` 与 `execution.get_output_data`
- 拓扑排序与调度 —— 本模块自己写（几十行，比 ExecutionList 的缓存/惰性/子图机制简单得多，
  采样流程规范里也只允许「纯计算型」节点）
- 参数补全（widget→输入名、连线、hidden 输入）全部交给官方，不自研节点适配

骨架输入：studio 运行时按**名字**喂值（摊平图里写 `["__studio__", "<槽名>"]`），
当前支持 conditioning / latent / noise / model / sampler / scheduler。

错误一律抛 `PipelineError`（信息面向用户：是骨架没接、漏接线、还是缺插件），
调用方直接把它冒泡成节点执行错误即可。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, NamedTuple

from .payload import PipelineGraph

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.studio")

#: 摊平图里代表「studio 运行时提供」的伪节点 id（与前端 utils/subgraph.ts 对齐）
STUDIO_INPUT_NODE = "__studio__"

#: studio 运行时能提供的骨架输入（槽名 → 类型）：前端骨架槽就是这几个
STUDIO_SLOTS: dict[str, str] = {
    "conditioning": "CONDITIONING",
    "latent": "LATENT",
    "noise": "NOISE",
    "model": "MODEL",
    "sampler": "SAMPLER",
    "scheduler": "SIGMAS",
}


class PipelineError(RuntimeError):
    """采样流程不可执行（用户可据此修正：骨架没接好 / 漏接线 / 缺节点）。"""


class _CacheEntry(NamedTuple):
    """`execution.get_input_data` 只需上游缓存条目的 outputs 字段。"""

    outputs: list


class _OutputStore:
    """上游输出表 —— 充当 `get_input_data` 的 execution_list（只用到 get_cache）。"""

    def __init__(self, outputs: Mapping[str, list]):
        self._outputs = outputs

    def get_cache(self, from_node_id: Any, to_node_id: Any = None) -> _CacheEntry | None:
        entry = self._outputs.get(str(from_node_id))
        return None if entry is None else _CacheEntry(outputs=entry)


@dataclass
class PipelineRuntime:
    """studio 提供的骨架槽运行时值。

    惰性求值（只有真被用到的槽才会去算，比如模型偏移/sigma 序列都只在需要时才构造），
    同一次执行内按槽缓存，避免重复构造。
    """

    providers: dict[str, Callable[[], Any]]
    _values: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def get(self, name: str) -> Any:
        if name not in self.providers:
            known = " / ".join(sorted(self.providers))
            raise PipelineError(
                f"采样流程引用了 studio 提供不了的骨架输入「{name}」（可用：{known}）"
            )
        if name not in self._values:
            self._values[name] = self.providers[name]()
        return self._values[name]


# ---------- 校验与绑定 ----------


def _is_studio_ref(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and value[0] == STUDIO_INPUT_NODE
    )


def _node_class_type(node: Any) -> str:
    """节点类型（兼容契约 dataclass `PipelineNode` 与纯 dict）。"""
    if isinstance(node, Mapping):
        return str(node.get("class_type") or "")
    return str(getattr(node, "class_type", "") or "")


def _node_inputs(node: Any) -> dict[str, Any]:
    """节点的输入表（兼容契约 dataclass 与纯 dict）。"""
    if isinstance(node, Mapping):
        return dict(node.get("inputs") or {})
    return dict(getattr(node, "inputs", None) or {})


def _is_link(value: Any, known: Iterable[str]) -> bool:
    """`[节点id, 输出槽]` 连线引用（节点 id 必须在图里存在）。"""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] in known
        and isinstance(value[1], int)
    )


def _hint(graph: PipelineGraph | None) -> str:
    """把前端摊平时给出的告警附在错误后面（往往正好点出问题）。"""
    if graph is None or not graph.warnings:
        return ""
    return "；前端提示：" + "；".join(graph.warnings)


def bind_studio_inputs(
    nodes: Mapping[str, Any],
    runtime: PipelineRuntime,
    graph: PipelineGraph | None = None,
) -> dict[str, dict[str, Any]]:
    """把 `["__studio__", "<槽名>"]` 换成 studio 运行时对象。"""
    prompt: dict[str, dict[str, Any]] = {}
    for raw_id, node in nodes.items():
        node_id = str(raw_id)
        class_type = _node_class_type(node)
        if not class_type:
            raise PipelineError(f"节点 {node_id} 缺少 class_type（定义损坏，请重新保存采样流程）")
        inputs: dict[str, Any] = {}
        for name, value in _node_inputs(node).items():
            if _is_studio_ref(value):
                slot = value[1]
                if not isinstance(slot, str):
                    # 旧版前端按下标引用（[__studio__, 0]）：契约已改为按槽名
                    raise PipelineError(
                        f"节点 {node_id} 的输入 {name} 用了旧的骨架引用（按序号）；"
                        "请硬刷新前端后重新打开采样流程再保存一次"
                    )
                inputs[name] = runtime.get(slot)
            else:
                inputs[name] = value
        prompt[node_id] = {"class_type": str(class_type), "inputs": inputs}
    if graph is not None and len(prompt) != len(graph.nodes):
        raise PipelineError("采样流程节点表不一致（定义损坏，请重新保存采样流程）")
    return prompt


def topo_order(prompt: Mapping[str, Mapping[str, Any]], output_node_id: str) -> list[str]:
    """从输出节点反向做后序 DFS：返回可直接顺序执行的节点列表（并检测环/悬空连线）。"""
    known = set(prompt.keys())
    order: list[str] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in done:
            return
        if node_id in visiting:
            raise PipelineError(f"采样流程里存在环：节点 {node_id} 绕回了自己")
        if node_id not in prompt:
            raise PipelineError(f"连线指向不存在的节点 {node_id}（请重新连一次那段线）")
        visiting.add(node_id)
        for value in prompt[node_id].get("inputs", {}).values():
            if _is_link(value, known):
                visit(str(value[0]))
        visiting.discard(node_id)
        done.add(node_id)
        order.append(node_id)

    visit(str(output_node_id))
    return order


def validate_graph(graph: PipelineGraph | None) -> None:
    """执行前的静态校验（结构 + 骨架契约），失败抛 PipelineError。"""
    if graph is None:
        raise PipelineError(
            "该片段绑定了采样流程，但 payload 里没有摊平图（graph）——前端产物过旧，请硬刷新前端"
        )
    if not graph.nodes:
        raise PipelineError("采样流程里没有节点：请在子图里搭出采样链" + _hint(graph))
    if graph.output is None:
        raise PipelineError(
            "采样流程没有把结果接到子图输出槽：请把最后一个节点的 LATENT 输出连到子图输出" + _hint(graph)
        )
    out_node = str(graph.output[0])
    if out_node not in graph.nodes:
        raise PipelineError(f"采样流程的输出指向不存在的节点 {out_node}（请重新连输出槽）")
    for node_id, node in graph.nodes.items():
        for name, value in _node_inputs(node).items():
            if _is_studio_ref(value):
                slot = value[1]
                if not isinstance(slot, str):
                    raise PipelineError(
                        f"节点 {node_id} 的输入 {name} 用了旧的骨架引用（按序号）；"
                        "请硬刷新前端后重新打开采样流程再保存一次"
                    )
                if slot not in STUDIO_SLOTS:
                    raise PipelineError(
                        f"节点 {node_id} 的输入 {name} 引用了 studio 不提供的骨架输入「{slot}」"
                        f"（可用：{' / '.join(STUDIO_SLOTS)}）"
                    )


# ---------- 执行 ----------


def _class_mapping() -> Mapping[str, Any]:
    """ComfyUI 节点类表（延迟导入：无 ComfyUI 环境时给出可读错误）。"""
    try:
        import nodes  # noqa: PLC0415 运行时才有（ComfyUI 根目录在 sys.path 上）
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"无法加载 ComfyUI 节点表（{exc}）") from exc
    return nodes.NODE_CLASS_MAPPINGS


async def _run_nodes(
    prompt: Mapping[str, Mapping[str, Any]],
    order: list[str],
    *,
    prompt_id: str,
    extra_data: Mapping[str, Any] | None,
) -> dict[str, list]:
    """按拓扑序逐个执行节点（输入解析/输出拆包全走官方函数）。"""
    try:
        import execution as comfy_execution  # noqa: PLC0415
        from comfy_execution.graph import DynamicPrompt  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"无法加载 ComfyUI 执行器（{exc}）") from exc

    dynprompt = DynamicPrompt(dict(prompt))
    classes = _class_mapping()
    outputs: dict[str, list] = {}
    store = _OutputStore(outputs)
    extra = dict(extra_data or {})

    for node_id in order:
        node = prompt[node_id]
        class_type = str(node["class_type"])
        class_def = classes.get(class_type)
        if class_def is None:
            raise PipelineError(
                f"采样流程用了本机没有的节点：{class_type}（请安装对应插件，或把它从采样流程里换掉）"
            )

        input_data_all, missing_keys, v3_data = comfy_execution.get_input_data(
            node.get("inputs") or {}, class_def, node_id, store, dynprompt, extra
        )
        if missing_keys:
            raise PipelineError(
                f"节点 {node_id}（{class_type}）缺少输入：{', '.join(sorted(missing_keys))}"
                "（是不是有一段线没接上？）"
            )

        obj = class_def()
        output_data, _ui, has_subgraph, has_pending = await comfy_execution.get_output_data(
            prompt_id, node_id, obj, input_data_all, v3_data=v3_data
        )
        if has_subgraph:
            raise PipelineError(f"节点 {node_id}（{class_type}）会在执行中生成子图，采样流程暂不支持")
        if has_pending:
            raise PipelineError(f"节点 {node_id}（{class_type}）是异步节点，采样流程暂不支持")
        outputs[node_id] = output_data

    return outputs


def _run_async(coro) -> Any:
    """在同步上下文里跑 coroutine。

    Studio 节点目前是同步 `execute`（V1），但调用方将来若改成 async（在事件循环里），
    直接 `asyncio.run` 会抛 "cannot be called from a running event loop"，因此这里探测
    本线程是否有运行中的循环，有就另起线程跑（结果/异常原样带回）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 原样带回
            box["error"] = exc

    thread = threading.Thread(target=_worker, name="studio-pipeline", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def run_pipeline_graph(
    graph: PipelineGraph | None,
    runtime: PipelineRuntime,
    *,
    prompt_id: str = "studio-pipeline",
    extra_data: Mapping[str, Any] | None = None,
) -> Any:
    """执行摊平图，返回输出槽的值（AV latent）。

    `runtime` 提供骨架输入；返回值为输出槽的第一个（单元素列表自动脱壳）值。
    """
    validate_graph(graph)
    assert graph is not None  # validate_graph 已保证
    prompt = bind_studio_inputs(graph.nodes, runtime, graph)
    out_node = str(graph.output[0])  # type: ignore[index]
    out_slot = int(graph.output[1])  # type: ignore[index]

    order = topo_order(prompt, out_node)
    outputs = _run_async(
        _run_nodes(prompt, order, prompt_id=prompt_id, extra_data=extra_data)
    )

    values = outputs.get(out_node) or []
    if out_slot >= len(values):
        raise PipelineError(
            f"采样流程输出槽取不到值（节点 {out_node} 只有 {len(values)} 个输出）"
            + _hint(graph)
        )
    value = values[out_slot]
    # ComfyUI 的输出槽值是「列表（batch）」；采样流程约定输出单个 latent
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value
