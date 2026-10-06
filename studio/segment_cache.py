"""段缓存与任务持久化。

分层：
- SQLite（user 目录）存任务元数据：tasks（任务快照，timeline 存时间线当前数据）
  + clip_versions（片段版本历史）+ version_samples（采样历史）
- 张量文件（output 子目录）存 AV latent：latent_{sample_fp}.pt（采样指纹进文件名）

产品模型（Timeline/Clip/Version/Sample）：
- 时间线 Timeline = 任务片段序列（timeline 列存当前完整数据，含每片段草稿，覆盖式自动保存）
- 片段 Clip = 时间线上一个镜头（clip_id 身份持久稳定，历史跟随）
- 版本 Version = 采样固化的片段参数快照（clip_versions，不可变，同内容复用）
- 采样 Sample = 版本下的 latent 结果（version_samples，seed 与 latent 绑死）

原则：
- 指纹 hash 进文件名，加载无需比对（文件存在即命中）
- latent 拆流存 tuple（CPU），torch.load(weights_only=True) 安全加载
- 所有 IO 失败静默降级，不阻塞主流程（缓存是 best-effort）
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import torch

log = logging.getLogger("ComfyUI-MiniMaxH3-Studio.segment_cache")

_DB_FILENAME = "tasks.db"
_LATENT_SUBDIR = "minimax_h3_studio"


def _folders():
    """惰性导入 folder_paths（无 ComfyUI 环境（单测）时缓存功能自动不可用）。"""
    import folder_paths

    return folder_paths


# ---------- 路径 ----------

def db_path() -> Path:
    root = Path(_folders().get_user_directory()) / "ComfyUI-MiniMaxH3-Studio"
    root.mkdir(parents=True, exist_ok=True)
    return root / _DB_FILENAME


def latent_dir(node_id: str) -> Path:
    root = Path(_folders().get_output_directory()) / _LATENT_SUBDIR / str(node_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def latent_path(node_id: str, fingerprint: str) -> Path:
    """文件名只含指纹 hash：同内容（含采样参数）跨任务/跨位置命中同一文件。"""
    return latent_dir(node_id) / f"latent_{fingerprint}.pt"


def preview_path(node_id: str, sample_fp: str) -> Path:
    """采样预览文件（动画 WebP）：与 latent 同目录同指纹，生命周期同步（删除/切换/历史）。"""
    return latent_dir(node_id) / f"preview_{sample_fp}.webp"


def _latent_root() -> Path:
    """latent 根目录（output/minimax_h3_studio，各节点 id 子目录的父级）。"""
    return Path(_folders().get_output_directory()) / _LATENT_SUBDIR


def latent_exists(node_id: str, sample_fp: str) -> bool:
    """latent 文件存在性：优先 node_id 目录（标准路径），未命中时扫描根目录。

    ComfyUI 节点 id 可能变化（删除重建/复制节点），历史文件留在旧节点目录——
    若任务记录 node_id 与写入时不一致，标准路径查不到但文件实际存在。
    """
    if latent_path(node_id, sample_fp).exists():
        return True
    root = _latent_root()
    if not root.exists():
        return False
    name = f"latent_{sample_fp}.pt"
    try:
        for sub in root.iterdir():
            if sub.is_dir() and (sub / name).exists():
                return True
    except OSError:
        pass
    return False


def preview_exists(node_id: str, sample_fp: str) -> bool:
    """preview 文件存在性：与 latent_exists 同策略（标准路径 + 根目录扫描）。"""
    if preview_path(node_id, sample_fp).exists():
        return True
    root = _latent_root()
    if not root.exists():
        return False
    name = f"preview_{sample_fp}.webp"
    try:
        for sub in root.iterdir():
            if sub.is_dir() and (sub / name).exists():
                return True
    except OSError:
        pass
    return False


def preview_url(node_id: str, sample_fp: str) -> str:
    """preview 文件 → ComfyUI /view URL（output 目录，文件名+subfolder 推导）。

    不存在返回空串（前端据此隐藏预览）；URL 不落数据契约，由 sample_fp 随时重建。
    """
    if not sample_fp or not preview_exists(node_id, sample_fp):
        return ""
    from urllib.parse import urlencode

    params = urlencode(
        {"filename": f"preview_{sample_fp}.webp", "subfolder": f"{_LATENT_SUBDIR}/{node_id}", "type": "output"}
    )
    return f"/view?{params}"


# ---------- SQLite ----------

# 表结构版本（PRAGMA user_version）：记录这一份库经历过哪些演进步骤。
# 取代早期的"结构不符 → 删库重建"——库是用户资产，只允许就地升级。
# 2 = 只增不减的结构演进基建；3 = + pipeline_library（全局采样流程库）+ 内联流程收编
# 4 = + version_samples.latent_width/latent_height（记录实际 latent 像素尺寸，二采放大后与画布不同）
_SCHEMA_VERSION = 4

# 建表 DDL（幂等）。init_db() 每次都执行一遍：缺表就地补表，缺列就地补列。
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT,
    name         TEXT DEFAULT '',
    -- 时间线当前数据：{version, canvas, clips:[{clipId, enabled, 当前参数草稿...}]}
    timeline     TEXT,
    sampling_json TEXT,
    status       TEXT,
    created_at   REAL,
    updated_at   REAL
);
-- 片段版本（纯 Model）：采样固化的参数快照，不可变；同内容复用
CREATE TABLE IF NOT EXISTS clip_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER,
    clip_id      TEXT,
    content_fp   TEXT,
    -- 采样时的画布（"{width}x{height}@{fps}"，任务级分辨率，用于按分辨率分组展示）
    canvas       TEXT,
    -- 参数快照 JSON（前端契约同构，恢复编辑面板用）
    snapshot     TEXT,
    created_at   REAL
);
-- 采样历史：挂在版本上（version_id）。样本携带「当时怎么拍的规格」：
-- 画布 canvas + 时长 duration_sec（锁定出片时据此恢复任务分辨率与片段时长）；
-- seed 保留作抽卡标识（并参与缓存命中）。采样工艺（steps/sampler/cfg 等）
-- 不记录——无法从 latent 恢复，展示无意义。
-- latent_width/latent_height：**实际 latent 的像素尺寸**。自定义采样流程（子图）可能
-- 二采放大，最终 latent 网格与画布不再是 1:1，canvas 不足以判断复用是否尺寸匹配。
CREATE TABLE IF NOT EXISTS version_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER,
    clip_id      TEXT,
    version_id   INTEGER,
    content_fp   TEXT,
    canvas       TEXT,
    sample_fp    TEXT,
    seed         INTEGER,
    duration_sec REAL,
    continuity   INTEGER,
    frames       INTEGER,
    sample_len   INTEGER,
    latent_width  INTEGER,
    latent_height INTEGER,
    created_at   REAL,
    UNIQUE (task_id, clip_id, sample_fp)
);
-- 全局采样流程库（**跨任务共享**，不属于任何任务）：片段只存 pipelineId 引用，
-- 新建/切换/删除任务都不动它（规格见 AGENT.md）。def = 子图定义 JSON。
CREATE TABLE IF NOT EXISTS pipeline_library (
    id         TEXT PRIMARY KEY,
    name       TEXT DEFAULT '',
    def        TEXT,
    updated_at REAL
);
"""

