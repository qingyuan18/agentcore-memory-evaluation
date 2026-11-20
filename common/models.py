"""按环境变量前缀构造 Strands 模型 (Agent 用 ``LLM_``, 评估裁判用 ``JUDGE_``)。

中国区没有原生 Bedrock 模型服务, 两种常用方式:
  - ``openai_compatible``: DeepSeek / 通义千问(DashScope) / 智谱 / SiliconFlow 等
    OpenAI 协议端点 (默认, 国内网络直连)
  - ``bedrock``: 通过 Bedrock API Key (bearer token) 调用海外区 Bedrock,
    需要 Runtime / 评估进程能访问海外区端点 (注意数据出境合规)

环境变量 (以 PREFIX=LLM 为例):
  LLM_PROVIDER      openai_compatible | bedrock          (默认 openai_compatible)
  LLM_MODEL         模型名 / Bedrock model id
  LLM_BASE_URL      openai_compatible 的 base_url
  LLM_API_KEY       API Key (或 LLM_API_KEY_SECRET_ID 走 Secrets Manager)
  LLM_BEDROCK_REGION  bedrock 的调用区域 (默认 us-west-2)
  LLM_TEMPERATURE / LLM_MAX_TOKENS / LLM_SUPPORTS_TEMPERATURE(false=不下发 temperature)
"""
from __future__ import annotations

import os

from .secrets import get_setting


def _num(name: str, default: float) -> float:
    return float(get_setting(name, str(default)))


def supports_temperature(prefix: str) -> bool:
    """部分模型(如 Bedrock 上的 Kimi K3)拒绝 temperature 参数, 设 {PREFIX}_SUPPORTS_TEMPERATURE=false 即不再下发。"""
    return get_setting(f"{prefix}_SUPPORTS_TEMPERATURE", "true").lower() != "false"


def build_strands_model(prefix: str = "LLM"):
    provider = get_setting(f"{prefix}_PROVIDER", "openai_compatible")
    model_id = get_setting(f"{prefix}_MODEL")
    if not model_id:
        raise ValueError(f"{prefix}_MODEL 未配置")
    temperature = _num(f"{prefix}_TEMPERATURE", 0.3)
    max_tokens = int(_num(f"{prefix}_MAX_TOKENS", 2048))
    sampling = {"temperature": temperature} if supports_temperature(prefix) else {}

    if provider == "openai_compatible":
        from strands.models.openai import OpenAIModel

        api_key = get_setting(f"{prefix}_API_KEY")
        base_url = get_setting(f"{prefix}_BASE_URL")
        if not api_key or not base_url:
            raise ValueError(f"{prefix}_API_KEY / {prefix}_BASE_URL 未配置")
        return OpenAIModel(
            client_args={"api_key": api_key, "base_url": base_url},
            model_id=model_id,
            params={**sampling, "max_tokens": max_tokens},
        )

    if provider == "bedrock":
        from strands.models import BedrockModel

        token = get_setting(f"{prefix}_BEDROCK_API_KEY")
        if token:
            # botocore 读取该变量后对 bedrock-runtime 使用 bearer token 鉴权
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token
        return BedrockModel(
            model_id=model_id,
            region_name=get_setting(f"{prefix}_BEDROCK_REGION", "us-west-2"),
            max_tokens=max_tokens,
            **sampling,
        )

    raise ValueError(f"未知的 {prefix}_PROVIDER: {provider}")
