"""LiteLLM provider implementation for multi-provider support."""

import json
import os
from typing import Any, AsyncIterator

import litellm
from litellm import acompletion
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest, StreamDelta
from nanobot.providers.registry import find_by_model, find_gateway


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.
    
    Supports OpenRouter, Anthropic, OpenAI, Gemini, Ollama and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """
    
    def __init__(
        self, 
        api_key: str | None = None, 
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        
        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)
        
        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)
        
        # Set global api_base for non-Ollama providers
        if api_base:
            litellm.api_base = api_base
        
        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True
    
    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)
    
    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model
        
        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"
        
        return model
    
    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return
    
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
        model = self._resolve_model(model or self.default_model)
        
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)
        
        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key
        
        # Pass api_base for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base
        
        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        
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
        
        reasoning_content = getattr(message, "reasoning_content", None)
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
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

        # Extended thinking support: provider-specific parameters
        if enable_thinking:
            if model.startswith("dashscope/"):
                # DashScope (通义): enable_thinking + thinking_budget
                # Pass via extra_body (OpenAI-compatible endpoint) and as kwargs (LiteLLM forwards to body)
                budget = min(max_tokens, 10000)
                kwargs["extra_body"] = {
                    "enable_thinking": True,
                    "thinking_budget": budget,
                }
                kwargs["enable_thinking"] = True
                kwargs["thinking_budget"] = budget
            else:
                # Anthropic Claude 3.5+ etc.: thinking block
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
