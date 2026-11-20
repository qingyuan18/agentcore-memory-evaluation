# AgentCore 中国区:Agent 记忆(Mem0) + 评估(离线/在线) Demo

只包含两个能力,运行在 **AWS 中国区 `cn-northwest-1`** 的 AgentCore Runtime 上:

| 能力 | 实现 |
|------|------|
| 短期记忆 | Strands `SlidingWindowConversationManager` + `S3SessionManager`(以 runtimeSessionId 为键,microVM 被回收后仍可恢复) |
| 长期记忆 | Mem0 + Aurora PostgreSQL(pgvector)。默认自动记忆:每轮前按用户输入检索并注入 system prompt,每轮后把对话交给 Mem0 抽取事实写入;模型另可调用 `recall / forget`。`AUTO_MEMORY=false` 退回纯工具模式(`remember / recall / forget`)。`user_id` 由闭包绑定 |
| 评估运行位置 | 评估 worker 也部署为一个 AgentCore Runtime(与被测 Agent 同 VPC),用 InvokeAgentRuntime 触发,裁判模型在 VPC 内调用 |
| 自定义评估器 | judge(自写指令+评分刻度)/ derived(改写内置评估器提示词)/ code(Python 规则),可随包部署或单次请求内联 |
| 离线评估 | ① 拉取可观测性里的历史会话批量打分 ② 用带参考答案的数据集回放调用 Runtime 后打分(含"跨会话记忆"用例) |
| 在线评估 | 轮询可观测性,对已空闲的线上会话做确定性采样打分,状态去重,结果落 S3/本地 |
| 数据来源 | AgentCore Observability:Runtime 经 ADOT 导出的 trace → CloudWatch `aws/spans` 与 `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT` |

## 架构

```
 调用方 ──InvokeAgentRuntime──▶ AgentCore Runtime (VPC 模式, zip, opentelemetry-instrument main.py)
                                   │ Strands Agent ── 短期: S3 (sessions/)
                                   │             └─ 长期: Mem0 ─▶ Aurora pgvector (私有子网)
                                   │ LLM / Embedding ─▶ OpenAI 兼容网关 (经 NAT 出网)
                                   └─ ADOT ─▶ X-Ray OTLP ─▶ CloudWatch Logs: aws/spans (Transaction Search)
                                                                     │
        离线/在线评估 (evaluation/) ◀── Logs Insights 按 session.id 回读 ──┘
              │ strands-agents-evals 的 LLM-as-judge (裁判模型 JUDGE_*)
              └─▶ 结果 JSONL: S3 或 eval_output/
```

目录:`agent/`(Runtime 应用) · `common/`(模型/密钥工具) · `evaluation/`(评估) · `infra/`(CFN) · `scripts/`(打包/部署/调用/开通可观测)

## 前置条件

- 中国区账号凭据(下文用 `AWS_PROFILE=china-region`),Python 3.13(与 Runtime 的 `PYTHON_3_13` 一致),boto3 ≥ 1.43(中国区只支持 platformVersion V2)
- 一个 OpenAI 兼容的 LLM / Embedding 端点(本 demo:LiteLLM 网关,`kimi-k3` + `titan-embed-v2`)
- 国内 pip 慢时设置 `PIP_INDEX_URL`

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-eval.txt -r requirements-agent.txt
cp .env.example .env   # 填写模型网关、Embedding 等
export AWS_PROFILE=china-region AWS_REGION=cn-northwest-1
```

## 实施步骤

### 1. 网络(可选)
Runtime 要连私有子网里的 Aurora,必须用 **VPC 模式**;VPC 模式的 ENI 没有公网 IP,访问 LLM 网关、Secrets Manager 需要 NAT。
已有带 NAT 的 VPC 可跳过。

```bash
aws cloudformation deploy --stack-name memdemo-net --template-file infra/network.yaml
```

### 2. 记忆底座
S3 桶(代码包 + 会话)、Secrets Manager(DB 随机密码,纯字母数字,Mem0 要把密码拼进 URI)、Aurora PostgreSQL Serverless v2、Runtime 执行角色。

```bash
aws cloudformation deploy --stack-name memdemo --template-file infra/memory-infra.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides VpcId=<vpc> PrivateSubnetIds=<subnetA>,<subnetB>
aws cloudformation describe-stacks --stack-name memdemo --query 'Stacks[0].Outputs'
```
Aurora 版本需在本区可用(`aws rds describe-db-engine-versions --engine aurora-postgresql`),模板默认 16.8。pgvector 扩展由 Mem0 首次连接时自动 `CREATE EXTENSION`。

### 3. 开通可观测性(一次性,区域级)
```bash
python scripts/enable_observability.py cn-northwest-1
```
把 X-Ray trace 目的地切到 CloudWatch Logs(Transaction Search)。**不开通时 ADOT 的 span 导出会返回 400,`aws/spans` 为空,评估读不到数据。**
中国区的坑:CloudWatch Logs 资源策略里的资源 ARN 必须是 `arn:aws-cn:`。如果账号里已有同名策略 `TransactionSearchXRayAccess` 写成了 `arn:aws:`,
`UpdateTraceSegmentDestination` 会报 "XRay does not have permission to call PutLogEvents",脚本因此另建了名为 `AgentDemoSpansXRayAccess` 的策略。

### 4. 打包并部署到 AgentCore Runtime
```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ./scripts/package_agent.sh   # linux/aarch64 wheels → build/agent.zip (约 67MB)
# kimi-k3 不接受 temperature: .env 里设 LLM_SUPPORTS_TEMPERATURE=false / JUDGE_SUPPORTS_TEMPERATURE=false
python scripts/deploy_runtime.py --kind agent --name memory_agent \
  --bucket <ArtifactBucket> --role-arn <RuntimeExecutionRoleArn> \
  --subnets <subnetA>,<subnetB> --security-groups <RuntimeSecurityGroupId> \
  --db-endpoint <DBEndpoint> --db-secret-arn <DBSecretArn> --env-file .env