# 补列清单（键=列名，值=ALTER TABLE ADD COLUMN 用的列定义；主键 id 不在此列）。
# 与 _SCHEMA_SQL 一一对应：**新增列时两处都要加**，已有库靠这里幂等补齐。
_SCHEMA_COLUMNS: dict[str, dict[str, str]] = {
    "tasks": {
        "node_id": "TEXT",
        "name": "TEXT DEFAULT ''",
        "timeline": "TEXT",
        "sampling_json": "TEXT",
        "status": "TEXT",
        "created_at": "REAL",
        "updated_at": "REAL",
    },
    "clip_versions": {
        "task_id": "INTEGER",
        "clip_id": "TEXT",
        "content_fp": "TEXT",
        "canvas": "TEXT",
        "snapshot": "TEXT",
        "created_at": "REAL",
    },
    "version_samples": {
        "task_id": "INTEGER",
        "clip_id": "TEXT",
        "version_id": "INTEGER",
        "content_fp": "TEXT",
        "canvas": "TEXT",
        "sample_fp": "TEXT",
        "seed": "INTEGER",
        "duration_sec": "REAL",
        "continuity": "INTEGER",
        "frames": "INTEGER",
        "sample_len": "INTEGER",
        "latent_width": "INTEGER",
        "latent_height": "INTEGER",
        "created_at": "REAL",
    },
    "pipeline_library": {
        "name": "TEXT DEFAULT ''",
        "def": "TEXT",
        "updated_at": "REAL",
    },
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# 「库已就绪」标记：init_db() 挂在**每个** DB 操作入口上（19 处），必须保证真正的
# 建表/补列/写 user_version 每个库路径每进程只跑一次——否则每次查询都要跑一遍
# executescript + 逐表 PRAGMA + ALTER 检查，还会给纯读操作平白加上写锁。
# key = 库文件路径（测试里换目录、运行中库文件被删掉时自动重新初始化）。
_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def init_db() -> None:
    """确保任务库可用（首次调用才真正建表/就地升级）。

    **每个库文件路径每进程只真正初始化一次**：各操作入口统一调它是为了免掉
    "no such table/column"，但除第一次外都只是「命中标记 + 一次 stat」。

    结构演进策略（取代早期的"结构不符 → 删库重建"）：
    - 库文件不存在 → 按当前定义建全表，并写入 user_version；
    - 库文件已存在 → 幂等补表（CREATE TABLE IF NOT EXISTS）+ 补列（ALTER TABLE ADD COLUMN），
      补了什么写进日志；缺哪列补哪列，既有数据一律不动；
    - 库文件损坏/不是 SQLite → **报错但不删除**，由用户备份后自行处理。

    演进步骤请同时更新 _SCHEMA_VERSION / _SCHEMA_SQL / _SCHEMA_COLUMNS 三处；
    不要再引入任何删除数据库文件或删表的分支。
    """
    path = db_path()
    key = str(path)
    if key in _schema_ready and path.exists():
        return
    with _schema_lock:  # HTTP 线程与采样线程可能同时首次进来
        if key in _schema_ready and path.exists():
            return
        _init_db_once(path)
        _schema_ready.add(key)


def _init_db_once(path: Path) -> None:
    """真正的初始化：新库建表 / 已有库就地升级（只增不减，绝不删文件）。"""
    if not path.exists():
        _create_schema(path)
        return
    try:
        conn = _connect()
        try:
            changes = _upgrade_schema(conn)
            conn.commit()
        finally:
            conn.close()  # 显式关闭：异常路径也要释放文件句柄（Windows 下会锁库文件）
    except sqlite3.DatabaseError as exc:
        # 损坏的库是用户资产：插件绝不代为删除，抛出清晰错误让用户自己备份/修复
        raise RuntimeError(
            f"任务数据库无法打开：{path}（{exc}）。插件不会删除它；"
            "请先备份该文件（或重命名后再启动，让插件另建一份新库）"
        ) from exc
    if changes:
        log.warning("任务库已就地升级（只增不减，未删除任何数据）：%s", "；".join(changes))


def _create_schema(path: Path) -> None:
    """按当前代码建表（只在库文件不存在时调用）并写入结构版本。

    MVC 分层：clip_versions / version_samples 是纯 Model（历史版本 + 采样）；
    片段当前数据（草稿）由前端存入 tasks.timeline（时间线当前完整数据），
    与历史解耦（反悔 = 快照复制加载，不指向历史）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA_SQL)
        _ensure_columns(conn)  # 防御：新库正常不会有缺列
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()  # 不显式关闭会留住文件句柄（Windows 上表现成"文件被占用"）


def _upgrade_schema(conn: sqlite3.Connection) -> list[str]:
    """幂等补齐表与列（只做加法）+ 按 user_version 跑一次性数据迁移。

    返回人类可读的变更清单，供日志回显。
    """
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.executescript(_SCHEMA_SQL)
    changes = [f"新建表 {name}" for name in _SCHEMA_COLUMNS if name not in existing]
    changes += _ensure_columns(conn)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < _SCHEMA_VERSION:
        # 一次性数据迁移步骤（每步都要幂等：重跑不产生副作用）
        changes += _migrate_inline_pipelines(conn)
    if version != _SCHEMA_VERSION:
        changes.append(f"结构版本 {version} → {_SCHEMA_VERSION}")
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    return changes


def _ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """按需补列：缺哪列 ALTER TABLE ADD COLUMN 补哪列（绝不删列/删表）。"""
    added: list[str] = []
    for table, columns in _SCHEMA_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "id" not in have:
            raise RuntimeError(f"任务库结构损坏：表 {table} 缺少主键列 id（请备份后手动处理）")
        for name, ddl in columns.items():
            if name in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            added.append(f"{table} 补列 {name}")
    return added


# ---------- 全局采样流程库（pipeline_library，跨任务共享） ----------
# 为什么独立于 tasks.timeline：流程是**可复用的工艺资产**，不属于某条片子。放任务里会
# 随任务切换消失（用户必须每个任务重建一份），也无法一处改到处生效。因此提到全局表，
# 片段只保留 pipelineId 引用（引用模型不变，只是库的作用域变成全局）。


def _pipeline_rows(entries) -> list[tuple[str, str, str]]:
    """宽松过滤 + 归一化流程库条目 → [(id, name, def_json), …]（非法条目直接丢弃）。"""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        pid = str(e.get("id") or "").strip()
        name = str(e.get("name") or "").strip()
        def_obj = e.get("def")
        if not pid or not name or not isinstance(def_obj, dict) or pid in seen:
            continue
        seen.add(pid)
        rows.append((pid, name, json.dumps(def_obj, ensure_ascii=False)))
    return rows


def list_pipeline_library() -> list[dict]:
    """全局流程库全部条目：[{id, name, def}]（def 已解析为对象；坏条目跳过不报错）。"""
    init_db()
    out: list[dict] = []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, def FROM pipeline_library ORDER BY updated_at DESC, id"
        ).fetchall()
    for r in rows:
        try:
            def_obj = json.loads(r["def"] or "null")
        except (TypeError, ValueError):
            log.warning("流程库条目 %s 的定义不是合法 JSON，已跳过", r["id"])
            continue
        if not isinstance(def_obj, dict):
            continue
        out.append({"id": str(r["id"]), "name": str(r["name"] or ""), "def": def_obj})
    return out


def save_pipeline_library(entries, confirm_clear: bool = False) -> int:
    """**覆盖式**保存全局流程库（前端是唯一编辑者：整库提交，含重命名/删除）。

    整库替换在一个事务里完成：要么全部生效，要么原样保留——半更新的库比旧库更糟
    （出现指向不存在定义的引用）。返回写入条数。

    额外保险：请求为**空**而库里非空时，必须显式 confirm_clear（否则 ValueError → 409）。
    前端在 pipelinesLoaded 为真之后才提交，正常流程始终带 confirm_clear；这道保险专门
    拦「还没加载就整库提交」的客户端——那会把用户的全部自定义流程删光。
    """
    init_db()
    rows = _pipeline_rows(entries)
    now = time.time()
    with _connect() as conn:
        existing = int(conn.execute("SELECT COUNT(*) FROM pipeline_library").fetchone()[0])
        if existing and not rows and not confirm_clear:
            raise ValueError(f"拒绝用空库覆盖 {existing} 份已有采样流程（缺少 confirm_clear）")
        conn.execute("DELETE FROM pipeline_library")
        conn.executemany(
            "INSERT OR REPLACE INTO pipeline_library (id, name, def, updated_at)"
            " VALUES (?, ?, ?, ?)",
            [(pid, name, def_json, now) for pid, name, def_json in rows],
        )
    return len(rows)


def merge_pipeline_library(entries) -> int:
    """按 id **并入**全局流程库（导入任务文件时用）：已存在的 id 保留库里那份
    （导入方本地版本为准，避免别人的同名流程覆盖自己的工艺）；新 id 直接入库，
    重名自动加「（N）」后缀。返回新增条数。
    """
    init_db()
    rows = _pipeline_rows(entries)
    if not rows:
        return 0
    existing = list_pipeline_library()
    known_ids = {p["id"] for p in existing}
    used_names = {p["name"] for p in existing}
    now = time.time()
    added = 0
    with _connect() as conn:
        for pid, name, def_json in rows:
            if pid in known_ids:
                continue
            unique = name
            i = 2
            while unique in used_names and i < 1000:
                unique = f"{name}（{i}）"
                i += 1
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_library (id, name, def, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (pid, unique, def_json, now),
            )
            known_ids.add(pid)
            used_names.add(unique)
            added += 1
    return added


def referenced_pipelines(timeline: dict) -> list[dict]:
    """时间线草稿里**被引用**的全局流程定义（导出时内嵌，让导入方也能跑同一条链）。

    库是全局的，任务文件本身不含定义；不内嵌的话「导出任务给同事」会全部退回官方流程。
    """
    ids: list[str] = []
    for c in timeline.get("clips") or []:
        if not isinstance(c, dict):
            continue
        pid = str(c.get("pipelineId") or "").strip()
        if pid and pid not in ids:
            ids.append(pid)
    if not ids:
        return []
    by_id = {p["id"]: p for p in list_pipeline_library()}
    return [by_id[i] for i in ids if i in by_id]


def _migrate_inline_pipelines(conn: sqlite3.Connection) -> list[str]:
    """（一次性数据迁移）历史 timeline 内联的流程定义 → 全局库，并从 timeline 删掉该键。

    老版本把定义存在任务里（clips[].pipeline.def 或 timeline.pipelines）；全局化之后定义
    只住全局库一处。这里把它们收编进库而不是丢掉——前端已不再认 timeline.pipelines，
    丢了用户就得重建。
    幂等：跑过一次后 timeline 里不再有 pipelines 键（第二次什么都不做）。撞 id 用
    INSERT OR IGNORE：本机已有就先到先得，绝不用旧副本覆盖。
    """
    moved = 0
    touched = 0
    for row in conn.execute("SELECT id, timeline FROM tasks").fetchall():
        timeline = _safe_json(row["timeline"])
        entries = timeline.pop("pipelines", None)
        if not isinstance(entries, list) or not entries:
            continue
        now = time.time()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO pipeline_library (id, name, def, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    pid,
                    str(entry.get("name") or ""),
                    json.dumps(entry.get("def"), ensure_ascii=False),
                    now,
                ),
            )
            moved += 1
        conn.execute(
            "UPDATE tasks SET timeline = ? WHERE id = ?",
            (json.dumps(timeline, ensure_ascii=False), row["id"]),
        )
        touched += 1
    if not moved:
        return []
    return [f"收编 {moved} 份内联采样流程到全局库（涉及 {touched} 个任务）"]


def create_task(
    node_id: str,
    timeline: str,
    sampling: dict,
    status: str = "running",
    name: str = "",
) -> int:
    """创建任务记录，返回自增主键 id（int）。

    status：running=执行中（旧执行路径）；created=前端新建的待编辑任务。
    name：任务可读名称（列表展示用；空则显示创建时间）。
    timeline：时间线当前数据（canvas + clips[]，含每片段草稿）。
    """
    init_db()
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (node_id, name, timeline, sampling_json, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (node_id, name, timeline, json.dumps(sampling, ensure_ascii=False), status, now, now),
        )
        return int(cur.lastrowid)


def _tid(task_id) -> int:
    """task_id 统一转自增整数 id。"""
    return int(task_id)


def update_task_name(task_id, name: str) -> None:
    """重命名任务（列表展示用）。"""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET name = ?, updated_at = ? WHERE id = ?",
            (name, time.time(), _tid(task_id)),
        )


def update_task_status(task_id, status: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), _tid(task_id)),
        )


def update_task_node_id(task_id, node_id: str) -> None:
    """同步任务记录的 node_id 为当前执行节点（节点 id 可能变化，保证 latent 路径推导一致）。"""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET node_id = ?, updated_at = ? WHERE id = ?",
            (str(node_id), time.time(), _tid(task_id)),
        )


def update_task_timeline(task_id, timeline: str) -> None:
    """保存时间线当前数据（前端实时编辑 → DB，覆盖式自动保存，草稿不丢）。"""
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE tasks SET timeline = ?, updated_at = ? WHERE id = ?",
            (timeline, time.time(), _tid(task_id)),
        )


def record_version_sample(
    task_id,
    clip_id: str,
    content_fp: str,
    sample_fp: str,
    snapshot: dict,
    sampling: dict,
    frames: int,
    sample_len: int,
    canvas: str = "",
    duration_sec: float = 0.0,
    continuity: bool = False,
    latent_width: int = 0,
    latent_height: int = 0,
) -> int:
    """采样成功后固化提示词条目（纯 Model）+ 采样记录挂其下，返回归属的 version_id。

    MVC 分层：clip_versions 是纯历史条目（无"当前"标记）——同内容复用，不同
    内容新建；片段当前数据（草稿）由前端存 timeline，与历史解耦。
    canvas/duration_sec：样本携带的规格（锁定出片时前端据此恢复任务分辨率与
    片段时长）；采样工艺（steps/sampler/cfg…）不落库（无法从 latent 恢复），
    仅 sampling.seed 取作抽卡标识。
    latent_width/latent_height：实际 latent 的像素尺寸（自定义采样流程二采放大时
    ≠ canvas；前端据此显示并高亮）。读不出时为 0，前端按无记录处理。
    """
    init_db()
    tid = _tid(task_id)
    now = time.time()
    with _connect() as conn:
        # 1. 条目：同内容（content_fp）复用，否则新建
        row = conn.execute(
            "SELECT id FROM clip_versions"
            " WHERE task_id = ? AND clip_id = ? AND content_fp = ?",
            (tid, str(clip_id), content_fp),
        ).fetchone()
        if row:
            version_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO clip_versions (task_id, clip_id, content_fp, canvas, snapshot, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (tid, str(clip_id), content_fp, canvas,
                 json.dumps(snapshot or {}, ensure_ascii=False), now),
            )
            version_id = int(cur.lastrowid)
            log.info(
                "固化新条目: task=%s clip=%s content_fp=%s canvas=%s",
                task_id, clip_id, content_fp, canvas,
            )
        # 2. 采样记录：挂在条目上（同 sample_fp 只一份）
        conn.execute(
            "INSERT OR REPLACE INTO version_samples"
            " (task_id, clip_id, version_id, content_fp, canvas, sample_fp, seed,"
            "  duration_sec, continuity, frames, sample_len, latent_width, latent_height, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, str(clip_id), version_id, content_fp, canvas, sample_fp,
                int(sampling.get("seed", 0)), float(duration_sec),
                int(bool(continuity)), int(frames), int(sample_len or frames),
                int(latent_width or 0), int(latent_height or 0), now,
            ),
        )
        return int(version_id)


def get_task(task_id) -> dict | None:
    init_db()
    try:
        tid = _tid(task_id)
    except (TypeError, ValueError):
        return None
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["task_id"] = str(out["id"])  # 兼容外部 task_id 字段
    return out


def list_tasks(node_id: str | None = None, limit: int = 50) -> list[dict]:
    init_db()
    with _connect() as conn:
        if node_id:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE node_id = ? ORDER BY created_at DESC LIMIT ?",
                (node_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["task_id"] = str(d["id"])
        out.append(d)
    return out


def delete_task(task_id) -> None:
    """删除任务记录及其所有缓存文件（latent 按采样指纹命名，路径由 node_id+sample_fp 推导）。"""
    init_db()
    task = get_task(task_id)
    node_id = (task or {}).get("node_id") or ""
    tid = _tid(task_id)
    samples = _all_samples(tid)
    with _connect() as conn:
        conn.execute("DELETE FROM clip_versions WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM version_samples WHERE task_id = ?", (tid,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    if node_id:
        for s in samples:
            fp = s.get("sample_fp")
            if fp:
                try:
                    latent_path(node_id, fp).unlink(missing_ok=True)
                    preview_path(node_id, fp).unlink(missing_ok=True)  # 预览随 latent 同删
                except OSError:
                    pass


def _all_samples(task_id) -> list[dict]:
    """读取任务全部采样指纹（删除时清理 latent 文件用）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sample_fp FROM version_samples WHERE task_id = ?",
            (_tid(task_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_clip_history(task_id, clip_id: str) -> None:
    """删除片段（按身份 clip_id）的全部历史：版本 + 采样记录 + latent 文件。

    用户确认语义：删除片段 = 连其所有采样缓存彻底删除（不可逆）。
    sample_fp 含 content_fp，文件唯一归属该片段，删除不会误伤其他片段。
    """
    init_db()
    task = get_task(task_id)
    node_id = (task or {}).get("node_id") or ""
    tid = _tid(task_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sample_fp FROM version_samples WHERE task_id = ? AND clip_id = ?",
            (tid, str(clip_id)),
        ).fetchall()
        conn.execute(
            "DELETE FROM clip_versions WHERE task_id = ? AND clip_id = ?", (tid, str(clip_id))
        )
        conn.execute(
            "DELETE FROM version_samples WHERE task_id = ? AND clip_id = ?", (tid, str(clip_id))
        )
    if node_id:
        for r in rows:
            fp = r["sample_fp"]
            if fp:
                try:
                    latent_path(node_id, fp).unlink(missing_ok=True)
                    preview_path(node_id, fp).unlink(missing_ok=True)  # 预览随 latent 同删
                except OSError:
                    pass


def _sample_fp_referenced_elsewhere(sample_fp: str, task_id, clip_id: str) -> bool:
    """该采样指纹是否仍被其他记录引用（同节点跨任务共享缓存文件的保护）。

    同节点不同任务的内容完全相同的片段共享同一 latent 文件——删除样本时若仍有
    其他引用，只删 DB 行保留文件，避免误伤其他任务。
    """
    init_db()
    tid = _tid(task_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM version_samples"
            " WHERE sample_fp = ? AND NOT (task_id = ? AND clip_id = ?) LIMIT 1",
            (sample_fp, tid, str(clip_id)),
        ).fetchone()
    return row is not None


def _unlink_sample_files(node_id: str, sample_fp: str) -> None:
    """删除样本的 latent + preview 文件（best-effort）。"""
    if not node_id or not sample_fp:
        return
    try:
        latent_path(node_id, sample_fp).unlink(missing_ok=True)
        preview_path(node_id, sample_fp).unlink(missing_ok=True)
    except OSError:
        pass


def delete_version_sample(task_id, clip_id: str, sample_fp: str) -> None:
    """删除单个采样样本（某次抽卡）：DB 行 +（无其他引用时）latent/preview 文件。

    引用保护：同节点跨任务共享文件时只删记录保留文件（其他任务缓存不受影响）；
    无其他引用才连文件删除。用户确认语义：该次抽卡从历史消失，不可恢复。
    """
    init_db()
    task = get_task(task_id)
    node_id = (task or {}).get("node_id") or ""
    tid = _tid(task_id)
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM version_samples WHERE task_id = ? AND clip_id = ? AND sample_fp = ?",
            (tid, str(clip_id), sample_fp),
        )
        deleted = cur.rowcount > 0
    # 引用保护：删掉的行仍被其他记录引用（跨任务共享文件）时保留文件；
    # 未删到行（rowcount=0）也绝不动文件
    if deleted and not _sample_fp_referenced_elsewhere(sample_fp, task_id, clip_id):
        _unlink_sample_files(node_id, sample_fp)


def delete_clip_version(task_id, clip_id: str, version_id: int) -> None:
    """删除单个历史版本（参数状态）及其全部采样：DB 行 +（无引用时）文件。

    版本是采样固化的参数快照，其下样本随之删除；每个样本同样做跨任务引用保护。
    """
    init_db()
    task = get_task(task_id)
    node_id = (task or {}).get("node_id") or ""
    tid = _tid(task_id)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sample_fp FROM version_samples"
            " WHERE task_id = ? AND clip_id = ? AND version_id = ?",
            (tid, str(clip_id), int(version_id)),
        ).fetchall()
        conn.execute(
            "DELETE FROM version_samples WHERE task_id = ? AND clip_id = ? AND version_id = ?",
            (tid, str(clip_id), int(version_id)),
        )
        conn.execute(
            "DELETE FROM clip_versions WHERE task_id = ? AND clip_id = ? AND id = ?",
            (tid, str(clip_id), int(version_id)),
        )
    for r in rows:
        fp = r["sample_fp"]
        if fp and not _sample_fp_referenced_elsewhere(fp, task_id, clip_id):
            _unlink_sample_files(node_id, fp)


def get_sample_meta(task_id, clip_id: str, sample_fp: str) -> tuple[int, int] | None:
    """查询采样记录的 (sample_len, frames)——解码 trim 需要（latent 文件本身不含）。"""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT sample_len, frames FROM version_samples"
            " WHERE task_id = ? AND clip_id = ? AND sample_fp = ?",
            (_tid(task_id), str(clip_id), sample_fp),
        ).fetchone()
    if not row:
        return None
    return int(row["sample_len"]), int(row["frames"])


