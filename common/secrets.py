"""配置取值:环境变量优先,否则从 AWS Secrets Manager 读取。

约定: 对配置项 FOO, 先看环境变量 ``FOO``; 没有再看 ``FOO_SECRET_ID``,
用它去 Secrets Manager 取值。SecretString 若是 JSON, 用 ``FOO_SECRET_KEY`` 指定字段。
这样 API Key / 数据库密码不用明文写进 Runtime 的环境变量。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

import boto3


@lru_cache(maxsize=32)
def _fetch_secret(secret_id: str, region: str) -> str:
    client = boto3.client("secretsmanager", region_name=region)
    return client.get_secret_value(SecretId=secret_id)["SecretString"]


def aws_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "cn-northwest-1"
    )


def get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value:
        return value
    secret_id = os.environ.get(f"{name}_SECRET_ID")
    if secret_id:
        raw = _fetch_secret(secret_id, aws_region())
        key = os.environ.get(f"{name}_SECRET_KEY")
        if key:
            return str(json.loads(raw)[key])
        return raw
    return default
