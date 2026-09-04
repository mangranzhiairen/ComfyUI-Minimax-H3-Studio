"""MiniMax H3 创意工作台 HTTP 路由：素材列表 + 任务库 CRUD（时间线唯一数据源在 SQLite）。"""

from __future__ import annotations

import json
import logging
import os

from aiohttp import web

import folder_paths

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif", ".jfif"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v", ".gif"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

_ROUTES_REGISTERED = False


def _get_media_exts(kind: str) -> set[str]:
    kind = str(kind or "").strip().lower()
    if kind == "image":
        return IMAGE_EXTS
    if kind == "video":
        return VIDEO_EXTS
    if kind == "audio":
        return AUDIO_EXTS
    if kind == "reference_audio":
        # 参考音频槽：音频或视频（视频可提取音轨）
        return AUDIO_EXTS | VIDEO_EXTS
    raise ValueError("kind 必须是 image / video / audio / reference_audio")


def _peek_image_size(path: str) -> tuple[int, int]:
    """读取图片宽高（不解码像素）。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            return int(im.size[0] or 0), int(im.size[1] or 0)
    except Exception:  # noqa: BLE001
        return 0, 0


def _list_input_media(kind: str) -> list[dict]:
    """遍历 ComfyUI/input/ 目录，按扩展名过滤返回素材列表。"""
    input_dir = folder_paths.get_input_directory()
    exts = _get_media_exts(kind)
    items: list[dict] = []
    for root, dirs, files in os.walk(input_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in exts:
                continue
            abs_path = os.path.join(root, name)
            try:
                stat = os.stat(abs_path)
            except OSError:
                continue
            try:
                rel_path = os.path.relpath(abs_path, input_dir).replace("\\", "/")
            except ValueError:
                continue
            if rel_path.startswith(".."):
                continue
            subfolder = os.path.dirname(rel_path).replace("\\", "/")
            if subfolder == ".":
                subfolder = ""
            width, height = (0, 0)
            if ext in IMAGE_EXTS:
                width, height = _peek_image_size(abs_path)
            items.append(
                {
                    "name": name,
                    "relPath": rel_path,
                    "subfolder": subfolder,
                    "type": "input",
                    "modified": float(stat.st_mtime),
                    "width": width,
                    "height": height,
                    "mediaKind": "video" if ext in VIDEO_EXTS else (
                        "audio" if ext in AUDIO_EXTS else "image"
                    ),
                }
            )
    # 最近修改的排前面
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items


async def list_input_media(request: web.Request) -> web.Response:
    """GET /minimax/studio/list_input_media?kind=image|video|audio|reference_audio"""
    try:
        kind = str(request.query.get("kind") or "").strip().lower()
        if not kind:
            return web.Response(status=400, text="缺少 kind 参数")
        items = _list_input_media(kind)
    except ValueError as exc:
        return web.Response(status=400, text=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("list input media 失败: %s", exc)
        return web.Response(status=500, text=str(exc))
    return web.json_response({"items": items})


# ---------- 任务库 CRUD（时间线唯一数据源在 SQLite） ----------

# 插件版本（与 web/package.json 的 version 同步维护）。
# 前端构建版本自检用：后端版本 ≠ 前端构建版本 → 浏览器缓存了旧版 minimax-h3-studio.js，
# 需强制刷新/清缓存，否则旧 JS 会把完整空 payload 写进工作流 json（数据丢失事故）。
PLUGIN_VERSION = "0.1.0"


async def get_plugin_version(request: web.Request) -> web.Response:
    """GET /minimax/studio/version → {version}（前端 nodeCreated 自检缓存过期用）"""
    return web.json_response({"version": PLUGIN_VERSION})


async def create_task(request: web.Request) -> web.Response:
    """POST /minimax/studio/tasks  body: {node_id, timeline?, name?} → {task_id}

    name 为空时兜底为「新任务」（DB 中 name 永不为空；前端创建必须弹窗强制输入）。
    timeline：时间线当前数据（canvas + clips[]，含每片段草稿）。
    """
    try:
        body = await request.json()
        node_id = str(body.get("node_id") or "default")
        timeline = body.get("timeline") or ""
        name = str(body.get("name") or "").strip() or "新任务"
        from .segment_cache import create_task as db_create

        tid = db_create(node_id, timeline, {}, status="created", name=name)
        return web.json_response({"task_id": tid})
    except Exception as exc:  # noqa: BLE001
        log.warning("创建任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def list_tasks(request: web.Request) -> web.Response:
    """GET /minimax/studio/tasks?node_id=xxx → {tasks: [...]}（含段数摘要）"""
    try:
        from .segment_cache import get_clip_history as db_history
        from .segment_cache import list_tasks as db_list

        node_id = request.query.get("node_id") or None
        tasks = db_list(node_id=node_id, limit=200)
        out = []
        for t in tasks:
            try:
                timeline = json.loads(t.get("timeline") or "{}")
                seg_count = len(timeline.get("clips") or [])
            except Exception:  # noqa: BLE001
                seg_count = 0
            try:
                hist = db_history(t["task_id"])
                sampled_count = sum(len(h["samples"]) for h in hist.values())
            except Exception:  # noqa: BLE001
                sampled_count = 0
            out.append(
                {
                    "task_id": t["task_id"],
                    "node_id": t.get("node_id", ""),
                    "name": t.get("name") or "",
                    "status": t.get("status", ""),
                    "created_at": t.get("created_at", 0),
                    "updated_at": t.get("updated_at", 0),
                    "segment_count": seg_count,
                    "sampled_count": sampled_count,
                }
            )
        return web.json_response({"tasks": out})
    except Exception as exc:  # noqa: BLE001
        log.warning("任务列表失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def get_task(request: web.Request) -> web.Response:
    """GET /minimax/studio/tasks/{task_id} → {task}（timeline/sampling/状态）"""
    try:
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        task = db_get(tid)
        if not task:
            return web.Response(status=404, text=f"任务不存在: {tid}")
        return web.json_response(
            {
                "task_id": task["task_id"],
                "node_id": task.get("node_id", ""),
                "name": task.get("name") or "",
                "timeline": task.get("timeline", ""),
                "sampling_json": task.get("sampling_json", ""),
                "status": task.get("status", ""),
                "created_at": task.get("created_at", 0),
                "updated_at": task.get("updated_at", 0),
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("读取任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def save_task_timeline(request: web.Request) -> web.Response:
    """PUT /minimax/studio/tasks/{task_id}/timeline  body: {timeline} → {ok}"""
    try:
        from .segment_cache import get_task as db_get
        from .segment_cache import update_task_timeline as db_save

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        body = await request.json()
        timeline = body.get("timeline")
        if not isinstance(timeline, str):
            timeline = json.dumps(timeline or {}, ensure_ascii=False)
        db_save(tid, timeline)
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("保存任务时间线失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def rename_task(request: web.Request) -> web.Response:
    """PUT /minimax/studio/tasks/{task_id}/name  body: {name} → {ok}"""
    try:
        from .segment_cache import get_task as db_get
        from .segment_cache import update_task_name as db_rename

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        body = await request.json()
        name = str(body.get("name") or "").strip()
        db_rename(tid, name)
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("重命名任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def delete_task(request: web.Request) -> web.Response:
    """DELETE /minimax/studio/tasks/{task_id} → {ok}（连带删除 latent 缓存文件）"""
    try:
        from .segment_cache import delete_task as db_delete

        tid = request.match_info.get("task_id", "")
        db_delete(tid)
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("删除任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def get_clip_history(request: web.Request) -> web.Response:
    """GET /minimax/studio/tasks/{task_id}/history → {history: {clip_id: {…}}}

    每片段（按 clip_id 身份）的提示词条目（画面语义快照）+ 采样记录
    （含文件存在性、采样画布 canvas——前端据此判可用性，画布已变更的样本折叠为失效）。
    """
    try:
        from .segment_cache import get_clip_history as db_history
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        return web.json_response({"history": db_history(tid)})
    except Exception as exc:  # noqa: BLE001
        log.warning("读取片段历史失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def delete_clip(request: web.Request) -> web.Response:
    """DELETE /minimax/studio/tasks/{task_id}/clips/{clip_id} → {ok}

    删除片段（按身份 clip_id）全部历史：版本 + 采样记录 + latent 文件。
    用户确认语义：删除片段 = 连其所有采样缓存彻底删除（不可逆）。
    """
    try:
        from .segment_cache import delete_clip_history as db_delete_clip
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        clip_id = request.match_info.get("clip_id", "")
        db_delete_clip(tid, clip_id)
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("删除片段历史失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def delete_version_sample(request: web.Request) -> web.Response:
    """DELETE /minimax/studio/tasks/{task_id}/clips/{clip_id}/samples/{sample_fp} → {ok}

    删除单个采样样本（某次抽卡）：DB 行 +（无其他引用时）latent/preview 文件。
    """
    try:
        from .segment_cache import delete_version_sample as db_delete_sample
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        clip_id = request.match_info.get("clip_id", "")
        sample_fp = request.match_info.get("sample_fp", "")
        db_delete_sample(tid, clip_id, sample_fp)
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("删除采样样本失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def delete_clip_version(request: web.Request) -> web.Response:
    """DELETE /minimax/studio/tasks/{task_id}/clips/{clip_id}/versions/{version_id} → {ok}

    删除单个历史版本（参数状态）及其全部采样（文件按引用保护策略处理）。
    """
    try:
        from .segment_cache import delete_clip_version as db_delete_version
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        clip_id = request.match_info.get("clip_id", "")
        version_id = request.match_info.get("version_id", "")
        try:
            version_id = int(version_id)
        except (TypeError, ValueError):
            return web.Response(status=400, text="version_id 必须为整数")
        db_delete_version(tid, clip_id, version_id)
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        log.warning("删除历史版本失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def export_task(request: web.Request) -> web.Response:
    """GET /minimax/studio/tasks/{task_id}/export → 可移植任务 JSON

    导出当前时间线草稿 + 采样参数 + 提示词历史（条目/采样记录元数据），
    不含素材文件与 latent/preview 缓存文件。前端直接下载为 .json。
    """
    try:
        from .segment_cache import export_task as db_export
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        data = db_export(tid)
        if data is None:
            return web.Response(status=404, text=f"任务不存在: {tid}")
        return web.json_response(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("导出任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def import_task(request: web.Request) -> web.Response:
    """POST /minimax/studio/tasks/import  body: {node_id, data} → {task_id}

    data 为导出文件 JSON（type 标记校验）。导入为新任务（时间线 + 提示词历史），
    不携带素材/latent 缓存文件；前端随后 loadTask 加载。
    """
    try:
        from .segment_cache import import_task as db_import

        body = await request.json()
        node_id = str(body.get("node_id") or "default")
        data = body.get("data")
        if not isinstance(data, dict):
            return web.Response(status=400, text="缺少 data（导出文件内容）")
        tid = db_import(data, node_id)
        return web.json_response({"task_id": tid})
    except ValueError as exc:
        return web.Response(status=400, text=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("导入任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


async def duplicate_task(request: web.Request) -> web.Response:
    """POST /minimax/studio/tasks/{task_id}/duplicate  body: {node_id?, name?} → {task_id}

    复制当前任务为新任务（DB 深拷贝）：独立 tasks 行（timeline 剥 sampleFp 锁定）+
    clip_versions 提示词历史逐行复制为新行；version_samples 采样记录与 latent/preview
    缓存文件一律不复制——副本不携带任何 latent 缓存信息，Queue 时按当前内容重新采样。
    """
    try:
        from .segment_cache import duplicate_task as db_duplicate
        from .segment_cache import get_task as db_get

        tid = request.match_info.get("task_id", "")
        if not db_get(tid):
            return web.Response(status=404, text=f"任务不存在: {tid}")
        body = await request.json()
        node_id = str(body.get("node_id") or "")
        name = str(body.get("name") or "")
        new_tid = db_duplicate(tid, node_id=node_id, name=name)
        return web.json_response({"task_id": str(new_tid)})
    except Exception as exc:  # noqa: BLE001
        log.warning("复制任务失败: %s", exc)
        return web.Response(status=500, text=str(exc))


def _do_register(routes, method: str, path: str, handler) -> None:
    """兼容不同版本 ComfyUI 的路由表 API（RouteTableDef 用装饰器，UrlDispatcher 用 add_route）。"""
    if hasattr(routes, "add_route"):
        routes.add_route(method, path, handler)
    elif method == "POST" and hasattr(routes, "post"):
        routes.post(path)(handler)
    elif method == "GET" and hasattr(routes, "get"):
        routes.get(path)(handler)
    elif method == "PUT" and hasattr(routes, "put"):
        routes.put(path)(handler)
    elif method == "DELETE" and hasattr(routes, "delete"):
        routes.delete(path)(handler)
    else:
        raise AttributeError("Unsupported ComfyUI route table API")


def _register_route(routes, method: str, path: str, handler) -> None:
    """注册路由，并显式注册 /api 前缀版。

    ComfyUI 的 add_routes() 会在 server 初始化时给 self.routes 里的路由自动加
    /api 前缀，但该处理早于自定义节点加载——节点注册的路由错过注入，
    前端 fetchApi（自动加 /api）会 404。因此这里两条路径都显式注册。
    """
    _do_register(routes, method, path, handler)
    _do_register(routes, method, "/api" + path, handler)


def register_routes() -> bool:
    """在 ComfyUI PromptServer 上注册创意工作台 HTTP 路由。"""
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return True

    try:
        from server import PromptServer

        server = PromptServer.instance
    except Exception as exc:  # noqa: BLE001 环境差异（如模拟加载）不应阻断插件
        log.warning("PromptServer 不可用，路由未注册: %s", exc)
        return False
    if server is None:
        log.warning("PromptServer 未就绪，HTTP 路由未注册")
        return False

    _register_route(server.routes, "GET", "/minimax/studio/list_input_media", list_input_media)
    _register_route(server.routes, "GET", "/minimax/studio/version", get_plugin_version)
    _register_route(server.routes, "POST", "/minimax/studio/tasks", create_task)
    _register_route(server.routes, "GET", "/minimax/studio/tasks", list_tasks)
    _register_route(server.routes, "GET", "/minimax/studio/tasks/{task_id}", get_task)
    _register_route(
        server.routes, "PUT", "/minimax/studio/tasks/{task_id}/timeline", save_task_timeline
    )
    _register_route(
        server.routes, "PUT", "/minimax/studio/tasks/{task_id}/name", rename_task
    )
    _register_route(server.routes, "DELETE", "/minimax/studio/tasks/{task_id}", delete_task)
    _register_route(
        server.routes,
        "GET",
        "/minimax/studio/tasks/{task_id}/history",
        get_clip_history,
    )
    _register_route(
        server.routes,
        "DELETE",
        "/minimax/studio/tasks/{task_id}/clips/{clip_id}",
        delete_clip,
    )
    _register_route(
        server.routes,
        "DELETE",
        "/minimax/studio/tasks/{task_id}/clips/{clip_id}/samples/{sample_fp}",
        delete_version_sample,
    )
    _register_route(
        server.routes,
        "DELETE",
        "/minimax/studio/tasks/{task_id}/clips/{clip_id}/versions/{version_id}",
        delete_clip_version,
    )
    _register_route(
        server.routes,
        "GET",
        "/minimax/studio/tasks/{task_id}/export",
        export_task,
    )
    _register_route(
        server.routes,
        "POST",
        "/minimax/studio/tasks/{task_id}/duplicate",
        duplicate_task,
    )
    _register_route(
        server.routes,
        "POST",
        "/minimax/studio/tasks/import",
        import_task,
    )
    _ROUTES_REGISTERED = True
    log.info("MiniMax H3 创意工作台 HTTP 路由已注册")
    return True
