"""构造 Mem0 配置: LLM(抽取事实) + Embedder(向量化) + pgvector(Aurora PostgreSQL)。

LLM 复用 Agent 的 ``LLM_*`` 配置; Embedding 使用 ``EMBED_*``:
  EMBED_PROVIDER   openai_compatible | bedrock   (默认 openai_compatible)
  EMBED_MODEL      如 text-embedding-v3 (DashScope) / amazon.titan-embed-text-v2:0
  EMBED_BASE_URL   openai_compatible 的 base_url
  EMBED_API_KEY    (或 EMBED_API_KEY_SECRET_ID)
  EMBED_DIMS       向量维度, 默认 1024

向量库 (Aurora PostgreSQL + pgvector):
  PG_HOST / PG_PORT / PG_DBNAME / PG_USER / PG_PASSWORD(或 PG_PASSWORD_SECRET_ID)
"""
from __future__ import annotations

from typing import Any, Dict

from common.models import supports_temperature
from common.secrets import get_setting


def _llm_config() -> Dict[str, Any]:
    provider = get_setting("LLM_PROVIDER", "openai_compatible")
    temperature = float(get_setting("LLM_TEMPERATURE", "0.1"))
    max_tokens = int(get_setting("LLM_MAX_TOKENS", "2000"))
    if provider == "openai_compatible":
        cfg = {
            "model": get_setting("LLM_MODEL"),
            "openai_base_url": get_setting("LLM_BASE_URL"),
            "api_key": get_setting("LLM_API_KEY"),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if not supports_temperature("LLM"):
            # Mem0 的 "推理模型" 参数集会丢掉 temperature / max_tokens, 避免被 Kimi K3 等模型拒绝
            cfg["is_reasoning_model"] = True
        return {"provider": "openai", "config": cfg}
    if provider == "bedrock":
        return {
            "provider": "aws_bedrock",
            "config": {
                "model": get_setting("LLM_MODEL"),
                "aws_region": get_setting("LLM_BEDROCK_REGION", "us-west-2"),
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        }
    raise ValueError(f"未知的 LLM_PROVIDER: {provider}")


def _embedder_config() -> Dict[str, Any]:
    provider = get_setting("EMBED_PROVIDER", "openai_compatible")
    dims = int(get_setting("EMBED_DIMS", "1024"))
    if provider == "openai_compatible":
        return {
            "provider": "openai",
            "config": {
                "model": get_setting("EMBED_MODEL"),
                "openai_base_url": get_setting("EMBED_BASE_URL"),
                "api_key": get_setting("EMBED_API_KEY"),
                "embedding_dims": dims,
            },
        }
    if provider == "bedrock":
        return {
            "provider": "aws_bedrock",
            "config": {
                "model": get_setting("EMBED_MODEL", "amazon.titan-embed-text-v2:0"),
                "aws_region": get_setting("EMBED_BEDROCK_REGION", "us-west-2"),
                "embedding_dims": dims,
            },
        }
    raise ValueError(f"未知的 EMBED_PROVIDER: {provider}")


def _vector_store_config() -> Dict[str, Any]:
    return {
        "provider": "pgvector",
        "config": {
            "host": get_setting("PG_HOST"),
            "port": int(get_setting("PG_PORT", "5432")),
            "dbname": get_setting("PG_DBNAME", "mem0"),
            "user": get_setting("PG_USER", "postgres"),
            # Mem0 把密码拼进 PostgreSQL URI, 密码须为纯字母数字 (CFN 模板已保证)
            "password": get_setting("PG_PASSWORD"),
            "collection_name": get_setting("PG_COLLECTION", "memories"),
            "embedding_model_dims": int(get_setting("EMBED_DIMS", "1024")),
            "sslmode": get_setting("PG_SSLMODE", "require"),
            "hnsw": True,
        },
    }


def build_mem0_config() -> Dict[str, Any]:
    return {
        "llm": _llm_config(),
        "embedder": _embedder_config(),
        "vector_store": _vector_store_config(),
        # Runtime 容器仅 /tmp 可写
        "history_db_path": get_setting("MEM0_HISTORY_DB", "/tmp/mem0_history.db"),
        "version": "v1.1",
    }
