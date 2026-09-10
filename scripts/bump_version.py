"""版本号升级 —— 写入单一来源，并派生 npm 侧元数据。

用法::

    python scripts/bump_version.py 0.1.2

本脚本只改两类文件：

1. 仓库根 ``VERSION`` —— **唯一需要手写版本号的地方**（本脚本之外不要手改）
2. ``web/package.json`` 与 ``web/package-lock.json`` 的 version 字段（npm 元数据，派生物）

后端 ``studio/version.py``、前端 ``web/vite.config.ts``、dev mock
``web/src/dev/mockPlugin.ts``、打包脚本 ``scripts/build_plugin.*`` 都从 ``VERSION``
读取，无需在此处理。

关于为什么不用 ``npm version``：本次只改 version 字段，正则定点替换与 npm 的行为
等价，且不引入子进程、Windows ``.cmd`` 调用、沙箱 spawn 等额外失败面。
**依赖的增删仍应由 npm 管理**（``npm install`` 会重写 lock，不影响本脚本）。
脚本末尾会自动调用 ``check_version.py``，任何不一致都会非零退出。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 中文 Windows 控制台是 GBK：无法编码的字符会让打印直接抛异常、把脚本搞崩。
# 降级为 replace，保证诊断信息一定打得出来。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except (ValueError, OSError):
        pass

VERSION_FILE = ROOT / "VERSION"
PKG_JSON = ROOT / "web" / "package.json"
PKG_LOCK = ROOT / "web" / "package-lock.json"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# "version": "x" —— 定点替换值，保留原文件其余字节与换行风格
_VERSION_FIELD_RE = re.compile(r'("version"\s*:\s*")[^"]*(")')

# package.json 只有根 version；package-lock.json 前两处依次是根与 packages[""]
_PATCH_TARGETS: tuple[tuple[Path, int, str], ...] = (
    (PKG_JSON, 1, 'web/package.json 根 version'),
    (PKG_LOCK, 2, 'web/package-lock.json 根 + packages[""] version'),
)


def patch_json_version(path: Path, version: str, expected_count: int, label: str) -> None:
    """定点替换前 ``expected_count`` 处 version 值，原样保留其余内容。"""
    with path.open("r", encoding="utf-8", newline="") as fh:
        text = fh.read()

    replaced = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal replaced
        replaced += 1
        return f"{match.group(1)}{version}{match.group(2)}"

    new_text = _VERSION_FIELD_RE.sub(_sub, text, count=expected_count)
    if replaced != expected_count:
        raise RuntimeError(
            f"{path.relative_to(ROOT)}：预期替换 {expected_count} 处 version，"
            f"实际 {replaced} 处 —— 文件结构可能已变化，请人工确认"
        )

    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    print(f"  - {label} → {version}（{replaced} 处）")


def write_root_version(version: str) -> None:
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8", newline="")
    print(f"  - VERSION → {version}（单一来源）")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        print("错误：需要且仅需要一个版本号参数，例如：python scripts/bump_version.py 0.1.2")
        return 2

    version = argv[1].strip()
    if not SEMVER_RE.match(version):
        print(f"错误：{version!r} 不是合法的语义化版本号（示例：0.1.2 / 1.0.0-rc.1）")
        return 2

    current = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.is_file() else None
    print(f"版本升级：{current or '(无)'} → {version}")

    try:
        for path, count, label in _PATCH_TARGETS:
            patch_json_version(path, version, count, label)
        # 单一来源最后写：只有前面全部成功，基准值才变更
        write_root_version(version)
    except (OSError, RuntimeError) as exc:
        print(f"\n升级失败：{exc}")
        print("部分文件可能已改动，请跑 python scripts/check_version.py 查看现状。")
        return 1

    print("\n校验一致性：")
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_version  # noqa: E402 同目录脚本，导入即用其 main

    # bump 只保证「声明」一致；刚改完版本号、前端还没重建属正常中间态，
    # 由发布门禁（直接运行 check_version.py）去卡住 --no-build 发出错包。
    check_version.REQUIRE_DIST = False

    if check_version.main() != 0:
        print("\n升级后校验未通过，请检查上面的 [FAIL] 项。")
        return 1
    print("\n升级完成。接着重新构建前端并打包：scripts\\build_plugin.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
