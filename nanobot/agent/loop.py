"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, StreamDelta
from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.oss import OSSUploadFileTool, OSSUploadTextTool
from nanobot.agent.tools.cron import CronTool

# 触发 AUTO_REGISTER_DEPS 工具的模块加载（确保 __subclasses__ 可被发现）
import nanobot.agent.tools  # noqa: F401
from nanobot.agent.subagent import SubagentManager
from nanobot.session.manager import SessionManager
from nanobot.utils.oss_service import OSSService
from nanobot.cron.service import CronService

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig, OSSConfig
    from nanobot.sse.context import RequestContext
    from nanobot.sse.emitter import SSEEmitter


class AgentLoop:
    """
    The agent loop is the core processing engine.
    
    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """
    
    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        oss_config: "OSSConfig | None" = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.oss_config = oss_config
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        
        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )
        
        self._running = False
        self._register_default_tools()
    
    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (restrict to workspace if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(allowed_dir=allowed_dir))
        
        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
        ))
        
        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        
        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)
        
        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)
        
        # OSS tools
        oss_service = OSSService(self.oss_config)
        if oss_service.is_enabled():
            self.tools.register(OSSUploadFileTool(oss_service))
            self.tools.register(OSSUploadTextTool(oss_service))

        # 自动注册声明了 AUTO_REGISTER_DEPS 的工具（如 EduSVGTool, EduDocTool）
        self.tools.auto_register_all({
            "provider": self.provider,
            "oss_service": oss_service,
        })
        
        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
    
    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")
        
        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                
                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue
    
    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")
    
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.
        
        Args:
            msg: The inbound message to process.
        
        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        if msg.channel == "system":
            return await self._process_system_message(msg)
        
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")
        
        # Get or create session
        session = self.sessions.get_or_create(msg.session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(msg.channel, msg.chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(msg.channel, msg.chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(msg.channel, msg.chat_id)
        
        # Build initial messages (use get_history for LLM-formatted messages)
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        
        # Agent loop
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            # Call LLM
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            # Handle tool calls
            if response.has_tool_calls:
                # Add assistant message with tool calls
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)  # Must be JSON string
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )
                
                # Execute tools
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    
                    # Send tool execution event to client
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"正在执行工具: {tool_call.name}",
                        metadata={
                            "type": "tool",
                            "tool_name": tool_call.name,
                            "arguments": tool_call.arguments
                        }
                    ))
                    
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                # No tool calls, we're done
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "I've completed processing but have no response to give."
        
        # Log response preview
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")
        
        # Save to session
        session.add_message("user", msg.content)
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},  # Pass through for channel-specific needs (e.g. Slack thread_ts)
        )
    
    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).
        
        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")
        
        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id
        
        # Use the origin session for context
        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        
        # Update tool contexts
        message_tool = self.tools.get("message")
        if isinstance(message_tool, MessageTool):
            message_tool.set_context(origin_channel, origin_chat_id)
        
        spawn_tool = self.tools.get("spawn")
        if isinstance(spawn_tool, SpawnTool):
            spawn_tool.set_context(origin_channel, origin_chat_id)
        
        cron_tool = self.tools.get("cron")
        if isinstance(cron_tool, CronTool):
            cron_tool.set_context(origin_channel, origin_chat_id)
        
        # Build messages with the announce content
        messages = self.context.build_messages(
            history=session.get_history(),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        
        # Agent loop (limited for announce handling)
        iteration = 0
        final_content = None
        
        while iteration < self.max_iterations:
            iteration += 1
            
            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model
            )
            
            if response.has_tool_calls:
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )
                
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"Tool call: {tool_call.name}({args_str[:200]})")
                    
                    # Send tool execution event to client
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=origin_channel,
                        chat_id=origin_chat_id,
                        content=f"正在执行工具: {tool_call.name}",
                        metadata={
                            "type": "tool",
                            "tool_name": tool_call.name,
                            "arguments": tool_call.arguments
                        }
                    ))
                    
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                final_content = response.content
                break
        
        if final_content is None:
            final_content = "Background task completed."
        
        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)
        
        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )
    
    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).
        
        Args:
            content: The message content.
            session_key: Session identifier.
            channel: Source channel (for context).
            chat_id: Source chat ID (for context).
        
        Returns:
            The agent's response.
        """
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )
        
        response = await self._process_message(msg)
        return response.content if response else ""

    # ==================================================================
    # SSE streaming / non-streaming processing
    # ==================================================================

    async def process_sse(
        self,
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        openai_messages: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        """Process a request and yield SSE event strings.

        This is the unified entry point used by the SSE HTTP handler.
        It supports both streaming and non-streaming modes controlled
        by ``ctx.stream``.

        Args:
            ctx: The request context carrying session_id, request_id, etc.
            emitter: The SSE emitter bound to this request context.
            openai_messages: OpenAI-format message list from the client.

        Yields:
            Ready-to-send SSE ``data: ...\\n\\n`` strings.
        """
        from nanobot.sse.context import RequestContext
        from nanobot.sse.emitter import SSEEmitter

        try:
            # 1. Get or create session
            session = self.sessions.get_or_create(ctx.session_key)

            # 2. Build tool registry with optional filtering (supports glob patterns)
            tools_defs = self._get_filtered_tool_definitions(ctx.tool_list)

            # 3. Resolve skill patterns against available skills
            resolved_skills: list[str] | None = None
            if ctx.skill_list and ctx.skill_list != ["*"]:
                all_skill_names = self.context.skills.get_all_skill_names()
                resolved_skills = self._match_patterns(all_skill_names, ctx.skill_list)
                logger.debug(
                    f"Skill pattern matching: patterns={ctx.skill_list} "
                    f"→ resolved={resolved_skills}"
                )

            # 4. Build messages (system prompt + history + current)
            current_text, current_media = self._extract_last_user_message(openai_messages)
            messages = self.context.build_messages(
                history=session.get_history(),
                current_message=current_text,
                skill_names=resolved_skills,
                media=current_media,
            )

            # 5. Agent loop
            if ctx.stream:
                async for event in self._sse_stream_loop(
                    ctx, emitter, messages, tools_defs, session
                ):
                    yield event
            else:
                async for event in self._sse_non_stream_loop(
                    ctx, emitter, messages, tools_defs, session
                ):
                    yield event

            # 6. Done
            yield emitter.emit_done()

        except Exception as e:
            logger.error(f"SSE processing error: {e}")
            yield emitter.emit_error(str(e))
            yield emitter.emit_done()

    # ------------------------------------------------------------------
    # Streaming loop
    # ------------------------------------------------------------------

    async def _sse_stream_loop(
        self,
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        messages: list[dict[str, Any]],
        tools_defs: list[dict[str, Any]] | None,
        session: Any,
    ) -> AsyncIterator[str]:
        """Run the agent loop in *streaming* mode, yielding SSE events."""
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1

            # -- Generate a new message_id for this LLM turn --
            turn_msg_id = ctx.new_message_id()

            # -- Accumulate tool call fragments --
            pending_tool_calls: dict[int, dict[str, Any]] = {}
            accumulated_content: list[str] = []
            finish_reason: str | None = None

            async for delta in self.provider.stream_chat(
                messages=messages,
                tools=tools_defs,
                model=self.model,
                enable_thinking=ctx.enable_thinking,
            ):
                # Thinking delta
                if delta.thinking_delta and ctx.enable_thinking:
                    yield emitter.emit_thinking_delta(delta.thinking_delta, turn_msg_id)

                # Text content delta
                if delta.content_delta:
                    accumulated_content.append(delta.content_delta)
                    yield emitter.emit_text_delta(delta.content_delta, turn_msg_id)

                # Tool call deltas (accumulate)
                if delta.tool_call_index is not None:
                    idx = delta.tool_call_index
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {
                            "id": delta.tool_call_id or "",
                            "name": delta.tool_call_name or "",
                            "arguments_parts": [],
                        }
                    tc = pending_tool_calls[idx]
                    if delta.tool_call_id:
                        tc["id"] = delta.tool_call_id
                    if delta.tool_call_name:
                        tc["name"] = delta.tool_call_name
                    if delta.tool_call_arguments_delta:
                        tc["arguments_parts"].append(delta.tool_call_arguments_delta)

                # Finish
                if delta.finish_reason:
                    finish_reason = delta.finish_reason

            # -- End of this LLM turn --
            full_content = "".join(accumulated_content) or None

            # If tool calls present, execute them and continue the loop
            if pending_tool_calls:
                # Build assistant message with tool_calls
                tool_call_dicts = []
                for _idx, tc in sorted(pending_tool_calls.items()):
                    args_str = "".join(tc["arguments_parts"])
                    tool_call_dicts.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": args_str,
                        },
                    })

                messages = self.context.add_assistant_message(
                    messages, full_content, tool_call_dicts
                )

                # Execute each tool (all tool events in this ReAct cycle
                # share the same turn_msg_id)
                for tc_dict in tool_call_dicts:
                    tc_id = tc_dict["id"]
                    tc_name = tc_dict["function"]["name"]
                    tc_args_str = tc_dict["function"]["arguments"]

                    try:
                        tc_args = json.loads(tc_args_str) if tc_args_str else {}
                    except json.JSONDecodeError:
                        tc_args = {}

                    # Emit tool call event (same turn_msg_id)
                    yield emitter.emit_tool_call(tc_name, tc_args, turn_msg_id)

                    # Execute (with progress-event draining for supported tools)
                    result = None
                    tool_instance = self.tools.get(tc_name)
                    if tool_instance and hasattr(tool_instance, "_progress_queue"):
                        async for is_sse, value in self._execute_with_progress(
                            tool_instance, tc_name, tc_args, ctx, emitter, turn_msg_id,
                        ):
                            if is_sse:
                                # SSE event string — yield to client
                                yield value
                            else:
                                # Final tool result string
                                result = value
                    else:
                        result = await self.tools.execute(tc_name, tc_args)

                    # Emit tool result event (same turn_msg_id)
                    yield emitter.emit_tool_result(tc_name, result or "", turn_msg_id)

                    # Add tool result to messages
                    messages = self.context.add_tool_result(
                        messages, tc_id, tc_name, result or ""
                    )

                # Continue loop for next LLM turn
                continue

            # No tool calls — we're done
            if full_content:
                session.add_message("user", self._extract_last_user_message(
                    [m for m in messages if m.get("role") == "user"]
                )[0] or "")
                session.add_message("assistant", full_content)
                self.sessions.save(session)
            break

    # ------------------------------------------------------------------
    # Non-streaming loop
    # ------------------------------------------------------------------

    async def _sse_non_stream_loop(
        self,
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        messages: list[dict[str, Any]],
        tools_defs: list[dict[str, Any]] | None,
        session: Any,
    ) -> AsyncIterator[str]:
        """Run the agent loop in *non-streaming* mode, yielding SSE events."""
        iteration = 0
        final_content: str | None = None

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=tools_defs,
                model=self.model,
            )

            turn_msg_id = ctx.new_message_id()

            # Thinking (if available in non-stream response)
            if response.reasoning_content and ctx.enable_thinking:
                yield emitter.emit_thinking_complete(response.reasoning_content, turn_msg_id)

            if response.has_tool_calls:
                # Build assistant message
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts
                )

                # Execute each tool (all tool events in this ReAct cycle
                # share the same turn_msg_id)
                for tool_call in response.tool_calls:
                    yield emitter.emit_tool_call(
                        tool_call.name, tool_call.arguments, turn_msg_id
                    )

                    # Execute (with progress-event draining for supported tools)
                    result = None
                    tool_instance = self.tools.get(tool_call.name)
                    if tool_instance and hasattr(tool_instance, "_progress_queue"):
                        async for is_sse, value in self._execute_with_progress(
                            tool_instance, tool_call.name, tool_call.arguments, ctx, emitter, turn_msg_id,
                        ):
                            if is_sse:
                                yield value
                            else:
                                result = value
                    else:
                        result = await self.tools.execute(tool_call.name, tool_call.arguments)

                    yield emitter.emit_tool_result(
                        tool_call.name, result or "", turn_msg_id
                    )

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result or ""
                    )
                continue

            # No tool calls — final answer
            final_content = response.content
            break

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Emit the complete text in a single event
        yield emitter.emit_text_complete(final_content, ctx.new_message_id())

        # Save to session
        user_text = self._extract_last_user_message(
            [m for m in messages if m.get("role") == "user"]
        )[0] or ""
        session.add_message("user", user_text)
        session.add_message("assistant", final_content)
        self.sessions.save(session)

    # ------------------------------------------------------------------
    # Progress-reporting tool execution helper
    # ------------------------------------------------------------------

    async def _execute_with_progress(
        self,
        tool_instance: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        ctx: "RequestContext",
        emitter: "SSEEmitter",
        message_id: str,
    ) -> AsyncIterator[tuple[bool, str]]:
        """Execute a progress-reporting tool while draining its event queue.

        For tools that have a ``_progress_queue`` attribute (e.g. EduDocTool,
        EduSVGTool), this method:

        1. Injects ``session_id``, ``request_id``, and a progress queue.
        2. Runs the tool execution as an :func:`asyncio.create_task`.
        3. Concurrently drains the queue, yielding SSE events:
           - ``"step"`` events  → ``emitter.emit_progress()``
           - ``"html_delta"``   → ``emitter.emit_html_delta()``
        4. Once the task completes, drains any remaining events, then
           yields the final result string.

        Yields:
            Tuples of ``(is_sse_event, value)``:
            - ``(True, sse_string)`` for SSE events to forward to the client
            - ``(False, result_string)`` for the final tool result (always last)
        """
        queue: asyncio.Queue = asyncio.Queue()
        tool_instance._progress_queue = queue

        # Inject SSE context identifiers for OSS object_key construction
        if hasattr(tool_instance, "_session_id"):
            tool_instance._session_id = ctx.session_id
        if hasattr(tool_instance, "_request_id"):
            tool_instance._request_id = ctx.request_id

        # Launch tool execution concurrently
        exec_task = asyncio.create_task(
            self.tools.execute(tool_name, tool_args)
        )

        try:
            # Drain progress events while tool is running
            while not exec_task.done():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.3)
                    sse_str = self._progress_event_to_sse(event, emitter, message_id)
                    if sse_str:
                        yield (True, sse_str)
                except asyncio.TimeoutError:
                    continue

            # Drain remaining events after task finishes
            while not queue.empty():
                try:
                    event = queue.get_nowait()
                    sse_str = self._progress_event_to_sse(event, emitter, message_id)
                    if sse_str:
                        yield (True, sse_str)
                except asyncio.QueueEmpty:
                    break

            # Yield the final result
            result = await exec_task
            yield (False, result)

        except Exception as e:
            logger.error(f"Progress-reporting tool execution error: {e}")
            if not exec_task.done():
                exec_task.cancel()
            yield (False, f"Error executing {tool_name}: {str(e)}")
        finally:
            tool_instance._progress_queue = None

    @staticmethod
    def _progress_event_to_sse(
        event: dict[str, Any],
        emitter: "SSEEmitter",
        message_id: str,
    ) -> str | None:
        """Convert a progress event dict to an SSE string."""
        if not event or not isinstance(event, dict):
            return None
        evt_type = event.get("type", "")
        if evt_type == "step":
            return emitter.emit_progress(event.get("message", ""), message_id)
        elif evt_type == "html_delta":
            content = event.get("content", "")
            if content:
                return emitter.emit_html_delta(content, message_id)
        elif evt_type in ("image", "file", "video"):
            files = event.get("files")
            if files:
                return emitter.emit_files(
                    files=files,
                    message_type=evt_type,
                    message_id=message_id,
                )
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Pattern matching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _match_patterns(names: list[str], patterns: list[str]) -> list[str]:
        """Filter *names* using glob-style *patterns*.

        Supported patterns (via :func:`fnmatch.fnmatch`):
        - ``"*"``       → match everything
        - ``"exec"``    → exact match
        - ``"read_*"``  → prefix wildcard
        - ``"*file*"``  → contains wildcard
        - ``"web_??"``  → single-char wildcard

        Args:
            names: All available names (tools / skills).
            patterns: List of glob patterns provided by the caller.

        Returns:
            De-duplicated list of matched names, preserving the order
            in which they first appeared in *names*.
        """
        if not patterns or patterns == ["*"]:
            return list(names)

        matched: list[str] = []
        seen: set[str] = set()
        for name in names:
            for pat in patterns:
                if fnmatch.fnmatch(name, pat):
                    if name not in seen:
                        matched.append(name)
                        seen.add(name)
                    break
        return matched

    def _get_filtered_tool_definitions(
        self, tool_list: list[str]
    ) -> list[dict[str, Any]] | None:
        """Return tool definitions, optionally filtered by *tool_list*.

        Supports glob-style patterns in *tool_list*:
        - ``["*"]``                → all tools (default)
        - ``["exec", "read_*"]``   → exact + wildcard
        - ``["*file*"]``           → any tool containing "file"
        """
        all_defs = self.tools.get_definitions()
        if not tool_list or tool_list == ["*"]:
            return all_defs if all_defs else None

        all_names = [d.get("function", {}).get("name", "") for d in all_defs]
        allowed = set(self._match_patterns(all_names, tool_list))

        filtered = [d for d in all_defs if d.get("function", {}).get("name") in allowed]
        return filtered if filtered else None

    @staticmethod
    def _extract_last_user_message(
        openai_messages: list[dict[str, Any]],
    ) -> tuple[str, list[str] | None]:
        """Extract text and media from the last user message.

        Returns:
            ``(text, media_list | None)``
        """
        # Walk backwards to find last user message
        for msg in reversed(openai_messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                return content, None
            # Multimodal: list of content parts
            if isinstance(content, list):
                texts: list[str] = []
                media: list[str] = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            texts.append(part.get("text", ""))
                        elif part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            if url:
                                media.append(url)
                return " ".join(texts), media if media else None
        return "", None
