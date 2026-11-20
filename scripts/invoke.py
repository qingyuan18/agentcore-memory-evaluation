#!/usr/bin/env python3
"""调用已部署的 Runtime。

  python scripts/invoke.py --runtime-arn <arn> --user alice "我喜欢喝美式咖啡,请记住"
  python scripts/invoke.py --runtime-arn <arn> --user alice --session <同一个id> "我喜欢喝什么?"

同一个 session id = 同一段短期记忆; 不同 session + 同一个 user = 只靠长期记忆(Mem0)衔接。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.runtime_client import invoke_runtime, new_session_id  # noqa: E402

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("prompt")
    p.add_argument("--runtime-arn", required=True)
    p.add_argument("--user", default="demo-user")
    p.add_argument("--session", default=new_session_id())
    p.add_argument("--region", default="cn-northwest-1")
    a = p.parse_args()
    out = invoke_runtime(a.runtime_arn, a.prompt, a.user, a.session, a.region)
    print(f"[session {a.session}]")
    print(out.get("result") or out)
