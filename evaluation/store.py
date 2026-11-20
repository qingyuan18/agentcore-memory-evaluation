"""结果存储 + 在线评估的 "已评估会话" 状态。

- 配置 EVAL_RESULTS_BUCKET: 结果写 S3 (results/<mode>/<date>/<ts>-<n>.jsonl), 状态写 S3
- 否则写本地 eval_output/
在线评估靠状态文件去重 (同一个会话只评一次); 记录以 JSON Lines 保存, 便于 Athena / 脚本分析。
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Set

import boto3


class ResultStore:
    def __init__(self, region: str, bucket: str | None = None, prefix: str = "eval", local_dir: str = "eval_output"):
        self.bucket = bucket or os.environ.get("EVAL_RESULTS_BUCKET")
        self.prefix = prefix.strip("/")
        self.local = Path(local_dir)
        self._s3 = boto3.Session(region_name=region).client("s3") if self.bucket else None

    # ---- 结果 ----
    def write(self, mode: str, records: List[Dict]) -> str:
        if not records:
            return ""
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records) + "\n"
        now = datetime.now(timezone.utc)
        name = f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}.jsonl"
        rel = f"results/{mode}/{now:%Y-%m-%d}/{name}"
        if self._s3:
            key = f"{self.prefix}/{rel}"
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode())
            return f"s3://{self.bucket}/{key}"
        path = self.local / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return str(path)

    # ---- 在线评估状态 ----
    def _state_key(self, runtime_id: str) -> str:
        return f"{self.prefix}/state/{runtime_id}.json"

    def load_evaluated(self, runtime_id: str) -> Set[str]:
        try:
            if self._s3:
                obj = self._s3.get_object(Bucket=self.bucket, Key=self._state_key(runtime_id))
                return set(json.loads(obj["Body"].read()))
            return set(json.loads((self.local / "state" / f"{runtime_id}.json").read_text()))
        except Exception:  # 首次运行: 无状态文件
            return set()

    def save_evaluated(self, runtime_id: str, session_ids: Iterable[str]) -> None:
        data = json.dumps(sorted(set(session_ids)))
        if self._s3:
            self._s3.put_object(Bucket=self.bucket, Key=self._state_key(runtime_id), Body=data.encode())
            return
        path = self.local / "state" / f"{runtime_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
