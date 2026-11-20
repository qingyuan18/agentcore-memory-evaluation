"""评估 worker: 把离线/在线评估作为一个 AgentCore Runtime 运行。

为什么要单独部署成 Runtime:
  - 裁判模型端点通常只在 VPC 内可达; worker 与被测 Agent 同 VPC, 走 NAT 访问
  - 评估所需权限(读 CloudWatch、调用被测 Runtime、写 S3)收敛在 worker 的执行角色上
  - 触发方式统一为 InvokeAgentRuntime: 本机脚本、Lambda、定时任务都能调

payload:
  {"mode": "sessions", "runtime_id": "...", "since": "24h", "limit": 20, "session_ids": [...], "evaluators": [...]}
  {"mode": "replay",   "runtime_id": "...", "runtime_arn": "...", "cases": [ {...}, ... ], "evaluators": [...]}
  {"mode": "online",   "runtime_id": "...", "lookback": "24h", "idle_minutes": 10, "sample_rate": 1.0, "batch": 20}
  {"mode": "list_evaluators"}                                   列出可用的内置/自定义评估器
任一 mode 都可带 "custom_evaluators": [...] 内联自定义评估器定义 (仅 judge / derived, 见 custom.py),
在 "evaluators" 里按名称引用, 或写 "custom:*" 启用全部自定义评估器。
返回: 汇总表 + 结果文件位置 + 精简后的记录 (reason 截断)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta
from typing import Any, Dict, List

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from common.secrets import aws_region

from .evaluators import DEFAULT_ONLINE, build_evaluators, catalog
from .offline import parse_duration, run_replay, run_sessions
from .online import tick
from .report import summarize
from .store import ResultStore

logger = logging.getLogger("eval-worker")
app = BedrockAgentCoreApp()


def _brief(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep = ("session_id", "case_id", "evaluator", "score", "passed", "label", "error", "reply")
    out = []
    for r in records:
        item = {k: r[k] for k in keep if k in r and r[k] is not None}
        if r.get("reason"):
            item["reason"] = str(r["reason"])[:300]
        if isinstance(item.get("reply"), str):
            item["reply"] = item["reply"][:200]
        out.append(item)
    return out


@app.entrypoint
def handle(payload: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    region = payload.get("region") or aws_region()
    mode = payload.get("mode", "sessions")
    custom = payload.get("custom_evaluators") or None
    if mode == "list_evaluators":
        try:
            items = catalog(custom)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"mode": mode, "evaluators": {n: {k: v for k, v in i.items() if k != "definition"}
                                             for n, i in items.items()}}
    runtime_id = payload.get("runtime_id") or os.environ.get("RUNTIME_ID")
    if not runtime_id:
        return {"error": "缺少 runtime_id"}
    store = ResultStore(region)
    started = time.time()
    try:
        return _run(mode, payload, runtime_id, region, store, started, custom)
    except ValueError as exc:  # 评估器定义 / 参数错误, 直接返回给调用方
        return {"error": str(exc)}


def _run(mode, payload, runtime_id, region, store, started, custom) -> Dict[str, Any]:

    if mode == "sessions":
        names = payload.get("evaluators") or DEFAULT_ONLINE
        records = run_sessions(runtime_id, region, names, since=parse_duration(payload.get("since", "24h")),
                               limit=int(payload.get("limit", 20)), session_ids=payload.get("session_ids"),
                               custom=custom)
        where = store.write("offline-sessions", records)
    elif mode == "replay":
        names = payload.get("evaluators") or DEFAULT_ONLINE + ["Correctness"]
        records = run_replay(runtime_id, payload["runtime_arn"], region, payload["cases"], names,
                             wait_s=int(payload.get("wait_s", 420)), custom=custom)
        where = store.write("offline-replay", records)
    elif mode == "online":
        names = payload.get("evaluators") or DEFAULT_ONLINE
        records = tick(runtime_id, region, store, build_evaluators(names, inline=custom),
                       lookback=parse_duration(payload.get("lookback", "24h")),
                       idle=timedelta(minutes=int(payload.get("idle_minutes", 10))),
                       rate=float(payload.get("sample_rate", 1.0)), batch=int(payload.get("batch", 20)))
        where = "见 online 结果目录"
    else:
        return {"error": f"未知 mode: {mode}"}

    return {
        "mode": mode, "runtime_id": runtime_id, "elapsed_s": round(time.time() - started, 1),
        "result_location": where, "summary": summarize(records), "records": _brief(records),
    }


if __name__ == "__main__":
    app.run()
