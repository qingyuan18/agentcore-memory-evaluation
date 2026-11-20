#!/usr/bin/env python3
"""记忆功能冒烟测试: 写入 -> 新会话召回 -> 用户隔离 -> 同会话多轮(短期记忆)。

  python scripts/memory_smoke_test.py --runtime-arn <arn>
每一步打印 session id, 后续可以直接拿这些 session 做离线评估。
"""
import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.runtime_client import invoke_runtime, new_session_id  # noqa: E402


def step(title, arn, user, prompt, session, region):
    t = time.time()
    try:
        resp = invoke_runtime(arn, prompt, user, session, region)
        out = resp.get("result", "")
        if resp.get("memories_saved"):
            out += f"\n   [Mem0 写入] {resp['memories_saved']}"
    except Exception as exc:  # 打印后继续, 便于看完整结果
        out = f"<ERROR {type(exc).__name__}: {exc}>"
    print(f"\n== {title}\n   user={user} session={session} ({time.time() - t:.1f}s)\n   Q: {prompt}\n   A: {out.strip()}")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--runtime-arn", required=True)
    p.add_argument("--region", default="cn-northwest-1")
    a = p.parse_args()
    tag = uuid.uuid4().hex[:6]
    alice, bob = f"alice-{tag}", f"bob-{tag}"

    step("1 写入长期记忆", a.runtime_arn, alice, "我叫小李,住在杭州,是后端工程师,喜欢喝不加糖的美式咖啡,请记住。", new_session_id(), a.region)
    time.sleep(3)
    step("2 新会话召回(只能靠 Mem0)", a.runtime_arn, alice, "我住在哪个城市?平时喜欢喝什么咖啡?", new_session_id(), a.region)
    step("3 另一个用户(隔离, 应答不知道)", a.runtime_arn, bob, "我住在哪个城市?平时喜欢喝什么咖啡?", new_session_id(), a.region)
    sid = new_session_id()
    step("4a 同会话多轮: 说一个临时信息", a.runtime_arn, alice, "这是一个临时口令: 蓝色海豚 42。这个不需要长期记住。", sid, a.region)
    step("4b 同会话多轮: 追问(靠短期记忆/S3 会话)", a.runtime_arn, alice, "我刚才说的临时口令是什么?", sid, a.region)
