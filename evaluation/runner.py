"""对单个会话打分: 从可观测性取回 trace -> 构造 EvaluationData -> 逐个评估器调用裁判模型。"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from strands_evals.types.evaluation import EvaluationData

from .spans import fetch_session, first_user_prompt

# 裁判调用偶发限流/网关错误: 简单退避重试
_RETRY_DELAYS = (2, 5, 15)


def _evaluate_with_retry(evaluator, case: EvaluationData):
    for i, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            return evaluator.evaluate(case)
        except Exception:
            if delay is None:
                raise
            time.sleep(delay)


def evaluate_session(
    provider,
    session_id: str,
    evaluators: List,
    *,
    mode: str,
    runtime_id: str,
    expected_output: Optional[str] = None,
    wait_s: int = 0,
    extra: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """返回每个 (评估器 x 输出) 一条记录; 单个评估器失败只产生一条 error 记录, 不影响其余。"""
    now = datetime.now(timezone.utc).isoformat()
    base = {"session_id": session_id, "runtime_id": runtime_id, "mode": mode, "evaluated_at": now,
            **(extra or {})}
    try:
        data = fetch_session(provider, session_id, wait_s=wait_s)
    except Exception as exc:  # 取不到 trace: 记一条 error, 由调用方决定是否重试
        return [{**base, "evaluator": "_fetch", "error": f"{type(exc).__name__}: {exc}"}]

    case = EvaluationData(
        input=first_user_prompt(data["trajectory"]),
        actual_output=data["output"],
        expected_output=expected_output,
        actual_trajectory=data["trajectory"],
        name=session_id,
    )
    records: List[Dict[str, Any]] = []
    for ev in evaluators:
        name = getattr(ev, "name", None) or type(ev).__name__
        try:
            for out in _evaluate_with_retry(ev, case):
                records.append({
                    **base, "evaluator": name, "score": out.score, "passed": out.test_pass,
                    "label": out.label, "reason": out.reason,
                })
        except Exception as exc:
            records.append({**base, "evaluator": name, "error": f"{type(exc).__name__}: {exc}",
                            "trace": traceback.format_exc(limit=3)})
    return records
