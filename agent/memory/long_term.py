"""长期记忆: Mem0 的薄封装 (add / search / delete)。"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Union

# 必须在 import mem0 之前设置: Runtime 里 $HOME 不可写, 关闭遥测
os.environ.setdefault("MEM0_DIR", "/tmp/.mem0")
os.environ.setdefault("MEM0_TELEMETRY", "false")
logging.getLogger("mem0.memory.entity_extractor").setLevel(logging.CRITICAL)
logging.getLogger("mem0.llms.aws_bedrock").setLevel(logging.ERROR)

from mem0 import Memory  # noqa: E402

from .config import build_mem0_config  # noqa: E402


class LongTermMemory:
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._memory = Memory.from_config(config or build_mem0_config())

    def add(self, messages: Union[str, List[Dict[str, str]]], user_id: str,
            agent_id: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """messages 可以是一段文本, 也可以是 [{"role", "content"}] 对话; Mem0 用 LLM 抽取事实并去重。"""
        return self._memory.add(
            messages=messages, user_id=user_id, agent_id=agent_id, metadata=metadata or {}
        )

    def search(self, query: str, user_id: str, agent_id: Optional[str] = None,
               limit: int = 5) -> List[Dict[str, Any]]:
        # Mem0 v2: user_id / agent_id 通过 filters 传入; threshold=0 不做机器过滤,
        # 交给 LLM 判断相关性 (跨语言 / 融合评分下默认阈值常误杀有效记忆)
        filters: Dict[str, Any] = {"user_id": user_id}
        if agent_id:
            filters["agent_id"] = agent_id
        result = self._memory.search(query=query, filters=filters, limit=limit, threshold=0.0)
        return result.get("results", []) if isinstance(result, dict) else (result or [])

    def delete(self, memory_id: str) -> None:
        self._memory.delete(memory_id=memory_id)
