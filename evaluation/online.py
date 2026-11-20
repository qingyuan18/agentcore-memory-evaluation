"""在线评估: 持续采样线上真实会话并打分。

每个 tick:
  1. 列出最近 --lookback 内该 Runtime 的会话 (来自可观测性)
  2. 保留 "已空闲 >= --idle-minutes" 的会话 (会话结束后再评, 避免评估到一半的对话)
  3. 排除已评估过的 (状态文件), 按 --sample-rate 确定性采样 (sha256(session_id), 重跑结果一致)
  4. 逐个打分, 写结果, 更新状态
用法:
  python -m evaluation.online --runtime-id <id> --once            # 跑一轮 (适合 cron / EventBridge 触发)
  python -m evaluation.online --runtime-id <id> --interval 300    # 常驻轮询
"""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from .evaluators import DEFAULT_ONLINE, build_evaluators
from .offline import parse_duration
from .report import summarize
from .runner import evaluate_session
from .spans import list_sessions, make_provider
from .store import ResultStore


def sampled(session_id: str, rate: float) -> bool:
    bucket = int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def tick(runtime_id: str, region: str, store: ResultStore, evaluators, *, lookback: timedelta,
         idle: timedelta, rate: float, batch: int) -> list:
    now = datetime.now(timezone.utc)
    done = store.load_evaluated(runtime_id)
    candidates = [
        s for s in list_sessions(runtime_id, region, lookback)
        if now - s.last_seen >= idle and s.session_id not in done and sampled(s.session_id, rate)
    ][:batch]
    if not candidates:
        print("本轮无待评估会话")
        return []
    provider = make_provider(runtime_id, region, lookback_days=max(1, lookback.days + 1))
    records, finished = [], set()
    for s in candidates:
        recs = evaluate_session(provider, s.session_id, evaluators, mode="online", runtime_id=runtime_id,
                                extra={"last_seen": s.last_seen.isoformat()})
        records += recs
        # 取不到 trace (入库延迟) 的会话不标记完成, 下一轮重试; 其余(含个别评估器报错)视为已评估
        if not any(r["evaluator"] == "_fetch" for r in recs):
            finished.add(s.session_id)
    store.write("online", records)
    store.save_evaluated(runtime_id, done | finished)
    print(f"评估 {len(finished)}/{len(candidates)} 个会话")
    print(summarize(records))
    return records


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runtime-id", default=os.environ.get("RUNTIME_ID"), required=not os.environ.get("RUNTIME_ID"))
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    p.add_argument("--evaluators", default=",".join(DEFAULT_ONLINE))
    p.add_argument("--lookback", type=parse_duration, default=parse_duration("24h"))
    p.add_argument("--idle-minutes", type=int, default=10)
    p.add_argument("--sample-rate", type=float, default=1.0)
    p.add_argument("--batch", type=int, default=20)
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--once", action="store_true")
    a = p.parse_args()

    store = ResultStore(a.region)
    evaluators = build_evaluators(a.evaluators.split(","))
    while True:
        tick(a.runtime_id, a.region, store, evaluators, lookback=a.lookback,
             idle=timedelta(minutes=a.idle_minutes), rate=a.sample_rate, batch=a.batch)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
