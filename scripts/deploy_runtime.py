#!/usr/bin/env python3
"""把代码 zip 部署为 AgentCore Runtime (中国区: cn-northwest-1)。

幂等: 同名 Runtime 已存在则 UpdateAgentRuntime, 否则 CreateAgentRuntime。
UpdateAgentRuntime 会重置未传入的字段, 所以环境变量/网络配置每次都完整传入。
需要 boto3 >= 1.43 (中国区仅支持 platformVersion V2, 老版本 SDK 无此参数)。

被测 Agent:
  python scripts/deploy_runtime.py --kind agent --name memory_agent \
      --bucket <ArtifactBucket> --role-arn <RuntimeExecutionRoleArn> \
      --subnets subnet-a,subnet-b --security-groups <RuntimeSecurityGroupId> \
      --db-endpoint <DBEndpoint> --db-secret-arn <DBSecretArn>
评估 worker:
  python scripts/deploy_runtime.py --kind eval --name memory_agent_eval \
      --bucket <ArtifactBucket> --role-arn <EvalRuntimeExecutionRoleArn> \
      --subnets subnet-a,subnet-b --security-groups <RuntimeSecurityGroupId>
"""
from __future__ import annotations

import argparse
import sys
import time

import boto3
from dotenv import dotenv_values

# 只把这些键从 .env 写入 Runtime 环境变量, 其余内容不上传
AGENT_KEYS = (
    "LLM_PROVIDER LLM_BASE_URL LLM_MODEL LLM_MAX_TOKENS LLM_BEDROCK_REGION LLM_SUPPORTS_TEMPERATURE "
    "LLM_API_KEY LLM_API_KEY_SECRET_ID LLM_API_KEY_SECRET_KEY "
    "EMBED_PROVIDER EMBED_BASE_URL EMBED_MODEL EMBED_DIMS EMBED_BEDROCK_REGION "
    "EMBED_API_KEY EMBED_API_KEY_SECRET_ID EMBED_API_KEY_SECRET_KEY "
    "CONVERSATION_WINDOW_SIZE AUTO_MEMORY MEMORY_TOP_K"
).split()
EVAL_KEYS = (
    "JUDGE_PROVIDER JUDGE_BASE_URL JUDGE_MODEL JUDGE_MAX_TOKENS JUDGE_BEDROCK_REGION JUDGE_SUPPORTS_TEMPERATURE "
    "JUDGE_API_KEY JUDGE_API_KEY_SECRET_ID JUDGE_API_KEY_SECRET_KEY"
).split()


def build_env(args) -> dict:
    keys = AGENT_KEYS if args.kind == "agent" else EVAL_KEYS
    env = {k: v for k, v in dotenv_values(args.env_file).items() if k in keys and v}
    if args.kind == "agent":
        if not (args.db_endpoint and args.db_secret_arn):
            sys.exit("--kind agent 需要 --db-endpoint 和 --db-secret-arn")
        env.update({
            "PG_HOST": args.db_endpoint,
            "PG_DBNAME": "mem0",
            "PG_USER": "postgres",
            "PG_PASSWORD_SECRET_ID": args.db_secret_arn,
            "PG_PASSWORD_SECRET_KEY": "password",
            "SESSION_S3_BUCKET": args.bucket,
            "AGENT_NAME": args.name,
        })
    else:
        env.update({"EVAL_RESULTS_BUCKET": args.bucket})
    return env


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=["agent", "eval"], default="agent")
    p.add_argument("--name", default=None)
    p.add_argument("--region", default="cn-northwest-1")
    p.add_argument("--zip", default=None)
    p.add_argument("--bucket", required=True)
    p.add_argument("--role-arn", required=True)
    p.add_argument("--subnets", required=True)
    p.add_argument("--security-groups", required=True)
    p.add_argument("--db-endpoint")
    p.add_argument("--db-secret-arn")
    p.add_argument("--env-file", default=".env")
    args = p.parse_args()
    args.name = args.name or ("memory_agent" if args.kind == "agent" else "memory_agent_eval")
    args.zip = args.zip or f"build/{args.kind}.zip"

    session = boto3.Session(region_name=args.region)
    key = f"agents/{args.name}/{args.kind}.zip"
    session.client("s3").upload_file(args.zip, args.bucket, key)
    print(f"uploaded s3://{args.bucket}/{key}")

    # 被测 Agent 经 opentelemetry-instrument 启动, ADOT 才会导出 trace 到可观测性;
    # 评估 worker 不需要被观测 (也避免把裁判调用混进 aws/spans)
    entry = ["opentelemetry-instrument", "main.py"] if args.kind == "agent" else ["main.py"]
    ctl = session.client("bedrock-agentcore-control")
    params = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": args.bucket, "prefix": key}},
            "runtime": "PYTHON_3_13",
            "entryPoint": entry,
        }},
        roleArn=args.role_arn,
        networkConfiguration={
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": args.subnets.split(","),
                "securityGroups": args.security_groups.split(","),
            },
        },
        environmentVariables=build_env(args),
        platformVersion="V2",
    )

    existing = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                     if r["agentRuntimeName"] == args.name), None)
    if existing:
        rid = existing["agentRuntimeId"]
        ctl.update_agent_runtime(agentRuntimeId=rid, **params)
        print(f"updating runtime {rid}")
    else:
        rid = ctl.create_agent_runtime(agentRuntimeName=args.name, **params)["agentRuntimeId"]
        print(f"creating runtime {rid}")

    for _ in range(90):
        rt = ctl.get_agent_runtime(agentRuntimeId=rid)
        status = rt["status"]
        print("status:", status)
        if status == "READY":
            print("RUNTIME_ID  =", rid)
            print("RUNTIME_ARN =", rt["agentRuntimeArn"])
            print("LOG_GROUP   = /aws/bedrock-agentcore/runtimes/%s-DEFAULT" % rid)
            return 0
        if status.endswith("FAILED"):
            print(rt.get("failureReason"), file=sys.stderr)
            return 1
        time.sleep(10)
    print("timeout waiting for READY", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
