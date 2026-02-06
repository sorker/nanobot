"""LiteLLM provider implementation for multi-provider support."""

import json
import os
from typing import Any, AsyncIterator

import litellm
from litellm import acompletion
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, StreamDelta, ToolCallRequest


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, Ollama, and many other providers through
    a unified interface.
    """
    
    def __init__(
        self, 
        api_key: str | None = None, 
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5"
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        
        # Configure LiteLLM based on provider
        if api_key:
            if "openrouter" in default_model:
                # OpenRouter mode - set key
                os.environ["OPENROUTER_API_KEY"] = api_key
            elif "ollama" in default_model:
                # Ollama usually doesn't need API key, but set a placeholder
                os.environ.setdefault("OLLAMA_API_KEY", api_key or "ollama")
                os.environ["OLLAMA_API_BASE"] = api_base
            elif "vllm" in default_model:
                # vLLM/custom endpoint - uses OpenAI-compatible API
                os.environ["OPENAI_API_KEY"] = api_key
            elif "deepseek" in default_model:
                os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
            elif "anthropic" in default_model:
                os.environ.setdefault("ANTHROPIC_API_KEY", api_key)
            elif "openai" in default_model or "gpt" in default_model:
                os.environ.setdefault("OPENAI_API_KEY", api_key)
            elif "gemini" in default_model.lower():
                os.environ.setdefault("GEMINI_API_KEY", api_key)
            elif "zhipu" in default_model or "glm" in default_model or "zai" in default_model:
                os.environ.setdefault("ZHIPUAI_API_KEY", api_key)
            elif "dashscope" in default_model:
                os.environ.setdefault("DASHSCOPE_API_KEY", api_key)
            elif "groq" in default_model:
                os.environ.setdefault("GROQ_API_KEY", api_key)
        
        # Set global api_base for non-Ollama providers
        if api_base:
            litellm.api_base = api_base
        
        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
    
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
        
        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = model or self.default_model
               
        # For Zhipu/Z.ai, ensure prefix is present
        # Handle cases like "glm-4.7-flash" -> "zai/glm-4.7-flash"
        if ("glm" in model.lower() or "zhipu" in model.lower()) and not (
            model.startswith("zhipu/") or 
            model.startswith("zai/") or 
            model.startswith("openrouter/") or
            model.startswith("ollama/")
        ):
            model = f"zai/{model}"
        
        # For vLLM, use hosted_vllm/ prefix per LiteLLM docs
        # Convert openai/ prefix to hosted_vllm/ if user specified it
        if "vllm" in model.lower():
            model = f"hosted_vllm/{model}"
        
        # For Gemini, ensure gemini/ prefix if not already present
        if "gemini" in model.lower() and not model.startswith("gemini/"):
            model = f"gemini/{model}"
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Pass api_base directly for custom endpoints (vLLM, etc.)
        if self.api_base:
            kwargs["api_base"] = self.api_base
        
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
        try:
            response = await acompletion(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            # Return error as content for graceful handling
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )
    
    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                if isinstance(args, str):
                    import json
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                
                tool_calls.append(ToolCallRequest(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
    
    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _prepare_model(self, model: str | None) -> str:
        """Apply provider-specific model name transformations."""
        model = model or self.default_model

        # Zhipu / Z.ai prefix
        if ("glm" in model.lower() or "zhipu" in model.lower()) and not (
            model.startswith("zhipu/")
            or model.startswith("zai/")
            or model.startswith("openrouter/")
            or model.startswith("ollama/")
        ):
            model = f"zai/{model}"

        # vLLM prefix
        if "vllm" in model.lower():
            model = f"hosted_vllm/{model}"

        # Gemini prefix
        if "gemini" in model.lower() and not model.startswith("gemini/"):
            model = f"gemini/{model}"

        return model

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
        Send a *streaming* chat completion request via LiteLLM.

        Yields :class:`StreamDelta` objects that the caller can
        convert to SSE events.
        """
        model = self._prepare_model(model)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        if self.api_base:
            kwargs["api_base"] = self.api_base

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Extended thinking support (Claude 3.5+ with thinking)
        if enable_thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": min(max_tokens, 10000)}

        try:
            response = await acompletion(**kwargs)

            async for chunk in response:
                delta = self._parse_stream_chunk(chunk)
                if delta is not None:
                    yield delta

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield StreamDelta(content_delta=f"Error calling LLM: {str(e)}")
            yield StreamDelta(finish_reason="error")

    def _parse_stream_chunk(self, chunk: Any) -> StreamDelta | None:
        """Parse a single streaming chunk into a :class:`StreamDelta`."""
        try:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                return None

            delta = getattr(choice, "delta", None)
            if delta is None:
                return None

            # --- thinking / reasoning content ---
            # Some providers expose thinking as `reasoning_content`
            thinking = getattr(delta, "reasoning_content", None)
            if thinking:
                return StreamDelta(thinking_delta=thinking)

            # --- text content ---
            content = getattr(delta, "content", None)
            if content:
                return StreamDelta(content_delta=content)

            # --- tool calls ---
            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                tc = tool_calls[0]
                func = getattr(tc, "function", None)
                return StreamDelta(
                    tool_call_index=getattr(tc, "index", 0),
                    tool_call_id=getattr(tc, "id", None),
                    tool_call_name=getattr(func, "name", None) if func else None,
                    tool_call_arguments_delta=getattr(func, "arguments", None) if func else None,
                )

            # --- finish reason ---
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason:
                usage = {}
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }
                return StreamDelta(finish_reason=finish_reason, usage=usage)

            return None
        except Exception:
            return None

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
