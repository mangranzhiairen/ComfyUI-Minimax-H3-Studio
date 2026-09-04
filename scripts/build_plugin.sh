#!/usr/bin/env bash
#
# MiniMax H3 创意工作台 —— Linux / macOS 一键打包脚本
#
# 用法：
#   bash scripts/build_plugin.sh            # 构建前端 + 打包 Release zip
#   bash scripts/build_plugin.sh --no-build # 跳过前端构建（复用已有 web/dist）
#
# 产物：dist_package/ComfyUI-MiniMaxH3-Studio_v{version}_{时间戳}.zip
# 内容：仅运行时必需 —— __init__.py、nodes/、studio/、requirements.txt、web/dist
# 依赖：node + npm（构建前端需要）、zip 命令（无则回退 tar.gz 并提示）
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NO_BUILD=0
if [ "${1:-}" = "--no-build" ]; then NO_BUILD=1; fi

# ---------- 读版本 ----------
if [ -f web/package.json ] && command -v node >/dev/null 2>&1; then
  VERSION="$(node -e "console.log(require('./web/package.json').version || '0.0.0')")"
else
  VERSION="0.0.0"
fi
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="$ROOT/dist_package"
ZIP="$OUT_DIR/ComfyUI-MiniMaxH3-Studio_v${VERSION}_${STAMP}.zip"
TOP="ComfyUI-MiniMaxH3-Studio"

# ---------- 构建前端（产出 web/dist） ----------
if [ "$NO_BUILD" -eq 0 ]; then
  echo "[1/2] 构建前端 (npm run build) ..."
  if [ ! -d "$ROOT/web/node_modules" ]; then
    echo "  web/node_modules 不存在，先 npm install ..."
    (cd "$ROOT/web" && npm install)
  fi
  (cd "$ROOT/web" && npm run build)
  if [ ! -f "$ROOT/web/dist/minimax-h3-studio.js" ]; then
    echo "  前端构建失败：web/dist/minimax-h3-studio.js 不存在" >&2
    exit 1
  fi
else
  if [ ! -f "$ROOT/web/dist/minimax-h3-studio.js" ]; then
    echo "  警告：web/dist/minimax-h3-studio.js 不存在，跳过构建将得到空前端" >&2
  fi
fi

# ---------- 组装运行时快照 ----------
echo "[2/2] 收集运行时文件并打包 ..."
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/$TOP"
mkdir -p "$PKG/web" "$PKG/nodes" "$PKG/studio"

cp "$ROOT/__init__.py" "$PKG/"
cp "$ROOT/requirements.txt" "$PKG/"

# nodes/ 全量（排除 __pycache__）
(cd "$ROOT" && find nodes -type f ! -path '*/__pycache__/*' | while read -r f; do
  mkdir -p "$PKG/$(dirname "$f")"; cp "$ROOT/$f" "$PKG/$f"; done)

# studio/ 全量（排除 __pycache__）
(cd "$ROOT" && find studio -type f ! -path '*/__pycache__/*' | while read -r f; do
  mkdir -p "$PKG/$(dirname "$f")"; cp "$ROOT/$f" "$PKG/$f"; done)

# 前端产物（仅 dist）
mkdir -p "$PKG/web/dist"
cp "$ROOT"/web/dist/* "$PKG/web/dist/"

# ---------- 打包 ----------
mkdir -p "$OUT_DIR"
if command -v zip >/dev/null 2>&1; then
  (cd "$STAGE" && zip -rq "$ZIP" "$TOP")
  echo "  打包完成：$ZIP"
else
  ZIP="${ZIP%.zip}.tar.gz"
  (cd "$STAGE" && tar czf "$ZIP" "$TOP")
  echo "  zip 命令不可用，已回退为 tar.gz：$ZIP"
  echo "  提示：如需 .zip 请安装 zip（如 apt install zip），或手动解压 tar.gz。"
fi

echo
echo "发布/部署说明："
echo "  - 将上方 zip 传到 GitHub Releases 附件，用户解压到 ComfyUI/custom_nodes/ 即装即用（前端已含）。"
echo "  - 依赖安装：pip install -r custom_nodes/$TOP/requirements.txt"
echo "  - 若源码变更后未重新构建，请勿发布（前端会与后端版本不符）。"
