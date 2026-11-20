#!/usr/bin/env bash
# 快速打包评估 worker: 复用已打好的 Agent 依赖目录 (build/agent), 去掉 Mem0/pgvector 相关包,
# 只补装评估额外需要的纯 Python 包。适合 pip 镜像很慢的环境; 正常情况用 ./scripts/package_agent.sh eval。
set -euo pipefail
cd "$(dirname "$0")/.."
shopt -s nullglob

BASE="${1:-build/agent}"
[ -d "$BASE" ] || { echo "先运行 ./scripts/package_agent.sh 生成 $BASE"; exit 1; }
OUT=build/eval
rm -rf "$OUT" build/eval.zip
cp -R "$BASE" "$OUT"
rm -rf "$OUT"/agent "$OUT"/common "$OUT"/main.py \
       "$OUT"/mem0 "$OUT"/mem0ai-* "$OUT"/qdrant_client "$OUT"/qdrant_client-* \
       "$OUT"/grpc "$OUT"/grpcio-* "$OUT"/posthog "$OUT"/posthog-* \
       "$OUT"/psycopg "$OUT"/psycopg-* "$OUT"/psycopg_binary "$OUT"/psycopg_binary-* \
       "$OUT"/psycopg_pool "$OUT"/psycopg_pool-* "$OUT"/sqlalchemy "$OUT"/SQLAlchemy-* "$OUT"/sqlalchemy-*

# 与已有 opentelemetry-instrumentation 版本保持一致
OTEL_INSTR=$(ls "$OUT" | sed -n 's/^opentelemetry_instrumentation-\(.*\)\.dist-info$/\1/p' | head -1)
python3 -m pip install -q --no-deps --target "$OUT" \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --only-binary=:all: \
  "strands-agents-evals==1.5.0" rich markdown-it-py mdurl pygments tenacity python-dotenv \
  "opentelemetry-instrumentation-threading==${OTEL_INSTR}"

cp -r evaluation common "$OUT/"
cp eval_main.py "$OUT/main.py"
find "$OUT" -name "__pycache__" -type d -prune -exec rm -rf {} +
(cd "$OUT" && zip -qr ../eval.zip .)
du -sh "$OUT" | awk '{print "解压后大小: "$1}'
ls -lh build/eval.zip
