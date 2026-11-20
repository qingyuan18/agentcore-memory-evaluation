#!/usr/bin/env python3
"""通过评估 worker Runtime 触发评估 (评估在 VPC 内执行, 本机只需 AWS 凭据)。

  # 离线: 评估可观测性里最近 24h 的会话
  python scripts/run_eval.py --worker-arn <eval_arn> sessions --runtime-id <agent_id> --since 24h --limit 10
  # 离线: 指定会话
  python scripts/run_eval.py --worker-arn <eval_arn> sessions --runtime-id <agent_id> --session-ids sid1,sid2
  # 离线: 数据集回放 (验证跨会话长期记忆)
  python scripts/run_eval.py --worker-arn <eval_arn> replay --runtime-id <agent_id> --runtime-arn <agent_arn> \
      --dataset evaluation/datasets/memory_cases.jsonl
  # 在线: 跑一轮采样评估 (定时触发见 README)
  python scripts/run_eval.py --worker-arn <eval_arn> online --runtime-id <agent_id> --idle-minutes 5
  # 自定义评估器: 列出可用评估器 / 指定评估器 / 内联传入新定义(judge、derived, 无需重新部署 worker)
  python scripts/run_eval.py --worker-arn <eval_arn> list
  python scripts/run_eval.py --worker-arn <eval_arn> --evaluators Helpfulness,MemoryRecallQuality sessions ...
  python scripts/run_eval.py --worker-arn <eval_arn> --custom-file my_evaluators.yaml --evaluators custom:* sessions ...
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.runtime_client import invoke_raw  # noqa: E402

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--worker-arn", required=True)
p.add_argument("--region", default="cn-northwest-1")
p.add_argument("--evaluators", default="", help="逗号分隔, 留空用默认集合")
p.add_argument("--json", action="store_true", help="打印完整 JSON 记录")
p.add_argument("--custom-file", default="", help="内联传入的自定义评估器定义 (YAML, 格式同 evaluation/custom_evaluators.yaml)")
sub = p.add_subparsers(dest="mode", required=True)
sub.add_parser("list", help="列出可用评估器")
s = sub.add_parser("sessions")
s.add_argument("--runtime-id", required=True)
s.add_argument("--since", default="24h")
s.add_argument("--limit", type=int, default=20)
s.add_argument("--session-ids", default="")
r = sub.add_parser("replay")
r.add_argument("--runtime-id", required=True)
r.add_argument("--runtime-arn", required=True)
r.add_argument("--dataset", required=True)
r.add_argument("--wait-s", type=int, default=420)
o = sub.add_parser("online")
o.add_argument("--runtime-id", required=True)
o.add_argument("--lookback", default="24h")
o.add_argument("--idle-minutes", type=int, default=10)
o.add_argument("--sample-rate", type=float, default=1.0)
o.add_argument("--batch", type=int, default=20)
a = p.parse_args()

payload = {"mode": "list_evaluators" if a.mode == "list" else a.mode}
if a.mode != "list":
    payload["runtime_id"] = a.runtime_id
if a.evaluators:
    payload["evaluators"] = a.evaluators.split(",")
if a.custom_file:
    import yaml  # noqa: E402

    payload["custom_evaluators"] = (yaml.safe_load(Path(a.custom_file).read_text(encoding="utf-8")) or {}).get("evaluators", [])
if a.mode == "list":
    pass
elif a.mode == "sessions":
    payload.update(since=a.since, limit=a.limit)
    if a.session_ids:
        payload["session_ids"] = a.session_ids.split(",")
elif a.mode == "replay":
    cases = [json.loads(x) for x in Path(a.dataset).read_text().splitlines() if x.strip()]
    payload.update(runtime_arn=a.runtime_arn, cases=cases, wait_s=a.wait_s)
else:
    payload.update(lookback=a.lookback, idle_minutes=a.idle_minutes, sample_rate=a.sample_rate, batch=a.batch)

# 同步调用上限 15 分钟; 大批量请拆分 (--limit / 数据集分片)
out = invoke_raw(a.worker_arn, payload, region=a.region, read_timeout=900)
if "error" in out:
    sys.exit(f"worker 返回错误: {out['error']}")
if a.mode == "list":
    for name, info in out["evaluators"].items():
        ref = " (需参考答案)" if info.get("needs_reference") else ""
        print(f"{name:<30}{info['kind']:<9}{info.get('description', '')}{ref}")
    sys.exit(0)
print(f"mode={out['mode']} 耗时={out['elapsed_s']}s 结果={out['result_location']}\n")
print(out["summary"])
if a.json:
    print(json.dumps(out["records"], ensure_ascii=False, indent=1))
else:
    for rec in out["records"]:
        key = rec.get("case_id") or rec.get("session_id", "")[:12]
        if "error" in rec:
            print(f"- [{key}] {rec['evaluator']}: ERROR {rec['error'][:200]}")
        else:
            print(f"- [{key}] {rec['evaluator']}: score={rec.get('score')} pass={rec.get('passed')} | {rec.get('reason', '')[:160]}")
