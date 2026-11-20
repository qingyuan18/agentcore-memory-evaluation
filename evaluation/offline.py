"""离线评估。

两种数据来源:
  1) sessions: 从可观测性 (CloudWatch: Runtime 日志组 + aws/spans) 拉取历史会话批量打分
  2) replay:   用数据集(带参考答案)回放调用被测 Runtime, 再读取这些调用的 trace 打分

数据集每行(JSONL): {"id": "...", "setup": ["先告诉 Agent 的信息", ...], "input": "考察问题", "expected_output": "参考答案"}
setup 的每一句在**独立会话**里发送, input 在**另一个新会话**里发送, 因此只有长期记忆(Mem0)正常工作才能答对。

运行方式:
  - 推荐: 通过评估 worker Runtime 运行 (见 scripts/run_eval.py), 评估在 VPC 内执行, 可访问裁判模型
  - 本机直接运行 (需要本机能访问裁判模型端点):
      python -m evaluation.offline sessions --runtime-id <id> --since 24h
      python -m evaluation.offline replay --runtime-id <id> --runtime-arn <arn> --dataset evaluation/datasets/memory_cases.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional

from common.runtime_client import invoke_runtime, new_session_id

from .evaluators import DEFAULT_ONLINE, build_evaluators
from .report import summarize
from .runner import evaluate_session
from .spans import list_sessions, make_provider
from .store import ResultStore


def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", text)
    if not m:
        raise ValueError("时长格式如 30m / 24h / 7d")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n})


def run_sessions(runtime_id: str, region: str, evaluator_names: List[str], *,
                 since: timedelta = timedelta(hours=24), limit: int = 50,
                 session_ids: Optional[List[str]] = None, workers: int = 4,
                 custom: Optional[List[Dict]] = None) -> List[Dict]:
    provider = make_provider(runtime_id, region, lookback_days=max(1, since.days + 1))
    ids = session_ids or [s.session_id for s in list_sessions(runtime_id, region, since, limit=limit)]
    evaluators = build_evaluators(evaluator_names, inline=custom)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = pool.map(
            lambda sid: evaluate_session(provider, sid, evaluators, mode="offline-sessions", runtime_id=runtime_id),
            ids,
        )
    return [r for batch in batches for r in batch]


def run_replay(runtime_id: str, runtime_arn: str, region: str, cases: List[Dict],
               evaluator_names: List[str], *, wait_s: int = 420, workers: int = 4,
               custom: Optional[List[Dict]] = None) -> List[Dict]:
    """先把所有用例调用完, 再统一等待 trace 入库并打分 (总耗时 ≈ 最慢的一次入库延迟, 而不是累加)。"""
    evaluators = build_evaluators(evaluator_names, has_reference=True, inline=custom)  # 先校验, 定义有误时不去调用被测 Agent
    run_id = uuid.uuid4().hex[:8]

    def play(case: Dict) -> Dict:
        user = f"eval-{run_id}-{case['id']}"   # 每个用例一个独立用户, 记忆互不污染
        for utterance in case.get("setup", []):
            invoke_runtime(runtime_arn, utterance, user, new_session_id(), region)
        sid = new_session_id()
        reply = invoke_runtime(runtime_arn, case["input"], user, sid, region)
        return {"case": case, "user": user, "session_id": sid, "reply": reply.get("result")}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        played = list(pool.map(play, cases))

    provider = make_provider(runtime_id, region)

    def score(p: Dict) -> List[Dict]:
        return evaluate_session(
            provider, p["session_id"], evaluators, mode="offline-replay", runtime_id=runtime_id,
            expected_output=p["case"].get("expected_output"), wait_s=wait_s,
            extra={"case_id": p["case"]["id"], "user_id": p["user"], "reply": p["reply"]},
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        batches = pool.map(score, played)
    return [r for batch in batches for r in batch]


def load_cases(path: str) -> List[Dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("sessions", "replay"):
        sp = sub.add_parser(name)
        sp.add_argument("--runtime-id", default=os.environ.get("RUNTIME_ID"), required=not os.environ.get("RUNTIME_ID"))
        sp.add_argument("--evaluators", default=",".join(DEFAULT_ONLINE + (["Correctness"] if name == "replay" else [])))
    s = sub.choices["sessions"]
    s.add_argument("--since", default="24h")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--session-ids", default="")
    r = sub.choices["replay"]
    r.add_argument("--dataset", required=True)
    r.add_argument("--runtime-arn", default=os.environ.get("RUNTIME_ARN"))
    r.add_argument("--wait", type=int, default=420, help="等待 trace 入库的最长秒数")
    a = p.parse_args()

    names = a.evaluators.split(",")
    if a.cmd == "sessions":
        records = run_sessions(a.runtime_id, a.region, names, since=parse_duration(a.since), limit=a.limit,
                               session_ids=[x for x in a.session_ids.split(",") if x])
    else:
        records = run_replay(a.runtime_id, a.runtime_arn, a.region, load_cases(a.dataset), names, wait_s=a.wait)
    where = ResultStore(a.region).write(f"offline-{a.cmd}", records)
    print(summarize(records))
    print("结果:", where)


if __name__ == "__main__":
    main()
