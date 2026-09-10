"""版本一致性校验 —— 确认所有派生物都与仓库根 ``VERSION`` 一致。

用法::

    python scripts/check_version.py

发版前必跑；任何不一致都会以非零退出码失败并指出具体位置。

校验项：
1. ``VERSION`` 存在、单行、符合语义化版本格式
2. ``web/package.json`` 的 version
3. ``web/package-lock.json`` 的两处 version（根 + ``packages[""]``）
4. 消费方没有残留硬编码字面量（应改为读 VERSION）
5. 若 ``web/dist`` 已构建，bundle 内注入的版本应为当前 VERSION（抓「改了没重新构建」）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 中文 Windows 控制台是 GBK，打印 {✓} 这类符号会抛 UnicodeEncodeError 把脚本搞崩
# （诊断信息全丢）。降级为 replace：中文仍正常显示，无法编码的字符变 "?"。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except (ValueError, OSError):
        pass

VERSION_FILE = ROOT / "VERSION"
PKG_JSON = ROOT / "web" / "package.json"
PKG_LOCK = ROOT / "web" / "package-lock.json"
DIST_JS = ROOT / "web" / "dist" / "minimax-h3-studio.js"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# 是否要求已构建的前端 bundle 与 VERSION 一致。
# - 直接运行本脚本（发布门禁）：True —— 防止 `build_plugin --no-build` 发出
#   前后端版本不一致的包。
# - bump_version.py 调用时置 False：刚改完版本号、还没重新构建属于正常中间态，
#   声明的一致性（VERSION / package.json / lock / 无硬编码）才是 bump 必须保证的。
REQUIRE_DIST = True

# 这些文件应当从 VERSION 读取，不得再出现硬编码版本字面量
NO_LITERAL_CHECKS: tuple[tuple[Path, re.Pattern[str], str], ...] = (
    (
        ROOT / "studio" / "http_routes.py",
        re.compile(r'PLUGIN_VERSION\s*=\s*["\']'),
        "后端应 `from .version import PLUGIN_VERSION`，不要再赋值字面量",
    ),
    (
        ROOT / "web" / "src" / "dev" / "mockPlugin.ts",
        re.compile(r'version:\s*["\']\d+\.\d+\.\d+["\']'),
        "dev mock 应读 VERSION 文件，不要再硬编码版本",
    ),
)


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, msg: str) -> None:
        print(f"  [OK]   {msg}")

    def bad(self, msg: str) -> None:
        self.failed += 1
        print(f"  [FAIL] {msg}")


def read_root_version(rep: Report) -> str | None:
    if not VERSION_FILE.is_file():
        rep.bad(f"缺少版本源文件：{VERSION_FILE.relative_to(ROOT)}")
        return None
    raw = VERSION_FILE.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) != 1:
        rep.bad(f"VERSION 应为单行，实际 {len(lines)} 行非空内容")
        return None
    version = lines[0].strip()
    if not SEMVER_RE.match(version):
        rep.bad(f"VERSION 内容不是语义化版本：{version!r}")
        return None
    rep.ok(f"VERSION = {version}")
    return version


def _load_json(path: Path, rep: Report) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        rep.bad(f"读取失败 {path.relative_to(ROOT)}：{exc}")
    except json.JSONDecodeError as exc:
        rep.bad(f"JSON 解析失败 {path.relative_to(ROOT)}：{exc}")
    return None


def check_package_json(version: str, rep: Report) -> None:
    rel = PKG_JSON.relative_to(ROOT)
    data = _load_json(PKG_JSON, rep)
    if data is None:
        return
    if data.get("version") == version:
        rep.ok(f"{rel} version = {version}")
    else:
        rep.bad(f"{rel} version = {data.get('version')!r}，应为 {version!r}")


def check_package_lock(version: str, rep: Report) -> None:
    rel = PKG_LOCK.relative_to(ROOT)
    data = _load_json(PKG_LOCK, rep)
    if data is None:
        return
    root_v = data.get("version")
    pkg_v = (data.get("packages") or {}).get("", {}).get("version")
    for label, actual in (("根 version", root_v), ('packages[""] version', pkg_v)):
        if actual == version:
            rep.ok(f"{rel} {label} = {version}")
        else:
            rep.bad(f"{rel} {label} = {actual!r}，应为 {version!r}")


def check_no_literals(rep: Report) -> None:
    for path, pattern, hint in NO_LITERAL_CHECKS:
        rel = path.relative_to(ROOT)
        if not path.is_file():
            rep.bad(f"缺少文件 {rel}")
            continue
        match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        if match:
            rep.bad(f"{rel} 残留硬编码版本 {match.group(0)!r}：{hint}")
        else:
            rep.ok(f"{rel} 无硬编码版本")


def check_dist_bundle(version: str, rep: Report) -> None:
    rel = DIST_JS.relative_to(ROOT)
    if not DIST_JS.is_file():
        print(f"  [SKIP] {rel} 不存在（未构建前端，跳过）")
        return
    if version in DIST_JS.read_text(encoding="utf-8", errors="replace"):
        rep.ok(f"{rel} 已注入版本 {version}")
    elif REQUIRE_DIST:
        rep.bad(f"{rel} 内未找到版本 {version}：需重新构建前端（npm run build）后再发版")
    else:
        print(f"  [WARN] {rel} 版本尚未同步：重新构建前端后即可（npm run build）")


def main() -> int:
    print(f"版本一致性校验（基准：{VERSION_FILE.relative_to(ROOT)}）")
    rep = Report()

    version = read_root_version(rep)
    if version is None:
        print(f"\n结果：失败（{rep.failed} 项）—— 版本源不可用，后续校验跳过")
        return 1

    check_package_json(version, rep)
    check_package_lock(version, rep)
    check_no_literals(rep)
    check_dist_bundle(version, rep)

    if rep.failed:
        print(f"\n结果：失败（{rep.failed} 项）。升版本请用：python scripts/bump_version.py <版本>")
        return 1
    print("\n结果：全部一致 (OK)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
