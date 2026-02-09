"""Base LLM provider interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    
    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


@dataclass
class StreamDelta:
    """A single streaming chunk from the LLM.

    Exactly one of the content fields will be non-None per chunk.
    """

    # Text content delta
    content_delta: str | None = None
    # Thinking / reasoning delta
    thinking_delta: str | None = None
    # Incremental tool-call fields
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments_delta: str | None = None
    # Finish reason (set on the last chunk)
    finish_reason: str | None = None
    # Usage (may appear on the last chunk)
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.
    
    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """
    
    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
    
    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        enable_thinking: bool = False,
    ) -> AsyncIterator[StreamDelta]:
        """
        Send a streaming chat completion request.

        Default implementation falls back to non-streaming ``chat()``,
        yielding a single delta with the full content.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            enable_thinking: Whether to request extended thinking.

        Yields:
            StreamDelta chunks.
        """
        response = await self.chat(messages, tools, model, max_tokens, temperature)
        if response.reasoning_content:
            yield StreamDelta(thinking_delta=response.reasoning_content)
        if response.content:
            yield StreamDelta(content_delta=response.content)
        for tc in response.tool_calls:
            import json as _json
            yield StreamDelta(
                tool_call_index=0,
                tool_call_id=tc.id,
                tool_call_name=tc.name,
                tool_call_arguments_delta=_json.dumps(tc.arguments),
            )
        yield StreamDelta(finish_reason=response.finish_reason, usage=response.usage)
    
    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