def get_sample_canvas(task_id, clip_id: str, sample_fp: str) -> str | None:
    """查询采样记录的画布标签（"{w}x{h}@{fps}"）。

    锁定 latent 出片前的兜底校验：只比对画布（内容一致性由前端"启用采样"时联动
    恢复保证），画布不符的 latent 尺寸不匹配会在解码合并时崩溃，必须拦截。
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT canvas FROM version_samples"
            " WHERE task_id = ? AND clip_id = ? AND sample_fp = ?",
            (_tid(task_id), str(clip_id), sample_fp),
        ).fetchone()
    return row["canvas"] if row else None


def get_clip_history(task_id, canvas: str | None = None) -> dict:
    """任务全部片段的历史（纯 Model）：按 clip_id 分组，提示词条目 + 采样记录。

    返回结构（供前端渲染历史区）：
      {clip_id: {versions: [...], samples: [...]}}
    - versions：提示词条目（采样固化的画面语义快照，含 versionId/contentFp/canvas/snapshot/createdAt）
    - samples：全部采样记录（含 versionId 归属、canvas、seed 等工艺参数、文件存在性）
    画布不做后端过滤：前端按采样记录的 canvas 判定可用性（与当前画布一致才可锁定出片，
    否则折叠为失效样本可清理）。片段当前数据（草稿）由前端存 timeline，本接口不返回。
    """
    init_db()
    task = get_task(task_id)
    node_id = (task or {}).get("node_id") or ""
    tid = _tid(task_id)
    with _connect() as conn:
        ver_rows = conn.execute(
            "SELECT id, clip_id, content_fp, canvas, snapshot, created_at"
            " FROM clip_versions WHERE task_id = ? AND (canvas = ? OR ? IS NULL)"
            " ORDER BY created_at DESC",
            (tid, canvas, canvas),
        ).fetchall()
        sample_rows = conn.execute(
            "SELECT version_id, clip_id, content_fp, canvas, sample_fp, seed,"
            " duration_sec, continuity, frames, sample_len, latent_width, latent_height, created_at"
            " FROM version_samples WHERE task_id = ? AND (canvas = ? OR ? IS NULL)"
            " ORDER BY created_at DESC",
            (tid, canvas, canvas),
        ).fetchall()

    out: dict[str, dict] = {}
    for r in ver_rows:
        clip_id = r["clip_id"] or ""
        if not clip_id:
            continue
        entry = out.setdefault(clip_id, {"versions": [], "samples": []})
        entry["versions"].append(
            {
                "versionId": r["id"],
                "contentFp": r["content_fp"],
                "canvas": r["canvas"] or "",
                "snapshot": _safe_json(r["snapshot"]),
                "createdAt": r["created_at"],
            }
        )
    for r in sample_rows:
        clip_id = r["clip_id"] or ""
        if not clip_id:
            continue
        entry = out.setdefault(clip_id, {"versions": [], "samples": []})
        entry["samples"].append(
            {
                "versionId": r["version_id"],
                "contentFp": r["content_fp"],
                "canvas": r["canvas"] or "",
                "sampleFp": r["sample_fp"],
                "seed": r["seed"],
                "durationSec": float(r["duration_sec"] or 0),
                "continuity": bool(r["continuity"]),
                "frames": r["frames"],
                "sampleLen": r["sample_len"],
                # 实际 latent 像素尺寸（二采放大时 ≠ canvas）；0/缺失 = 旧样本无记录
                "latentWidth": int(r["latent_width"] or 0) or None,
                "latentHeight": int(r["latent_height"] or 0) or None,
                "createdAt": r["created_at"],
                "exists": bool(node_id) and latent_exists(node_id, r["sample_fp"]),
                "previewUrl": preview_url(node_id, r["sample_fp"]),
            }
        )
    return out


def _safe_json(text: str | None) -> dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


# ---------- 任务导入导出（只含时间线草稿 + 历史元数据，不含素材/latent 文件） ----------

# 导出文件类型标记（导入时校验）
EXPORT_TYPE = "minimax-h3-studio-task"
EXPORT_FORMAT_VERSION = 1
# 采样记录中的运行时派生字段（存在性/预览 URL 依赖本机缓存，不导出）
_RUNTIME_SAMPLE_KEYS = {"exists", "previewUrl"}


def _strip_sample_locks(timeline: dict) -> dict:
    """清洗 timeline 草稿（导出/复制/导入都要过一遍）：

    - 移除 clips 的 sampleFp（锁定指针指向本地 latent 缓存文件，导出不含缓存文件，
      导入后必然失效；保留会造成卡片"已锁定但文件丢失"卡住）。
    - 丢弃历史遗留的 `pipelines` 字段：流程定义住在**全局流程库**（pipeline_library），
      任务时间线里再带一份就是两处真相（导出时另有 `pipelines` 顶层段内嵌被引用的定义）。
    """
    out = dict(timeline)
    out.pop("pipelines", None)
    clips = out.get("clips")
    if isinstance(clips, list):
        out["clips"] = [
            {k: v for k, v in c.items() if k != "sampleFp"} if isinstance(c, dict) else c
            for c in clips
        ]
    return out
def export_task(task_id) -> dict | None:
    """导出任务为可移植 JSON：name + timeline 草稿（剥 sampleFp）+ 采样参数 + 历史元数据。

    不含实际素材文件与 latent/preview 缓存文件（导入方无缓存时采样记录 exists=False，
    只能用于恢复提示词条目与展示工艺元数据，重新采样后按 content_fp 回归原条目）。
    """
    init_db()
    task = get_task(task_id)
    if not task:
        return None
    try:
        timeline = json.loads(task.get("timeline") or "{}")
        if not isinstance(timeline, dict):
            timeline = {}
    except (TypeError, ValueError):
        timeline = {}
    try:
        sampling = json.loads(task.get("sampling_json") or "{}")
        if not isinstance(sampling, dict):
            sampling = {}
    except (TypeError, ValueError):
        sampling = {}
    history: dict = {}
    for clip_id, h in get_clip_history(task_id).items():
        history[clip_id] = {
            "versions": h.get("versions", []),
            "samples": [
                {k: v for k, v in s.items() if k not in _RUNTIME_SAMPLE_KEYS}
                for s in h.get("samples", [])
            ],
        }
    return {
        "type": EXPORT_TYPE,
        "formatVersion": EXPORT_FORMAT_VERSION,
        "exportedAt": float(task.get("created_at") or time.time()),
        "name": str(task.get("name") or ""),
        "timeline": _strip_sample_locks(timeline),
        # 被引用的全局流程定义（导入方并入自己的流程库；空 = 该任务没用自定义流程）
        "pipelines": referenced_pipelines(timeline),
        "sampling": sampling,
        "history": history,
    }


def import_task(data: dict, node_id: str) -> int:
    """从导出 payload 导入为新任务：时间线草稿 + 提示词历史（条目+采样记录元数据）。

    校验导出标记后建 tasks 行，再按 clip_id 重建 clip_versions/version_samples
    （version id 重映射；保留原 created_at 使历史顺序一致）。sampleFp 锁定一律剥离
    （缓存文件不随导出迁移）。文件里内嵌的 `pipelines` 按 id 并入**全局流程库**
    （已存在的 id 保留本地那份），导入后卡片的流程引用即可解析。返回新 task_id。
    """
    init_db()
    if not isinstance(data, dict) or data.get("type") != EXPORT_TYPE:
        raise ValueError("不是有效的创意工作台任务导出文件（type 标记缺失/不符）")
    timeline_raw = data.get("timeline")
    if not isinstance(timeline_raw, dict):
        raise ValueError("导出文件缺少 timeline")
    timeline_json = json.dumps(_strip_sample_locks(timeline_raw), ensure_ascii=False)
    name = str(data.get("name") or "导入任务")[:50]
    sampling = data.get("sampling") if isinstance(data.get("sampling"), dict) else {}
    # 先并入流程库：时间线里的 pipelineId 引用到 loadTask 时就能解析（顺序无关紧要，
    # 但先入库语义更顺——库是任务的外部依赖）
    merge_pipeline_library(data.get("pipelines"))
    tid = create_task(node_id, timeline_json, sampling, status="created", name=name)

    history = data.get("history")
    if not isinstance(history, dict):
        return tid
    now = time.time()
    with _connect() as conn:
        for clip_id, h in history.items():
            if not isinstance(h, dict):
                continue
            vmap: dict[int, int] = {}
            for v in h.get("versions") or []:
                if not isinstance(v, dict):
                    continue
                try:
                    cur = conn.execute(
                        "INSERT INTO clip_versions"
                        " (task_id, clip_id, content_fp, canvas, snapshot, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            tid, str(clip_id), str(v.get("contentFp") or ""),
                            str(v.get("canvas") or ""),
                            json.dumps(v.get("snapshot") or {}, ensure_ascii=False),
                            float(v.get("createdAt") or now),
                        ),
                    )
                    old_id = int(v.get("versionId") or 0)
                    if old_id:
                        vmap[old_id] = int(cur.lastrowid)
                except (TypeError, ValueError):
                    continue
            for s in h.get("samples") or []:
                if not isinstance(s, dict):
                    continue
                new_vid = vmap.get(int(s.get("versionId") or 0))
                if new_vid is None:
                    continue  # 归属条目缺失则跳过（不一致数据）
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO version_samples"
                        " (task_id, clip_id, version_id, content_fp, canvas, sample_fp, seed,"
                        "  duration_sec, continuity, frames, sample_len, latent_width, latent_height, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            tid, str(clip_id), new_vid, str(s.get("contentFp") or ""),
                            str(s.get("canvas") or ""), str(s.get("sampleFp") or ""),
                            int(s.get("seed") or 0), float(s.get("durationSec") or 0),
                            int(bool(s.get("continuity"))),
                            int(s.get("frames") or 0), int(s.get("sampleLen") or 0),
                            int(s.get("latentWidth") or 0), int(s.get("latentHeight") or 0),
                            float(s.get("createdAt") or now),
                        ),
                    )
                except (TypeError, ValueError):
                    continue
    return tid


def duplicate_task(task_id, node_id: str = "", name: str = "") -> int:
    """复制任务为新任务：时间线草稿（剥 sampleFp 锁定）+ 提示词历史条目（clip_versions）。

    采样记录（version_samples）与 latent/preview 缓存文件一律不复制——副本不携带任何
    latent 缓存信息，锁定状态清空，Queue 时按当前内容重新采样。版本条目是纯画面语义
    快照（无缓存引用，保留供回填/比对），clip_id 沿用（历史跟随卡片身份）。
    node_id：未指定时沿用源任务（未来执行会再同步为实际节点）。返回新 task_id。
    """
    init_db()
    src = get_task(task_id)
    if not src:
        raise ValueError(f"任务不存在: {task_id}")
    if not str(node_id or ""):
        node_id = str(src.get("node_id") or "")
    try:
        timeline = json.loads(src.get("timeline") or "{}")
        if not isinstance(timeline, dict):
            timeline = {}
    except (TypeError, ValueError):
        timeline = {}
    timeline_json = json.dumps(_strip_sample_locks(timeline), ensure_ascii=False)
    src_name = str(src.get("name") or "").strip() or "任务"
    new_name = (str(name or "").strip() or f"{src_name} 副本")[:50]
    tid = create_task(node_id, timeline_json, {}, status="created", name=new_name)
    now = time.time()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT clip_id, content_fp, canvas, snapshot, created_at"
            " FROM clip_versions WHERE task_id = ?",
            (_tid(task_id),),
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO clip_versions"
                " (task_id, clip_id, content_fp, canvas, snapshot, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    str(r["clip_id"] or ""),
                    str(r["content_fp"] or ""),
                    str(r["canvas"] or ""),
                    str(r["snapshot"] or ""),
                    float(r["created_at"] or now),
                ),
            )
    return tid


def canvas_label(canvas: dict) -> str:
    """画布对象 → 分辨率标签（"{width}x{height}@{fps}"），样本按此判可用性。"""
    return f"{int(canvas.get('width', 0))}x{int(canvas.get('height', 0))}@{int(canvas.get('fps', 0))}"


# ---------- 指纹 ----------

def content_fingerprint(seg, canvas: dict | None = None) -> str:
    """提示词条目指纹（版本身份 content_fp）= 纯画面语义：mode/prompt/素材。

    执行态（enabled）、画布（canvas）与时长（duration_sec）不参与身份：勾选参与生成、
    切换全局画布、调整片段时长都不产生新条目——画布/时长差异由采样指纹
    （sample_fingerprint 的 canvas/duration_sec 分量）承担文件防覆盖，
    规格只让旧采样失效/另存，而非让提示词历史分裂。
    内容一变即新条目；同内容跨次编辑去重。
    """
    parts = [
        seg.mode,
        seg.prompt,
        *(f"img:{m.path}" for m in seg.ref_images),
        *(f"vid:{m.path}" for m in seg.ref_videos),
        *(f"aud:{m.path}" for m in seg.ref_audios),
        seg.first_frame.path if seg.first_frame else "",
        seg.last_frame.path if seg.last_frame else "",
        seg.source_video.path if seg.source_video else "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def sample_fingerprint(
    clip_id: str,
    content_fp: str,
    sampling: dict,
    *,
    continuity_enabled: bool,
    continuity_frames: int,
    canvas: str = "",
    duration_sec: float = 0.0,
) -> str:
    """采样指纹（latent 文件 ID sample_fp）：clip 归属 + 内容指纹 + 规格 + 工艺 + continuity。

    clip_id 加入指纹——卡片（clip）是任务私有的，跨任务即使内容完全相同（同 content_fp）
    也不共享 latent 文件（任务 A 不能用任务 B 的卡片，自然也不能用其缓存）；
    同 clip_id 跨任务恢复（addClipsFromHistory 沿用 id）仍可命中，身份延续。
    canvas（"{w}x{h}@{fps}"）与 duration_sec 为文件防覆盖分量：内容指纹已不含画布/时长，
    同内容跨画布/改时长采样必须落在不同文件，否则互相覆盖/错命。工艺参数（seed/steps/…）
    仍参与指纹（不同工艺产出不同 latent），但只在内部区分文件，不落库不展示。
    同片段同规格同工艺重跑命中同一文件（条目内缓存复用）。
    """
    parts = [
        str(clip_id),
        content_fp,
        str(canvas),
        f"{duration_sec:.3f}",
        str(sampling.get("seed")), f"{sampling.get('cfg', 1.0):.6f}", str(sampling.get("steps")),
        str(sampling.get("sampler")), str(sampling.get("scheduler")),
        f"{sampling.get('shift_video', 12.0):.6f}", f"{sampling.get('shift_audio', 3.0):.6f}",
        str(continuity_enabled), str(continuity_frames),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------- latent 读写 ----------

def _av_latent_to_cpu(av_latent: dict) -> dict:
    """AV latent → CPU 化并拆流为 tuple（避免 pickle 自定义类依赖）。"""
    samples = av_latent["samples"]
    if hasattr(samples, "unbind"):
        parts = tuple(p.detach().cpu().contiguous() for p in samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = tuple(p.detach().cpu().contiguous() for p in samples)
    elif torch.is_tensor(samples):
        parts = samples.detach().cpu().contiguous()
    else:
        parts = samples
    out: dict[str, Any] = {"samples": parts}
    for key, value in av_latent.items():
        if key == "samples":
            continue
        if hasattr(value, "unbind"):  # NestedTensor 类字段（如 noise_mask）
            out[key] = tuple(p.detach().cpu().contiguous() for p in value.unbind())
        elif torch.is_tensor(value):
            out[key] = value.detach().cpu().contiguous()
        else:
            out[key] = value
    return out


def save_av_latent(path: Path, av_latent: dict) -> bool:
    """写盘（best-effort，失败不阻塞）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(_av_latent_to_cpu(av_latent), path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("AV latent 写盘失败（跳过缓存）: %s", exc)
        return False


def load_av_latent(path: str | Path | None) -> dict | None:
    """读盘（CPU + weights_only 安全加载），并把拆流的 samples 重建为原始结构。"""
    if not path:
        return None
    try:
        payload = torch.load(path, map_location="cpu", weights_only=True)
        if not isinstance(payload, dict) or "samples" not in payload:
            return None
        return _restore_av_latent(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("AV latent 读盘失败: %s", exc)
        return None


def _restore_av_latent(payload: dict) -> dict:
    """缓存时 samples 拆成了 tuple，这里重建为 NestedTensor（有 unbind）。

    - 解码（VAEDecode / VAEDecodeAudio 直接吃 AV latent）需要 samples.unbind()
    - 段间引导（motion_context._streams_from_latent）兼容 tuple 与 unbind
    重建失败时保持 tuple（段间引导仍可用，解码会报错提示）。
    """
    samples = payload["samples"]
    if isinstance(samples, (tuple, list)):
        try:
            import comfy.nested_tensor

            payload = {**payload, "samples": comfy.nested_tensor.NestedTensor(tuple(samples))}
        except Exception as exc:  # noqa: BLE001
            log.warning("AV latent samples 重建失败（保持 tuple）: %s", exc)
    return payload


# ---------- 合并 ----------

def merge_audios(audio_list: list[dict]) -> dict:
    """各段 AUDIO 拼接（waveform concat，通道数补齐一致）。"""
    valid = [a for a in audio_list if isinstance(a, dict) and a.get("waveform") is not None]
    if not valid:
        return {"waveform": torch.zeros(1, 1, 1), "sample_rate": 32000}
    sr = int(valid[0].get("sample_rate") or 32000)
    waves = [a["waveform"] for a in valid]
    max_ch = max(int(w.shape[1]) for w in waves)
    padded = []
    for w in waves:
        if int(w.shape[1]) < max_ch:
            pad = torch.zeros(1, max_ch - int(w.shape[1]), int(w.shape[-1]), dtype=w.dtype)
            w = torch.cat([w, pad], dim=1)
        padded.append(w)
    merged = torch.cat(padded, dim=-1)
    return {"waveform": merged, "sample_rate": sr}
