"""短期记忆: SlidingWindow 控制上下文 + S3 持久化完整会话。

AgentCore Runtime 的 microVM 在空闲后会被回收, 内存中的对话历史不可靠;
用 S3SessionManager 以 runtime session id 为键落盘, 下一次调用同一 session 时自动恢复。
未配置 SESSION_S3_BUCKET 时退化为本地文件 (仅本地开发)。
"""
from __future__ import annotations

import boto3
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.session.file_session_manager import FileSessionManager
from strands.session.s3_session_manager import S3SessionManager

from common.secrets import aws_region, get_setting


def build_session_manager(session_id: str):
    bucket = get_setting("SESSION_S3_BUCKET")
    if bucket:
        region = aws_region()
        return S3SessionManager(
            session_id=session_id,
            bucket=bucket,
            prefix=get_setting("SESSION_S3_PREFIX", "sessions/"),
            boto_session=boto3.Session(region_name=region),
            region_name=region,
        )
    return FileSessionManager(
        session_id=session_id, storage_dir=get_setting("SESSION_FILE_DIR", "/tmp/sessions")
    )


def build_conversation_manager() -> SlidingWindowConversationManager:
    return SlidingWindowConversationManager(
        window_size=int(get_setting("CONVERSATION_WINDOW_SIZE", "20"))
    )