```
与 Runtime 集成的要点:
- `entryPoint: ["opentelemetry-instrument", "main.py"]` + 依赖 `aws-opentelemetry-distro`:Runtime 自动注入 OTEL 环境变量,ADOT 导出 trace;不经 opentelemetry-instrument 启动就没有 trace。
- 入口用 `BedrockAgentCoreApp` 的 `@app.entrypoint`;`context.session_id` 即 `runtimeSessionId`,同时作为 S3 会话键和 span 的 `session.id`(代码里也通过 `trace_attributes` 兜底)。
- `UpdateAgentRuntime` 会重置未传字段,`deploy_runtime.py` 每次都完整传入网络与环境变量。
- 执行角色信任主体是 `bedrock-agentcore.amazonaws.com`(各分区一致),资源 ARN 用 `arn:aws-cn:`。
- 代码 zip 解压后不能超过 250MB;超出请改容器部署。
- 记录输出的 `RUNTIME_ID` / `RUNTIME_ARN` 写入 `.env`。

### 5. 验证记忆
```bash
python scripts/memory_smoke_test.py --runtime-arn $RUNTIME_ARN
```
依次验证:写入长期记忆 → 新会话召回(只能靠 Mem0)→ 另一个用户隔离 → 同会话多轮(短期记忆)。
为什么默认用自动记忆:实测 kimi-k3 在纯工具模式下会口头答"已记住"却不调用 `remember`,写入不能交给模型决定。

### 6. 部署评估 worker(Runtime)
裁判模型端点通常只在 VPC 内可达,所以评估也跑在 AgentCore Runtime 上(执行角色 `EvalRuntimeExecutionRole`:读 CloudWatch、调用被测 Runtime、写 S3)。
```bash
./scripts/package_agent.sh eval            # 或 pip 很慢时: bash scripts/package_eval_fast.sh(复用 build/agent 的依赖)
python scripts/deploy_runtime.py --kind eval --name memory_agent_eval \
  --bucket <ArtifactBucket> --role-arn <EvalRuntimeExecutionRoleArn> \
  --subnets <subnetA>,<subnetB> --security-groups <RuntimeSecurityGroupId>
```
worker 不经 opentelemetry-instrument 启动,避免把裁判调用混进可观测数据。

### 7. 离线评估
```bash
# a) 历史会话(来自可观测性)
python scripts/run_eval.py --worker-arn $EVAL_ARN sessions --runtime-id $RUNTIME_ID --since 24h --limit 10
python scripts/run_eval.py --worker-arn $EVAL_ARN sessions --runtime-id $RUNTIME_ID --session-ids sid1,sid2
# b) 数据集回放(带参考答案,验证跨会话长期记忆)
python scripts/run_eval.py --worker-arn $EVAL_ARN replay --runtime-id $RUNTIME_ID --runtime-arn $RUNTIME_ARN \
  --dataset evaluation/datasets/memory_cases.jsonl
```
评估器:Helpfulness / Faithfulness / ResponseRelevance / GoalSuccessRate / ToolSelectionAccuracy(默认)、Correctness(仅回放,需 `expected_output`)等,见 `evaluation/evaluators.py`。
回放会先调用完所有用例,再统一等待 trace 入库(通常 1~3 分钟,`--wait-s`)后打分。同步调用上限 15 分钟,大批量请拆分。
本机能直连裁判模型时,也可直接 `python -m evaluation.offline ...`。

### 8. 在线评估
```bash
python scripts/run_eval.py --worker-arn $EVAL_ARN online --runtime-id $RUNTIME_ID --idle-minutes 10 --sample-rate 0.5
```
每次调用跑一轮:列出最近的会话 → 只评已空闲超过 `idle-minutes` 的 → 按 sha256(session_id) 确定性采样 → 跳过已评估的(状态存 S3)→ 打分写结果。
定时执行:用 EventBridge Scheduler 每 N 分钟触发一个调用 `InvokeAgentRuntime` 的 Lambda;或在 VPC 内常驻 `python -m evaluation.online --interval 300`。
结果为 JSONL,写在 `s3://<ArtifactBucket>/eval/results/<mode>/<date>/`,可用 Athena 或脚本分析。

