"""带短期 + 长期记忆的 Strands Agent, 作为 AgentCore Runtime 应用。

调用协议 (InvokeAgentRuntime 的 payload):
    {"prompt": "...", "user_id": "alice"}
runtimeSessionId 由调用方通过 InvokeAgentRuntime 的 runtimeSessionId 参数传入,
Runtime 会注入到 context.session_id, 同时 ADOT 会把它写进 span 的 session.id,
评估模块据此按会话回读 trace。

长期记忆流程 (AUTO_MEMORY=true, 默认):
  1. 每轮开始前用用户输入检索 Mem0, 命中的记忆以 <用户长期记忆> 标签拼在本轮用户消息前(会进入 trace, 评估裁判可见)
  2. 每轮结束后把 (用户输入, 回答) 交给 Mem0, 由 Mem0 的 LLM 抽取值得保存的事实并去重
模型仍可调用 recall (补充检索) / forget (删除)。
实测 kimi-k3 只靠工具时经常口头说"已记住"却不调用 remember, 所以写入不交给模型决定。
AUTO_MEMORY=false 时退回纯工具模式 (remember / recall / forget)。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent

from common.models import build_strands_model
from common.secrets import get_setting

from .memory.long_term import LongTermMemory
from .memory.session import build_conversation_manager, build_session_manager
from .memory.tools import build_memory_tools

logger = logging.getLogger("memory-agent")

BASE_PROMPT = """你是一个具备长期记忆能力的智能助手。每个新会话你看不到之前的对话,过往信息只存在于长期记忆里。

规则:
1. 用户消息开头 <用户长期记忆> 标签内是系统检索到的该用户长期记忆(不是用户本轮说的话)。
   回答涉及用户自身信息、偏好或过往说过的话时以它为准;不够时可以调用 recall 补充检索。
2. 记忆里没有相关内容时,如实告诉用户没有记录,不要编造。
3. 用户要求删除某条记忆时,先 recall 找到 id,再调用 forget。
4. 用中文自然回答,不要提及"工具"或"记忆库"等实现细节。
"""

TOOL_ONLY_PROMPT = BASE_PROMPT + """5. 用户提供长期有效的信息(姓名、偏好、身份、约定、事实)时,必须调用 remember 保存,不要只口头答应。
"""

app = BedrockAgentCoreApp()

# microVM 内复用: 模型和 Mem0 连接只在首次调用时初始化
_state: Dict[str, Any] = {}


def _shared():
    if not _state:
        _state["model"] = build_strands_model("LLM")
        _state["ltm"] = LongTermMemory()
    return _state["model"], _state["ltm"]


def _auto_memory() -> bool:
    return get_setting("AUTO_MEMORY", "true").lower() != "false"


def _memory_block(hits: List[Dict[str, Any]]) -> str:
    if not hits:
        return "\n(无)\n"
    return "\n" + "\n".join(f"- {h.get('memory', '')}" for h in hits) + "\n"


def build_agent(user_id: str, session_id: str, system_prompt: str, auto: bool) -> Agent:
    model, ltm = _shared()
    agent_name = get_setting("AGENT_NAME", "memory-agent")
    return Agent(
        model=model,
        tools=build_memory_tools(ltm, user_id=user_id, agent_id=agent_name, include_remember=not auto),
        system_prompt=system_prompt,
        agent_id=agent_name,
        conversation_manager=build_conversation_manager(),
        session_manager=build_session_manager(session_id),
        # 兜底: 保证 span 上带 session.id / user.id, 评估按它查询
        trace_attributes={"session.id": session_id, "user.id": user_id},
    )


@app.entrypoint
def invoke(payload: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    prompt = (payload or {}).get("prompt")
    if not prompt:
        return {"error": "payload 缺少 prompt"}
    user_id = str(payload.get("user_id") or "anonymous")
    session_id = str(
        getattr(context, "session_id", None) or payload.get("session_id") or uuid.uuid4().hex * 2
    )
    _, ltm = _shared()
    agent_name = get_setting("AGENT_NAME", "memory-agent")
    auto = _auto_memory()

    if auto:
        hits = ltm.search(prompt, user_id=user_id, agent_id=agent_name,
                          limit=int(get_setting("MEMORY_TOP_K", "5")))
        system_prompt = BASE_PROMPT
        # 记忆放进本轮用户消息(而不是 system prompt): 这样它会出现在 trace 里,
        # 评估裁判才能看到回答的依据, 否则基于记忆的正确回答会被判为"无依据"
        agent_input = f"<用户长期记忆>{_memory_block(hits)}</用户长期记忆>\n\n{prompt}"
    else:
        system_prompt = TOOL_ONLY_PROMPT
        agent_input = prompt

    agent = build_agent(user_id, session_id, system_prompt, auto)
    answer = str(agent(agent_input))

    saved: List[str] = []
    if auto:
        try:
            result = ltm.add(
                [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}],
                user_id=user_id, agent_id=agent_name, metadata={"session_id": session_id},
            )
            saved = [e.get("memory", "") for e in (result or {}).get("results", []) if e.get("memory")]
        except Exception:  # 写记忆失败不影响本轮回答
            logger.exception("mem0 add failed")
    return {"result": answer, "session_id": session_id, "user_id": user_id, "memories_saved": saved}


if __name__ == "__main__":
    app.run()
