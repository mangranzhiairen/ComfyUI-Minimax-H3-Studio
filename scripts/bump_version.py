"""版本号升级 —— 写入唯一来源。

用法::

    python scripts/bump_version.py 0.1.2

本脚本只改一个文件：

1. 仓库根 VERSION —— 唯一需要手写版本号的地方（本脚本之外不要手改）

后端 studio/version.py、前端 web/vite.config.ts、dev mock
web/src/dev/mockPlugin.ts、打包脚本 scripts/build_plugin.* 都从 VERSION
读取，无需在此处理。web/package.json 不声明 version（见 check_version.py），
npm 侧也没有需要同步的版本字段。

脚本末尾会自动调用 check_version.py，任何不一致都会非零退出。
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

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def write_root_version(version: str) -> None:
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8", newline="")
    print(f"  - VERSION -> {version}（唯一来源）")


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
    print(f"版本升级：{current or '(无)'} -> {version}")

    try:
        write_root_version(version)
    except OSError as exc:
        print(f"\n升级失败：{exc}")
        return 1

    print("\n校验一致性：")
    sys.path.insert(0, str(ROOT / "scripts"))
    import check_version  # noqa: E402 同目录脚本，导入即用其 main

    # bump 只保证声明一致；刚改完版本号、前端还没重建属正常中间态，
    # 由发布门禁（直接运行 check_version.py）去卡住 --no-build 发出错包。
    check_version.REQUIRE_DIST = False

    if check_version.main() != 0:
        print("\n升级后校验未通过，请检查上面的 [FAIL] 项。")
        return 1
    print("\n升级完成。接着重新构建前端并打包：scripts/build_plugin.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
