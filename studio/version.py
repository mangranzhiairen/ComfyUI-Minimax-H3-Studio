"""插件版本 —— **单一来源**。

版本号只在仓库根目录的 ``VERSION`` 文件里手写一次，其余全部从它派生：

- 后端运行时：本模块读 VERSION（随 Release 包一起分发）
- 前端构建时：``web/vite.config.ts`` 读 VERSION 注入 ``__STUDIO_VERSION__``
- dev 预览 mock：``web/src/dev/mockPlugin.ts`` 读 VERSION
- 打包命名：``scripts/build_plugin.*`` 读 VERSION
- ``web/package.json`` / ``package-lock.json``：由 ``scripts/bump_version.py`` 经
  ``npm version`` 派生

升级版本请用 ``python scripts/bump_version.py <新版本>``，不要手改任何一处；
``python scripts/check_version.py`` 校验各派生物是否一致（发版前必跑）。
"""

from __future__ import annotations

from pathlib import Path

# studio/version.py → 上一级即插件根目录
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

# 读不到时的兜底值：自检只会多报一次"版本不一致"，不会让插件加载失败。
_FALLBACK = "0.0.0"


def _warn(message: str) -> None:
    """告警打印，绝不让日志问题（如非 UTF-8 控制台）反过来搞崩插件加载。"""
    try:
        print(f"[MiniMaxH3Studio] 警告: {message}")
    except Exception:  # noqa: BLE001 日志失败不得影响主流程
        pass


def read_version() -> str:
    """读取 VERSION 文件内容（去空白）。读不到时告警并返回 ``0.0.0``。

    宁可让插件照常加载（版本自检只会多报一次不一致），也不因缺文件拒绝启动；
    但**必须把原因打出来** —— Release 包若漏带 VERSION，前端版本自检会对所有用户
    误报「浏览器缓存了旧版，请强制刷新」，有这条告警才定位得到根因。
    """
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _warn(f"读取版本文件失败 {_VERSION_FILE}（{exc}），暂用 {_FALLBACK}（打包时需带上 VERSION）")
        return _FALLBACK
    if not value:
        _warn(f"版本文件为空 {_VERSION_FILE}，暂用 {_FALLBACK}")
        return _FALLBACK
    return value


# 后端接口 /minimax/studio/version 对外返回的版本
PLUGIN_VERSION = read_version()
