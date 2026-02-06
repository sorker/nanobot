"""Request context management for SSE processing pipeline.

Each incoming SSE request creates a ``RequestContext`` that carries all
metadata (session_id, request_id, stream mode, etc.) through the entire
agent processing chain — from receiving the HTTP request, through the
agent loop, all the way to sending SSE events back to the client.

**message_id semantics**

Within a single request (``request_id``), there are typically multiple
ReAct cycles.  Each cycle represents one complete "action":

    think → tool_call → tool_result → (repeat) → final answer

Every ReAct cycle is assigned a unique ``message_id``.  All SSE events
within the *same* cycle (thought, tool, tool_result …) share that
``message_id`` so the client can group them together.  The next cycle
receives a new ``message_id`` via :meth:`new_message_id`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestContext:
    """Immutable* context for a single SSE request.

    Attributes that may change during processing (message_counter,
    current_message_id) are managed via helper methods to avoid
    uncoordinated mutation.
    """

    # --- identifiers -------------------------------------------------------
    session_id: str
    request_id: str

    # --- request parameters ------------------------------------------------
    agent_type: str = "agent"
    skill_list: list[str] = field(default_factory=lambda: ["*"])
    tool_list: list[str] = field(default_factory=lambda: ["*"])
    workflow_list: list[str] = field(default_factory=list)
    stream: bool = True
    enable_thinking: bool = False

    # --- runtime state (mutable) -------------------------------------------
    _message_order: int = field(default=0, init=False, repr=False)
    _current_message_id: str | None = field(default=None, init=False, repr=False)
    _metadata: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    # --- helpers -----------------------------------------------------------

    def next_order(self) -> int:
        """Return the next message order number (1-based, monotonically increasing)."""
        self._message_order += 1
        return self._message_order

    def new_message_id(self) -> str:
        """Generate and cache a new message ID.

        Called once per ReAct cycle so that all events within the same
        cycle (thought, tool call, tool result, text deltas …) share
        the same ``message_id``.
        """
        self._current_message_id = uuid.uuid4().hex[:16]
        return self._current_message_id

    @property
    def current_message_id(self) -> str:
        """Return the current (or a freshly created) message ID."""
        if self._current_message_id is None:
            return self.new_message_id()
        return self._current_message_id

    def set_metadata(self, key: str, value: Any) -> None:
        self._metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        return self._metadata.get(key, default)

    @property
    def session_key(self) -> str:
        """Compatibility property — maps to the bus-based session key format."""
        return f"sse:{self.session_id}"

    # --- factory -----------------------------------------------------------

    @classmethod
    def from_request(cls, req: "SSERequest") -> "RequestContext":
        """Build a *RequestContext* from an :class:`SSERequest`."""
        from nanobot.sse.models import SSERequest  # noqa: F811 — local import to avoid circular

        return cls(
            session_id=req.session_id,
            request_id=req.request_id,
            agent_type=req.agent_type,
            skill_list=req.skill_list,
            tool_list=req.tool_list,
            workflow_list=req.workflow_list,
            stream=req.stream,
            enable_thinking=req.enable_thinking,
        )
