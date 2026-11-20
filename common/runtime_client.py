"""InvokeAgentRuntime 的薄封装。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config


def new_session_id() -> str:
    """runtimeSessionId 要求 >= 33 个字符。"""
    return uuid.uuid4().hex + uuid.uuid4().hex


def invoke_runtime(runtime_arn: str, prompt: str, user_id: str, session_id: Optional[str] = None,
                   region: str = "cn-northwest-1", profile: Optional[str] = None,
                   read_timeout: int = 300) -> Dict[str, Any]:
    return invoke_raw(runtime_arn, {"prompt": prompt, "user_id": user_id}, session_id, region,
                      profile, read_timeout)


def invoke_raw(runtime_arn: str, payload: Dict[str, Any], session_id: Optional[str] = None,
               region: str = "cn-northwest-1", profile: Optional[str] = None,
               read_timeout: int = 300) -> Dict[str, Any]:
    # 默认 60s 读超时对 Agent 多轮工具调用 / 评估任务不够
    cfg = Config(read_timeout=read_timeout, retries={"max_attempts": 2, "mode": "standard"})
    client = boto3.Session(region_name=region, profile_name=profile).client("bedrock-agentcore", config=cfg)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id or new_session_id(),
        payload=json.dumps(payload).encode(),
    )
    return json.loads(resp["response"].read())
