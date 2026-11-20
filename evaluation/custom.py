"""自定义评估器。

三种类型 (定义见 evaluation/custom_evaluators.yaml, 也可以在单次评估请求里内联传入):

  judge    自己写评估指令(rubric), 可选评分刻度, 由裁判模型打分
  derived  基于内置评估器, 替换它的评估提示词 (system_prompt)
  code     确定性规则, 指向 evaluation/custom_code/ 下的 Python 函数 (不调用模型)

字段:
  name            评估器名称, 字母开头, 字母/数字/下划线, <= 48 字符, 不能与内置评估器重名
  type            judge | derived | code
  description     说明 (可选)
  level           judge 专用: TRACE(只评最后一轮, 之前的轮次作为上下文) | SESSION(评整个会话), 默认 TRACE
  instructions    judge 专用: 评估指令
  rating_scale    judge 专用(可选): [{label, value(0~1), definition}], 裁判从中选一档; 不给则直接打 0~1 分
  pass_threshold  judge/code: 分数 >= 阈值视为通过, 默认 0.5
  needs_reference judge 专用: 是否需要参考答案 expected_output (只在数据集回放时可用), 默认 false
  base            derived 专用: 内置评估器名称, 如 Helpfulness
  system_prompt   derived 专用: 替换后的评估提示词
  function        code 专用: "模块:函数", 模块必须位于 evaluation.custom_code 包内
  params          code 专用(可选): 传给函数的关键字参数

code 函数签名:  fn(case: dict, **params) -> {"score": float, "passed": bool(可选), "reason": str}
  case = {"input", "output", "expected_output", "turns": [{"user", "assistant", "tools": [{"name", "arguments", "result"}]}]}
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml
from pydantic import BaseModel, Field, create_model
from strands import Agent
from strands_evals.evaluators import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput
from strands_evals.types.trace import AgentInvocationSpan, Session, ToolExecutionSpan

DEFAULT_FILE = Path(__file__).with_name("custom_evaluators.yaml")
CODE_PACKAGE = "evaluation.custom_code"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
TYPES = ("judge", "derived", "code")
LEVELS = ("TRACE", "SESSION")


# --------------------------------------------------------------------------- #
# 定义加载与校验
# --------------------------------------------------------------------------- #
def load_definitions(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = Path(path) if path else DEFAULT_FILE
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(data.get("evaluators") or [])


def validate_definitions(defs: Iterable[Dict[str, Any]], builtin_names: Iterable[str],
                         allow_code: bool = True) -> Dict[str, Dict[str, Any]]:
    """校验并返回 {name: definition}。有问题直接抛 ValueError, 列出全部错误。"""
    builtin = set(builtin_names)
    out: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    for i, d in enumerate(defs):
        name = str(d.get("name") or "")
        where = f"evaluators[{i}] {name or '<无名>'}"
        kind = d.get("type")
        if not NAME_RE.match(name):
            errors.append(f"{where}: name 须字母开头, 只含字母/数字/下划线, <= 48 字符")
        elif name in builtin:
            errors.append(f"{where}: 与内置评估器重名")
        elif name in out:
            errors.append(f"{where}: 名称重复")
        if kind not in TYPES:
            errors.append(f"{where}: type 须为 {'/'.join(TYPES)}")
            continue
        if kind == "judge":
            if not str(d.get("instructions") or "").strip():
                errors.append(f"{where}: judge 需要 instructions")
            if d.get("level", "TRACE") not in LEVELS:
                errors.append(f"{where}: level 须为 TRACE 或 SESSION")
            for j, item in enumerate(d.get("rating_scale") or []):
                v = item.get("value")
                if not item.get("label") or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                    errors.append(f"{where}: rating_scale[{j}] 需要 label 和 0~1 之间的 value")
        elif kind == "derived":
            if d.get("base") not in builtin:
                errors.append(f"{where}: base 须为内置评估器之一: {sorted(builtin)}")
            if not str(d.get("system_prompt") or "").strip():
                errors.append(f"{where}: derived 需要 system_prompt")
        elif kind == "code":
            if not allow_code:
                errors.append(f"{where}: 内联定义不允许 code 类型, 请写进打包内的 custom_evaluators.yaml")
            fn = str(d.get("function") or "")
            if ":" not in fn or not fn.split(":")[0].startswith(CODE_PACKAGE + "."):
                errors.append(f"{where}: function 须为 '{CODE_PACKAGE}.<模块>:<函数>'")
        if not errors or not any(e.startswith(where) for e in errors):
            out[name] = dict(d)
    if errors:
        raise ValueError("自定义评估器定义有误:\n  " + "\n  ".join(errors))
    return out


# --------------------------------------------------------------------------- #
# 会话 -> 文本 / dict
# --------------------------------------------------------------------------- #
def session_turns(session: Optional[Session]) -> List[Dict[str, Any]]:
    """按轮次整理: 每个 trace 一轮, 含用户输入、最终回答和该轮的工具调用。"""
    turns: List[Dict[str, Any]] = []
    for trace in (session.traces if session else []):
        turn: Dict[str, Any] = {"user": "", "assistant": "", "tools": []}
        for span in trace.spans:
            if isinstance(span, AgentInvocationSpan):
                turn["user"] = str(span.user_prompt or "")
                turn["assistant"] = str(span.agent_response or "")
            elif isinstance(span, ToolExecutionSpan):
                turn["tools"].append({
                    "name": span.tool_call.name,
                    "arguments": span.tool_call.arguments,
                    "result": str(span.tool_result.content)[:2000],
                })
        if turn["user"] or turn["assistant"]:
            turns.append(turn)
    return turns


def _format_turns(turns: List[Dict[str, Any]]) -> str:
    lines = []
    for n, t in enumerate(turns, 1):
        lines.append(f"[第 {n} 轮] 用户: {t['user']}")
        for tool in t["tools"]:
            lines.append(f"  工具调用: {tool['name']}({tool['arguments']}) -> {tool['result']}")
        lines.append(f"[第 {n} 轮] 助手: {t['assistant']}")
    return "\n".join(lines) or "(无)"


def case_dict(case: EvaluationData) -> Dict[str, Any]:
    return {
        "input": case.input,
        "output": case.actual_output,
        "expected_output": case.expected_output,
        "turns": session_turns(case.actual_trajectory if isinstance(case.actual_trajectory, Session) else None),
    }


# --------------------------------------------------------------------------- #
# judge
# --------------------------------------------------------------------------- #
class _ScoreVerdict(BaseModel):
    reason: str = Field(description="评分理由, 先分析再下结论")
    score: float = Field(ge=0.0, le=1.0, description="0.0 ~ 1.0 之间的分数")


JUDGE_SYSTEM = (
    "你是严格、客观的 AI 助手质量评审。只依据给出的对话记录、工具调用结果和评估指令打分, "
    "不要臆测记录之外的信息。理由用中文简要说明。"
)


class CustomJudgeEvaluator(Evaluator):
    def __init__(self, definition: Dict[str, Any], model: Any):
        super().__init__(name=definition["name"])
        self.d = definition
        self.model = model
        self.scale = list(definition.get("rating_scale") or [])
        self.threshold = float(definition.get("pass_threshold", 0.5))

    def _prompt(self, case: EvaluationData) -> str:
        turns = case_dict(case)["turns"]
        if self.d.get("level", "TRACE") == "SESSION" or len(turns) <= 1:
            body = f"## 待评估的完整会话\n{_format_turns(turns)}"
        else:
            body = (f"## 之前的对话(上下文)\n{_format_turns(turns[:-1])}\n\n"
                    f"## 待评估的最后一轮\n{_format_turns(turns[-1:])}")
        if not turns:  # 没有 trace 结构时退回 input/output
            body = f"## 用户输入\n{case.input}\n\n## 助手回答\n{case.actual_output}"
        parts = [body]
        if case.expected_output:
            parts.append(f"## 参考答案\n{case.expected_output}")
        parts.append(f"## 评估指令\n{self.d['instructions'].strip()}")
        if self.scale:
            scale = "\n".join(f"- {s['label']}: {s.get('definition', '')}" for s in self.scale)
            parts.append(f"## 评分刻度(label 只能取以下之一, 只写 label 本身)\n{scale}")
        else:
            parts.append("## 打分\n给出 0.0 ~ 1.0 的分数, 1.0 为完全满足评估指令。")
        return "\n\n".join(parts)

    def evaluate(self, evaluation_case: EvaluationData) -> List[EvaluationOutput]:
        agent = Agent(model=self.model, system_prompt=JUDGE_SYSTEM, callback_handler=None)
        prompt = self._prompt(evaluation_case)
        if self.scale:
            verdict = agent(prompt, structured_output_model=self._label_model()).structured_output
            match = self._match_label(str(verdict.label))
            if match is None:
                raise ValueError(f"裁判给出的 label {verdict.label!r} 不在评分刻度中")
            score, label = float(match["value"]), match["label"]
        else:
            verdict = agent(prompt, structured_output_model=_ScoreVerdict).structured_output
            score, label = float(verdict.score), None
        return [EvaluationOutput(score=score, test_pass=score >= self.threshold, reason=verdict.reason, label=label)]

    def _label_model(self):
        """label 在 JSON Schema 里声明为 enum 引导模型; 校验放在 _match_label 里做容错, 不让 pydantic 直接拒绝。"""
        labels = [s["label"] for s in self.scale]
        return create_model(
            "LabelVerdict",
            reason=(str, Field(description="评分理由, 先分析再下结论")),
            label=(str, Field(description="从评分刻度中选择的档位, 只写 label 本身",
                              json_schema_extra={"enum": labels})),
        )

    def _match_label(self, raw: str) -> Optional[Dict[str, Any]]:
        """容错: 裁判有时会带上 "(=1.0)" 之类的后缀, 取以某个 label 开头的那一档 (最长匹配)。"""
        text = raw.strip().strip("\"'「」").lower()
        exact = [s for s in self.scale if s["label"].strip().lower() == text]
        if exact:
            return exact[0]
        prefixed = [s for s in self.scale if text.startswith(s["label"].strip().lower())]
        return max(prefixed, key=lambda s: len(s["label"])) if prefixed else None


# --------------------------------------------------------------------------- #
# code
# --------------------------------------------------------------------------- #
class CodeEvaluator(Evaluator):
    def __init__(self, definition: Dict[str, Any]):
        super().__init__(name=definition["name"])
        module_name, func_name = definition["function"].split(":", 1)
        if not module_name.startswith(CODE_PACKAGE + "."):  # 双重保护
            raise ValueError(f"code 评估器只能引用 {CODE_PACKAGE} 包内的函数")
        self.fn = getattr(importlib.import_module(module_name), func_name)
        self.params = dict(definition.get("params") or {})
        self.threshold = float(definition.get("pass_threshold", 0.5))

    def evaluate(self, evaluation_case: EvaluationData) -> List[EvaluationOutput]:
        result = self.fn(case_dict(evaluation_case), **self.params)
        score = float(result["score"])
        passed = bool(result.get("passed", score >= self.threshold))
        return [EvaluationOutput(score=score, test_pass=passed, reason=str(result.get("reason", "")),
                                 label=result.get("label"))]


def build_custom(definition: Dict[str, Any], judge_model: Any, builtin_registry: Dict[str, Any]) -> Evaluator:
    kind = definition["type"]
    if kind == "judge":
        return CustomJudgeEvaluator(definition, judge_model)
    if kind == "derived":
        return builtin_registry[definition["base"]](
            model=judge_model, name=definition["name"], system_prompt=definition["system_prompt"])
    return CodeEvaluator(definition)
