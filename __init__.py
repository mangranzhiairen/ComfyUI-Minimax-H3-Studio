"""MiniMax H3 创意工作台 —— ComfyUI 自定义节点。

当前状态：
- 后端：Executor（调度）+ Task 子类（t2v/i2v/fl2v/r2v/v2v/rv2v）架构
- 前端：web/ 目录 Vue 3 时间线面板（独立预览：npm run dev）
- 数据：时间线存 SQLite 任务库，工作流 json 只存 taskId

License：GPL-3.0（Copyright (C) 2026 mangranzhiairen），见仓库根 LICENSE。
"""

from .nodes.studio_console import MiniMaxH3StudioConsole

# 前端扩展目录：web/dist 下的 minimax-h3-studio.js（Vue 时间线面板 + ComfyUI 扩展注册）
WEB_DIRECTORY = "./web/dist"

# 注册 HTTP 路由（素材列表「选已有」用）；失败时打印日志（不静默，便于排查）
from .studio.http_routes import register_routes

if not register_routes():
    print("[MiniMaxH3Studio] 警告: HTTP 路由注册失败（PromptServer 未就绪），将在节点首次执行时重试")

NODE_CLASS_MAPPINGS = {
    "MiniMaxH3StudioConsole": MiniMaxH3StudioConsole,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3StudioConsole": "MiniMax H3 创意工作台",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