### 9. 自定义评估器
除内置评估器外,支持三类自定义评估器,定义写在 `evaluation/custom_evaluators.yaml`(字段说明见 `evaluation/custom.py` 顶部):

| 类型 | 用途 | 关键字段 |
|---|---|---|
| `judge` | 自己写评估指令,由裁判模型打分;可选评分刻度(label → 0~1 分) | `instructions`、`level`(TRACE 评最后一轮 / SESSION 评整个会话)、`rating_scale`、`pass_threshold`、`needs_reference` |
| `derived` | 基于内置评估器,替换它的评估提示词 | `base`(如 Helpfulness)、`system_prompt` |
| `code` | 确定性规则,不调用模型 | `function`(必须是 `evaluation.custom_code.<模块>:<函数>`)、`params` |

内置示例(针对记忆 Agent):`MemoryRecallQuality`(是否正确使用长期记忆:正确使用 / 部分使用 / 召回失败 / 编造)、`MemoryAnswerMatchesReference`(回放专用,对照参考答案)、`StrictHelpfulness`(derived)、`NoImplementationLeak`、`AnswerInChinese`(code)。

```bash
python scripts/run_eval.py --worker-arn $EVAL_ARN list                                   # 查看所有可用评估器
python scripts/run_eval.py --worker-arn $EVAL_ARN --evaluators Helpfulness,MemoryRecallQuality sessions --runtime-id $RUNTIME_ID
python scripts/run_eval.py --worker-arn $EVAL_ARN --evaluators "custom:*" replay ...       # custom:* = 全部自定义评估器
# 内联传入新定义(只允许 judge / derived),不需要重新部署 worker:
python scripts/run_eval.py --worker-arn $EVAL_ARN --custom-file evaluation/examples/inline_evaluators.yaml \
  --evaluators PersonalizedTone,ConciseAnswer sessions --runtime-id $RUNTIME_ID
```
- 新增或修改 `code` 评估器、或修改 `custom_evaluators.yaml`,需要重新打包部署 worker(第 6 步)。
- 安全约束:`code` 只能引用打包在 `evaluation/custom_code/` 里的函数,请求里内联的定义不能是 `code`,避免通过评估请求执行任意代码。
- 名称不能与内置评估器重名;定义有误时 worker 直接返回全部错误,不会去调用被测 Agent。
- 离线、回放、在线三种模式都能使用自定义评估器;`needs_reference: true` 的只在回放时运行。

## 安全说明
- `.env` 含密钥,已在 `.gitignore`;**demo 的 `deploy_runtime.py` 会把 `LLM_API_KEY` 放进 Runtime 环境变量**。生产请改为 `LLM_API_KEY_SECRET_ID=<Secrets Manager 名称>`(`common/secrets.py` 支持任一配置项的 `_SECRET_ID/_SECRET_KEY` 后缀),执行角色已允许读 `agent-demo/*` 前缀的密钥。
- `user_id` 目前来自 payload,调用方可伪造。生产请启用 Runtime 的 JWT 入站认证并从 claims 取 user_id。
- 数据库只允许 Runtime 安全组访问,不对公网开放。

## 清理
```bash
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id $RUNTIME_ID
aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id $EVAL_RUNTIME_ID
aws cloudformation delete-stack --stack-name memdemo      # Aurora 会留快照;S3 桶非空需先清空
aws cloudformation delete-stack --stack-name memdemo-net  # 含 NAT 网关(持续计费)
```

## 已知限制
- 评估使用 `strands-agents-evals` 的默认 CloudWatch 映射;含多次工具调用的轮次,"最终回答"的提取可能取到工具调用前的铺垫文本(Launchpad 为此重写了 mapper)。
- 在线评估是轮询式,不是托管的 Online Evaluation 服务。
- 自动记忆把检索结果放在 system prompt 里,Faithfulness 类裁判看不到 system prompt,可能把"来自记忆的事实"判为无依据,解读分数时注意。
- 网关背后若用临时凭证生成的短期 Bedrock Key,会随 STS 会话一起过期(实测约 6~12 小时),届时 Agent 和裁判都会返回 403。
- 裁判模型为推理型模型(如 kimi-k3)时,`JUDGE_MAX_TOKENS` 需要足够大,否则裁判输出被截断。
