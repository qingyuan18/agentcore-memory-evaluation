"""从 AgentCore 可观测性(CloudWatch Logs)读取会话数据。

AgentCore Runtime + ADOT 在中国区的数据落点:
  - /aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>  应用日志 + 带 body.input/output.messages 的 OTEL 事件记录
  - aws/spans                                              Transaction Search 开通后的 span (账号/区域内所有 Agent 共用)
评估只需要 "按 session.id 取回一次会话完整的 trace", 直接复用 strands-agents-evals 的
CloudWatchProvider (boto3 按 region 自动走 cn 分区端点), 这里只补充 "列出会话" 的能力。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import boto3
from strands_evals.providers import CloudWatchProvider
from strands_evals.providers.exceptions import SessionNotFoundError
from strands_evals.types.trace import AgentInvocationSpan, Session


def runtime_log_group(runtime_id: str, endpoint: str = "DEFAULT") -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}"


@dataclass
class SessionInfo:
    session_id: str
    records: int
    first_seen: datetime
    last_seen: datetime


def _logs_client(region: str, profile: Optional[str] = None):
    return boto3.Session(region_name=region, profile_name=profile).client("logs")


def _run_query(logs, groups: List[str], query: str, start: datetime, end: datetime,
               timeout_s: int = 90) -> List[dict]:
    qid = logs.start_query(
        logGroupNames=groups,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
    )["queryId"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        res = logs.get_query_results(queryId=qid)
        if res["status"] == "Complete":
            return [{f["field"]: f["value"] for f in row} for row in res["results"]]
        if res["status"] in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Logs Insights query {res['status']}")
        time.sleep(1.5)
    raise TimeoutError("Logs Insights query timed out")


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def list_sessions(runtime_id: str, region: str, since: timedelta, until: Optional[datetime] = None,
                  profile: Optional[str] = None, limit: int = 200) -> List[SessionInfo]:
    """列出时间窗口内该 Runtime 出现过的会话(按最后活动时间倒序)。"""
    logs = _logs_client(region, profile)
    end = until or datetime.now(timezone.utc)
    rows = _run_query(
        logs,
        [runtime_log_group(runtime_id)],
        "filter ispresent(attributes.session.id) "
        "| stats count(*) as n, min(@timestamp) as first, max(@timestamp) as last by attributes.session.id as sid "
        f"| sort last desc | limit {int(limit)}",
        end - since,
        end,
    )
    return [
        SessionInfo(r["sid"], int(r["n"]), _parse_ts(r["first"]), _parse_ts(r["last"]))
        for r in rows if r.get("sid")
    ]


def make_provider(runtime_id: str, region: str, lookback_days: int = 1) -> CloudWatchProvider:
    return CloudWatchProvider(
        region=region, log_group=runtime_log_group(runtime_id), lookback_days=lookback_days
    )


def fetch_session(provider: CloudWatchProvider, session_id: str, wait_s: int = 0) -> dict:
    """取回会话。wait_s>0 时轮询等待日志入库(刚调用完的会话通常要 1~3 分钟才可查)。"""
    deadline = time.time() + wait_s
    while True:
        try:
            return provider.get_evaluation_data(session_id)
        except SessionNotFoundError:
            if time.time() >= deadline:
                raise
            time.sleep(15)


def first_user_prompt(session: Session) -> str:
    for trace in session.traces:
        for span in trace.spans:
            if isinstance(span, AgentInvocationSpan) and span.user_prompt:
                return str(span.user_prompt)
    return ""
