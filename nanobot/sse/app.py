"""FastAPI application providing the SSE unified Agent endpoint.

Usage (standalone)::

    uvicorn nanobot.sse.app:create_app --factory --host 0.0.0.0 --port 18790

Or via the CLI::

    nanobot sse --port 18790
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger

from nanobot.sse.handler import SSEHandler
from nanobot.sse.models import SSERequest


def create_app(
    handler: SSEHandler | None = None,
    *,
    title: str = "Nanobot SSE Gateway",
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build and return a configured FastAPI application.

    Parameters
    ----------
    handler:
        A pre-built :class:`SSEHandler`.  When *None* (the default),
        the app creates its own handler from the global config on
        startup — useful for ``uvicorn --factory`` usage.
    cors_origins:
        Allowed CORS origins.  Defaults to ``["*"]`` for ease of
        development; tighten in production.
    """

    app = FastAPI(title=title, version="0.1.0")

    # -- CORS ---------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- State holder -------------------------------------------------------
    # We store the handler in app.state so the route closure can access it.
    app.state.sse_handler = handler  # may be None until startup

    # -- Startup hook -------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:
        if app.state.sse_handler is not None:
            logger.info("SSE handler already initialised")
            return

        # Lazy build from config (for uvicorn --factory path)
        from nanobot.config.loader import load_config
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.litellm_provider import LiteLLMProvider
        from nanobot.agent.loop import AgentLoop

        config = load_config()
        bus = MessageBus()
        provider = LiteLLMProvider(
            api_key=config.get_api_key(),
            api_base=config.get_api_base(),
            default_model=config.agents.defaults.model,
        )
        agent = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            max_iterations=config.agents.defaults.max_tool_iterations,
            brave_api_key=config.tools.web.search.api_key or None,
            exec_config=config.tools.exec,
            oss_config=config.tools.oss,
        )
        app.state.sse_handler = SSEHandler(agent)
        logger.info("SSE handler built from config")

    # -- Routes -------------------------------------------------------------

    @app.post("/v1/chat/completions")
    async def sse_chat(request: SSERequest) -> StreamingResponse:
        """Unified SSE Agent entry point.

        Accepts an :class:`SSERequest` body and returns a streaming SSE
        response.  Even in non-streaming mode the transport is SSE — the
        difference is that non-streaming returns fewer, larger events.
        """
        handler: SSEHandler = app.state.sse_handler  # type: ignore[assignment]

        async def _generate():
            async for event in handler.handle(request):
                yield event

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/health")
    async def health():
        """Simple health-check endpoint."""
        return {"status": "ok", "service": "nanobot-sse"}

    return app
