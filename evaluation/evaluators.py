"""评估器注册表: 内置评估器 + 自定义评估器。

内置: strands-agents-evals 自带的 LLM-as-judge 评估器 (与 AgentCore Evaluations 的 Builtin.* 对应)。
自定义: evaluation/custom_evaluators.yaml 中定义, 或单次评估请求内联传入 (judge / derived / code, 见 custom.py)。
裁判模型来自 JUDGE_* 环境变量 (见 common/models.py)。
需要参考答案(expected_output)的评估器只在数据集回放时可用, 其他场景自动跳过。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from strands_evals.evaluators import (
    CoherenceEvaluator,
    ConcisenessEvaluator,
    CorrectnessEvaluator,
    FaithfulnessEvaluator,
    GoalSuccessRateEvaluator,
    HarmfulnessEvaluator,
    HelpfulnessEvaluator,
    ResponseRelevanceEvaluator,
    ToolParameterAccuracyEvaluator,
    ToolSelectionAccuracyEvaluator,
)

from common.models import build_strands_model

from .custom import build_custom, load_definitions, validate_definitions

BUILTIN = {
    "Helpfulness": HelpfulnessEvaluator,
    "Correctness": CorrectnessEvaluator,
    "Faithfulness": FaithfulnessEvaluator,
    "ResponseRelevance": ResponseRelevanceEvaluator,
    "Coherence": CoherenceEvaluator,
    "Conciseness": ConcisenessEvaluator,
    "Harmfulness": HarmfulnessEvaluator,
    "GoalSuccessRate": GoalSuccessRateEvaluator,
    "ToolSelectionAccuracy": ToolSelectionAccuracyEvaluator,
    "ToolParameterAccuracy": ToolParameterAccuracyEvaluator,
}
BUILTIN_NEEDS_REFERENCE = {"Correctness"}
DEFAULT_ONLINE = ["Helpfulness", "Faithfulness", "ResponseRelevance", "GoalSuccessRate", "ToolSelectionAccuracy"]
# 向后兼容旧名称
REGISTRY = BUILTIN


def catalog(inline: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """所有可用评估器: {name: {"kind": builtin|judge|derived|code, "needs_reference": bool, "definition"?}}。

    inline 为单次请求内联的自定义定义 (只允许 judge / derived), 同名时覆盖文件中的定义。
    """
    items: Dict[str, Dict[str, Any]] = {
        n: {"kind": "builtin", "needs_reference": n in BUILTIN_NEEDS_REFERENCE} for n in BUILTIN
    }
    from_file = validate_definitions(load_definitions(), BUILTIN, allow_code=True)
    from_inline = validate_definitions(inline or [], BUILTIN, allow_code=False)
    for name, d in {**from_file, **from_inline}.items():
        items[name] = {"kind": d["type"], "needs_reference": bool(d.get("needs_reference")),
                       "definition": d, "description": d.get("description", "")}
    return items


def build_evaluators(names: Iterable[str], has_reference: bool = False,
                     inline: Optional[List[Dict[str, Any]]] = None) -> List:
    """构造评估器实例; 没有参考答案时跳过需要参考答案的评估器。

    names 中写 "custom:*" 表示启用全部自定义评估器。
    """
    items = catalog(inline)
    wanted: List[str] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name == "custom:*":
            wanted += [n for n, v in items.items() if v["kind"] != "builtin"]
        elif name not in items:
            raise ValueError(f"未知评估器 {name}, 可选: {sorted(items)}")
        else:
            wanted.append(name)

    judge = None
    built: Dict[str, Any] = {}
    for name in dict.fromkeys(wanted):  # 去重保序
        info = items[name]
        if info["needs_reference"] and not has_reference:
            continue
        if info["kind"] != "code" and judge is None:
            judge = build_strands_model("JUDGE")
        if info["kind"] == "builtin":
            built[name] = BUILTIN[name](model=judge, name=name)
        else:
            built[name] = build_custom(info["definition"], judge, BUILTIN)
    return list(built.values())
