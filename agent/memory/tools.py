"""把 Mem0 长期记忆暴露为 Strands 工具: remember / recall / forget。

user_id 通过闭包绑定, 模型无法越权读写其他用户的记忆。
"""
from __future__ import annotations

from typing import List, Optional

from strands import tool

from .long_term import LongTermMemory


def build_memory_tools(ltm: LongTermMemory, user_id: str, agent_id: Optional[str] = None,
                       include_remember: bool = True) -> List:
    """include_remember=False 时不提供 remember (自动记忆模式下由入口统一写入, 避免重复)。"""

    @tool
    def remember(content: str) -> str:
        """把关于当前用户的长期有效信息(偏好、身份、约定、事实)保存到长期记忆。

        Args:
            content: 需要记住的原始文本。
        """
        result = ltm.add(content, user_id=user_id, agent_id=agent_id)
        events = result.get("results", []) if isinstance(result, dict) else []
        if not events:
            return "已提交长期记忆(未抽取到新的事实)。"
        return "已写入: " + "; ".join(e.get("memory", "") for e in events if e.get("memory"))

    @tool
    def recall(query: str, limit: int = 5) -> str:
        """按语义检索当前用户的长期记忆,用于回答涉及历史信息或个性化的问题。

        Args:
            query: 自然语言问题或关键词。
            limit: 返回条数上限。
        """
        hits = ltm.search(query, user_id=user_id, agent_id=agent_id, limit=limit)
        if not hits:
            return "未在长期记忆中找到相关内容。"
        return "\n".join(f"{i}. [{h.get('id')}] {h.get('memory', '')}" for i, h in enumerate(hits, 1))

    @tool
    def forget(memory_id: str) -> str:
        """删除一条长期记忆。memory_id 来自 recall 结果中方括号里的 id。

        Args:
            memory_id: 记忆条目 ID。
        """
        ltm.delete(memory_id)
        return f"已删除记忆 {memory_id}"

    return [remember, recall, forget] if include_remember else [recall, forget]
