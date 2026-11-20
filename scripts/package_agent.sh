#!/usr/bin/env bash
# 打包 AgentCore Runtime 代码 zip (linux/aarch64, Python 3.13)。
#   ./scripts/package_agent.sh          # 被测 Agent  -> build/agent.zip
#   ./scripts/package_agent.sh eval     # 评估 worker -> build/eval.zip
# 国内镜像: PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ./scripts/package_agent.sh
set -euo pipefail
cd "$(dirname "$0")/.."

KIND="${1:-agent}"
case "$KIND" in
  agent) REQ=requirements-agent.txt; ENTRY=runtime_main.py; SRC=(agent common) ;;
  eval)  REQ=requirements-eval.txt;  ENTRY=eval_main.py;    SRC=(evaluation common) ;;
  *) echo "用法: $0 [agent|eval]"; exit 1 ;;
esac

OUT="build/$KIND"
rm -rf "$OUT" "build/$KIND.zip"
mkdir -p "$OUT"

python3 -m pip install \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
  --implementation cp --python-version 3.13 --only-binary=:all: \
  --target "$OUT" -r "$REQ"

cp -r "${SRC[@]}" "$OUT/"
cp "$ENTRY" "$OUT/main.py"
find "$OUT" -name "__pycache__" -type d -prune -exec rm -rf {} +

(cd "$OUT" && zip -qr "../$KIND.zip" .)
du -sh "$OUT" | awk '{print "解压后大小: "$1" (上限 250MB, 超出请改用容器部署)"}'
ls -lh "build/$KIND.zip"
