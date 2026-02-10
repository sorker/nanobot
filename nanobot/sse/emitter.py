"""SSE message emitter — builds well-formed SSE events for the client.

Each public ``emit_*`` method returns a ready-to-send SSE string
(``data: {...}\\n\\n``).  The emitter is stateful: it tracks message
ordering and the current *message_id* through the bound
:class:`RequestContext`.
"""

from __future__ import annotations

from nanobot.sse.context import RequestContext
from nanobot.sse.models import SSEMessage, SSEMessageBody


class SSEEmitter:
    """Construct and serialise SSE events for a single request lifecycle."""

    def __init__(self, ctx: RequestContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build(
        self,
        message_type: str,
        status: str = "processing",
        message: SSEMessageBody | None = None,
        error: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Build and return an SSE ``data:`` line."""
        msg = SSEMessage(
            stream=self.ctx.stream,
            session_id=self.ctx.session_id,
            request_id=self.ctx.request_id,
            message_id=message_id or self.ctx.current_message_id,
            message_order=self.ctx.next_order(),
            event_type=self.ctx.agent_type,
            status=status,
            message_type=message_type,
            error=error,
            message=message,
        )
        return msg.to_sse_string()

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------

    def emit_text_delta(self, delta: str, message_id: str | None = None) -> str:
        """Streaming text delta."""
        return self._build(
            message_type="text",
            message=SSEMessageBody(delta=delta),
            message_id=message_id,
        )

    def emit_text_complete(self, content: str, message_id: str | None = None) -> str:
        """Complete text message (non-streaming mode)."""
        return self._build(
            message_type="text",
            status="completed",
            message=SSEMessageBody(content=content),
            message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Thinking / Reasoning
    # ------------------------------------------------------------------

    def emit_thinking_delta(self, delta: str, message_id: str | None = None) -> str:
        """Streaming thinking/reasoning delta."""
        return self._build(
            message_type="thought",
            message=SSEMessageBody(delta=delta),
            message_id=message_id,
        )

    def emit_thinking_complete(self, content: str, message_id: str | None = None) -> str:
        """Complete thinking block."""
        return self._build(
            message_type="thought",
            status="completed",
            message=SSEMessageBody(content=content),
            message_id=message_id,
        )

    # ------------------------------------------------------------------
    # HTML (streaming content generation)
    # ------------------------------------------------------------------

    def emit_html_delta(self, delta: str, message_id: str | None = None) -> str:
        """Streaming HTML content delta (e.g. lesson-plan generation)."""
        return self._build(
            message_type="html",
            message=SSEMessageBody(delta=delta),
            message_id=message_id,
        )

    def emit_html_complete(self, content: str, message_id: str | None = None) -> str:
        """Complete HTML content."""
        return self._build(
            message_type="html",
            status="completed",
            message=SSEMessageBody(content=content),
            message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Files / Images / Videos
    # ------------------------------------------------------------------

    def emit_files(
        self,
        files: list[dict],
        message_type: str = "file",
        message_id: str | None = None,
    ) -> str:
        """Send file(s) to the client.

        Args:
            files: [{file_name, file_url, file_type, file_desc, index?}]
            message_type: "image" | "file" | "video"
        """
        return self._build(
            message_type=message_type,
            message=SSEMessageBody(files=files),
            message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Progress (intermediate step messages during long tool execution)
    # ------------------------------------------------------------------

    def emit_progress(self, message: str, message_id: str | None = None) -> str:
        """Intermediate progress/step message during tool execution."""
        return self._build(
            message_type="text",
            message=SSEMessageBody(delta=f"💡 {message}\n"),
            message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Tool
    # ------------------------------------------------------------------

    def emit_tool_call(
        self,
        tool_name: str,
        arguments: dict | None = None,
        message_id: str | None = None,
    ) -> str:
        """Tool call start event."""
        return self._build(
            message_type="tool",
            status="tool_calling",
            message=SSEMessageBody(
                tool_name=tool_name,
                tool_arguments=arguments,
            ),
            message_id=message_id,
        )

    def emit_tool_result(
        self,
        tool_name: str,
        result: str,
        message_id: str | None = None,
    ) -> str:
        """Tool execution result event."""
        return self._build(
            message_type="tool_result",
            message=SSEMessageBody(
                tool_name=tool_name,
                tool_result=result,
            ),
            message_id=message_id,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def emit_done(self) -> str:
        """Processing complete signal."""
        return self._build(
            message_type="done",
            status="completed",
        )

    def emit_error(self, error: str) -> str:
        """Error event."""
        return self._build(
            message_type="error",
            status="error",
            error=error,
        )
