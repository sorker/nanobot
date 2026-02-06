"""SSE request handler — the unified Agent entry point.

This module wires together the request context, the agent loop and
the SSE emitter into a single coroutine that can be plugged into
any ASGI framework (FastAPI, Starlette, etc.).
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.sse.context import RequestContext
from nanobot.sse.emitter import SSEEmitter
from nanobot.sse.models import SSERequest


class SSEHandler:
    """Stateless handler — one instance per application, not per request."""

    def __init__(self, agent: AgentLoop) -> None:
        self.agent = agent

    async def handle(self, request: SSERequest) -> AsyncIterator[str]:
        """Process an :class:`SSERequest` and yield SSE event strings.

        This is the single entry point that the HTTP layer calls.

        Args:
            request: Validated SSE request from the client.

        Yields:
            ``data: {...}\\n\\n`` strings ready to be written to the
            HTTP response body.
        """
        # 1. Build request context
        ctx = RequestContext.from_request(request)

        logger.info(
            f"SSE request: session={ctx.session_id} request={ctx.request_id} "
            f"agent_type={ctx.agent_type} stream={ctx.stream} "
            f"thinking={ctx.enable_thinking}"
        )

        # 2. Build emitter
        emitter = SSEEmitter(ctx)

        # 3. Validate agent_type
        if ctx.agent_type == "workflow":
            yield emitter.emit_error("workflow agent type is not yet implemented")
            yield emitter.emit_done()
            return

        if ctx.agent_type != "agent":
            yield emitter.emit_error(f"unknown agent_type: {ctx.agent_type}")
            yield emitter.emit_done()
            return

        # 4. Delegate to agent loop
        async for event in self.agent.process_sse(
            ctx=ctx,
            emitter=emitter,
            openai_messages=request.message,
        ):
            yield event
