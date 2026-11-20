"""把评估记录汇总成每个评估器的均值/通过率表。"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List


def summarize(records: List[Dict]) -> str:
    stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "score": 0.0, "pass": 0, "err": 0})
    for r in records:
        s = stats[r["evaluator"]]
        if "error" in r:
            s["err"] += 1
            continue
        s["n"] += 1
        s["score"] += float(r.get("score") or 0)
        s["pass"] += 1 if r.get("passed") else 0
    lines = [f"{'evaluator':<24}{'n':>4}{'mean':>8}{'pass%':>8}{'errors':>8}"]
    for name, s in sorted(stats.items()):
        n = int(s["n"])
        mean = s["score"] / n if n else float("nan")
        rate = 100 * s["pass"] / n if n else float("nan")
        lines.append(f"{name:<24}{n:>4}{mean:>8.2f}{rate:>7.0f}%{int(s['err']):>8}")
    return "\n".join(lines)
