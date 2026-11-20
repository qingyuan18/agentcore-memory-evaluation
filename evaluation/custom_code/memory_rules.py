"""记忆 Agent 的确定性规则评估器示例。"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def no_implementation_leak(case: Dict[str, Any], forbidden: List[str] | None = None) -> Dict[str, Any]:
    """回答中不能暴露实现细节 (系统提示要求不提"工具""记忆库", 也不能把记忆标签原样吐出来)。"""
    forbidden = forbidden or ["<用户长期记忆>", "</用户长期记忆>", "记忆库", "recall", "remember", "工具调用"]
    hits = []
    for n, turn in enumerate(case["turns"] or [{"assistant": case.get("output") or ""}], 1):
        for word in forbidden:
            if word.lower() in str(turn.get("assistant", "")).lower():
                hits.append(f"第{n}轮含'{word}'")
    if hits:
        return {"score": 0.0, "passed": False, "reason": "回答暴露了实现细节: " + ", ".join(hits)}
    return {"score": 1.0, "passed": True, "reason": "未发现实现细节泄露"}


def answer_in_chinese(case: Dict[str, Any], min_ratio: float = 0.3) -> Dict[str, Any]:
    """最后一轮回答应以中文为主: 中文字符占非空白字符的比例 >= min_ratio。"""
    text = str((case["turns"][-1]["assistant"] if case["turns"] else case.get("output")) or "")
    chars = re.sub(r"\s", "", text)
    if not chars:
        return {"score": 0.0, "passed": False, "reason": "回答为空"}
    ratio = len(re.findall(r"[\u4e00-\u9fff]", chars)) / len(chars)
    return {"score": round(ratio, 3), "passed": ratio >= min_ratio,
            "reason": f"中文字符占比 {ratio:.0%} (阈值 {min_ratio:.0%})"}
