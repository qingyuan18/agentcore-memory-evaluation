#!/usr/bin/env python3
"""开通 CloudWatch Transaction Search (一次性, 区域级)。

Runtime 的 trace 经 ADOT -> X-Ray OTLP 端点 -> 'aws/spans' 日志组。评估模块读的就是 aws/spans,
所以这一步没做, 评估会查不到任何会话。步骤:
  1. 给 CloudWatch Logs 加资源策略, 允许 X-Ray 服务写 aws/spans
  2. X-Ray 的 trace segment 目的地切到 CloudWatchLogs, 等待 ACTIVE
"""
import json
import sys
import time

import boto3

REGION = sys.argv[1] if len(sys.argv) > 1 else "cn-northwest-1"
session = boto3.Session(region_name=REGION)
logs, xray, sts = session.client("logs"), session.client("xray"), session.client("sts")

account = sts.get_caller_identity()["Account"]
partition = "aws-cn" if REGION.startswith("cn-") else "aws"

# 不复用 "TransactionSearchXRayAccess": 他处在中国区常把资源 ARN 写成 arn:aws:(海外分区写法),
# 同名策略存在会被跳过, 导致 UpdateTraceSegmentDestination 报 "XRay does not have permission to call PutLogEvents"。
# 这里用独立名称, 且按分区生成 ARN。
POLICY = "AgentDemoSpansXRayAccess"
if not any(p["policyName"] == POLICY for p in logs.describe_resource_policies().get("resourcePolicies", [])):
    logs.put_resource_policy(
        policyName=POLICY,
        policyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": POLICY,
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},  # 服务主体各分区一致, 但资源 ARN 必须用 arn:aws-cn
                "Action": "logs:PutLogEvents",
                "Resource": [
                    f"arn:{partition}:logs:{REGION}:{account}:log-group:aws/spans:*",
                    f"arn:{partition}:logs:{REGION}:{account}:log-group:/aws/application-signals/data:*",
                ],
                "Condition": {
                    "ArnLike": {"aws:SourceArn": f"arn:{partition}:xray:{REGION}:{account}:*"},
                    "StringEquals": {"aws:SourceAccount": account},
                },
            }],
        }),
    )
    print("resource policy created")

state = xray.get_trace_segment_destination()
if state.get("Destination") != "CloudWatchLogs":
    xray.update_trace_segment_destination(Destination="CloudWatchLogs")
for _ in range(30):
    state = xray.get_trace_segment_destination()
    if state.get("Status") == "ACTIVE":
        break
    time.sleep(5)
print("transaction search:", state)
