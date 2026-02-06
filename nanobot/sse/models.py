"""SSE data models for request, response, and message schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class SSERequest(BaseModel):
    """Unified SSE Agent request."""

    session_id: str = Field(..., description="会话ID，用于标识一个完整的对话会话")
    request_id: str = Field(..., description="请求ID，每次请求唯一")
    agent_type: str = Field(
        default="agent",
        description="Agent类型: agent(对话智能体) | workflow(工作流，预留)",
    )
    skill_list: list[str] = Field(
        default_factory=lambda: ["*"],
        description="技能列表，默认[\"*\"]表示全部",
    )
    tool_list: list[str] = Field(
        default_factory=lambda: ["*"],
        description="工具列表，默认[\"*\"]表示全部",
    )
    workflow_list: list[str] = Field(
        default_factory=list,
        description="工作流列表（预留）",
    )
    message: list[dict[str, Any]] = Field(
        ...,
        description="OpenAI格式的消息列表，支持文本和多模态",
    )
    stream: bool = Field(default=True, description="是否流式输出")
    enable_thinking: bool = Field(default=False, description="是否启用思考过程输出")


# ---------------------------------------------------------------------------
# SSE Message body
# ---------------------------------------------------------------------------

class SSEMessageBody(BaseModel):
    """SSE消息体 — message 字段的结构."""

    content: str | None = Field(default=None, description="文本内容（非流式时为完整内容）")
    files: list[dict[str, Any]] | None = Field(default=None, description="文件结果列表")
    delta: str | None = Field(default=None, description="流式增量文本")
    tool_name: str | None = Field(default=None, description="工具名称（message_type=tool时）")
    tool_arguments: dict[str, Any] | None = Field(default=None, description="工具参数")
    tool_result: str | None = Field(default=None, description="工具执行结果")


# ---------------------------------------------------------------------------
# SSE Message
# ---------------------------------------------------------------------------

class SSEMessage(BaseModel):
    """单条SSE事件消息."""

    stream: bool = Field(..., description="是否流式")
    session_id: str = Field(..., description="会话ID")
    request_id: str = Field(..., description="请求ID")
    message_id: str = Field(..., description="消息ID（流式时同一消息共享此ID）")
    message_order: int = Field(..., description="消息序号（从1开始递增）")
    event_type: str = Field(default="agent", description="事件类型: agent | workflow")
    status: str = Field(
        default="processing",
        description="状态: processing | completed | error | tool_calling",
    )
    message_type: str = Field(
        ...,
        description="消息类型: text | html | tool | tool_result | thought | error | done",
    )
    error: str | None = Field(default=None, description="错误信息")
    message: SSEMessageBody | None = Field(default=None, description="消息体")

    def to_sse_string(self) -> str:
        """Serialize to an SSE ``data:`` line."""
        return f"data: {self.model_dump_json(exclude_none=True)}\n\n"
